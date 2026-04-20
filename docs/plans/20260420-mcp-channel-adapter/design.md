# MCP 채널 어댑터 전환 설계

작성일: 2026-04-20
상태: Stage 1 착수. 스택 결정 — **전체 브릿지를 TS/Bun 으로 재구축** (공식 `server.ts` 미러링). `bridge/*.py` 는 병행 없이 직접 교체.

---

## §1 배경 및 동기

### 1.1 직접적 트리거

**20260420_123305 버그**: `⏺ Reading 1 file… (ctrl+o to expand)` 가 pre-approval
flush 시점에 완료 블록으로 간주되어 `StreamQueue.last_fp` 앵커로 설정됨. 툴 실행
완료 후 해당 블록이 `❯` 프롬프트 영역으로 말려 올라가면서 사라지자 앵커가 불능
상태가 됨 → 이후 모든 flush 에서 `detect_slip = anchor_mismatch` → `take_new()`
가 보수적으로 `[]` 반환 → `⏺ 두 질문 정리…`, `⏺ Update(...)` 등 실제 응답 블록
전체가 영구 stranded.

### 1.2 구조적 원인

| 레벨 | 문제 |
|---|---|
| L1 | `⏺ <verb>ing… (ctrl+o to expand)` 같은 transient tool-invocation 폼이 실제 응답 블록과 **구조적으로 구분 불가** |
| L2 | `_ACTIVE_BLOCK_RE` 는 `Running…\|Waiting…\|ctrl+b.*background` 3패턴만 잡고 나머지는 "완료 블록"으로 승격 → 앵커 오염 |
| L3 | `StreamQueue.take_new()` 는 앵커 유실 시 `[]` 반환 — **로그 없이 조용한 누락**. silent failure |
| L4 | pane 파싱 자체가 Claude Code UI 의 버전/verbose 모드/터미널 크기 변화에 취약 (구조적 coupling) |

### 1.3 기존 누적 이슈들의 공통분모

- **20260417_095801**: scrollback eviction → idx off-by-one → 앵커 도입으로 보완.
- **20260420_072013**: msg1 text + msg2 forward race → `⏺` stranded → `_queue_lock`
  + `_emergency_flush_before_input` A+ 패치로 완화.
- **20260420_123305** (오늘): anchor transformation → 해결 방식이 모두 **파싱 레이어
  내부의 보정 누적**.

세 건 모두 "pane 텍스트를 외부에서 파싱하여 Claude 의 의도를 재구성" 하는 접근의
본질적 취약점을 가리킴. 추가 땜질 대신 **레이어 자체 폐기** 가 필요.

### 1.4 대안으로서의 MCP 채널

Claude Code 는 `claude/channel` 이라는 MCP 프로토콜 capability 를 제공. 채널 MCP
서버는:

- Claude 에게 인바운드 메시지를 `<channel source="..." chat_id="..." ...>` 블록으로 주입
- Claude 가 `reply()` / `react()` / `edit_message()` 툴을 **명시적으로 호출**할 때만 발신
- `permission_request` notification 을 받아 승인 UI 를 채널에 relay

즉 **Claude 가 "무엇을 언제 어떻게 말할지" 판단**. 우리는 전달 플러밍만 소유.
pane 콘텐츠 파싱 불필요.

참고 구현: [anthropics/claude-plugins-official/external_plugins/telegram](https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/telegram)
(Bun/TypeScript, grammy 라이브러리, 1032 lines).

---

## §2 공식 플러그인 아키텍처 (레퍼런스)

### 2.1 활성화

```bash
/plugin install telegram@claude-plugins-official    # 1회
claude --channels plugin:telegram@claude-plugins-official    # 세션마다
```

### 2.2 MCP 툴 / notification

| 인터페이스 | 방향 | 역할 |
|---|---|---|
| `reply` | 툴 (Claude → MCP) | 텍스트 + 첨부 전송, `chat_id` 필수, `reply_to` 옵션 |
| `react` | 툴 | 메시지에 이모지 반응 (Telegram 화이트리스트) |
| `edit_message` | 툴 | 기존 봇 메시지 편집 — 푸시 알림 없음 |
| `download_attachment` | 툴 | 첨부 파일을 inbox 로 다운로드 |
| `claude/channel/permission_request` | notification (MCP → Claude) 의 역방향 notification | Claude 가 툴 승인을 요청할 때 MCP 서버가 채널로 relay |
| `claude/channel` 블록 주입 | MCP 서버 → Claude context | 인바운드 메시지를 `<channel>` 블록으로 세션에 전달 |

### 2.3 인증 / 접근 제어

- `~/.claude/channels/telegram/access.json` — allowlist + pairing 상태
- 페어링 모드: 초기 DM 에 6자리 hex 코드 → 유저가 `/telegram:access pair <code>` 로 승인
- 그룹 채팅: `requireMention` + `allowFrom` 서브리스트
- 권한 relay: 단일 유저 모델 — 그룹에는 `permission_request` 전달 안 함 (보안 결정)

### 2.4 구조적 한계

- **봇 토큰 1개 = polling 독점자 1개**. 같은 토큰으로 2세션 띄우면 `server.ts` 가
  PID 파일 기반으로 이전 세션을 SIGTERM 으로 강제 종료.
- 대화 기록 조회 불가 (Telegram Bot API 자체 한계 — 공식/자체 구현 모두 동일).

---

## §3 소유 경계

자체 구현에서 **claude-bridge2 가 유지할 범위** vs **Claude 에게 위임할 범위**.
전체 재구축 스택: **Bun + TypeScript** (공식 `server.ts` 를 baseline 으로 미러).

| 레이어 | 소유자 | 위치 (신규) |
|---|---|---|
| MCP channel 서버 (stdio) | **우리** | `channels/telegram/src/server.ts` — 공식 미러 |
| Telegram polling / Bot API | **우리** | `channels/telegram/src/telegram/` (grammy) |
| tmux 세션 생명주기 · resume · skip-permissions | **우리** | `channels/telegram/src/tmux/session.ts` (Bun `spawn`) |
| 세션 스폰 / 활성 전환 / 라우팅 테이블 | **우리** | `channels/telegram/src/dispatcher.ts` + `src/registry.ts` |
| 인증 / allowlist / pairing | **우리** | `channels/telegram/src/access.ts` (`~/.claude/channels/telegram/access.json` 기존 스키마 재사용) |
| 이미지 / 첨부 inbox | **우리** | `channels/telegram/src/inbox.ts` (`~/.claude-bridge/inbox/`) |
| Anomaly 로그 (always-on) | **우리** | `channels/telegram/src/anomaly.ts` (`~/.claude-bridge/anomaly.jsonl`) |
| compact / limit / resume picker / trust 감지 | **우리** | `channels/telegram/src/observer.ts` — pane 외부 관찰 (TS 이식) |
| busy→idle 전이 감지 (완료 알림용) | **우리** | 동 — tmux `capture-pane` 주기 샘플링 |
| `⏺` 블록 파싱 · 스트리밍 · 앵커 | **폐기** | (`parser.py` · `stream_queue.py` · `sender.py` **삭제**) |
| 승인 박스 파싱 / approval callback race | **폐기** | MCP `permission_request` notification 으로 대체 |
| "무엇을 언제 어떻게 말할지" 판단 | **Claude 본인** | 외부 상태 머신 불필요 |

핵심 구분: **pane 외부 관찰 (상태 감지) 은 TS 로 이식 · 유지**, **pane 콘텐츠 파싱
(메시지 추출) 은 전면 제거**.

### 3.1 Python 자산 처분

| 현재 (Python) | 처분 |
|---|---|
| `bridge/parser.py` 중 `⏺` 파싱 (`extract_response_blocks`, `_is_block_active`, `_ACTIVE_BLOCK_RE`, `_response_region`) | 삭제 |
| `bridge/parser.py` 중 상태 감지 (`COMPACT_RE`, `CONTEXT_LIMIT_RE`, `COMPACT_ERROR_RE`, `_STATUS_LINE_RE`, `is_busy`, `has_compaction`) | TS 로 포트 → `observer.ts` |
| `bridge/stream_queue.py` | 삭제 |
| `bridge/sender.py` | 삭제 (MCP `reply`/`edit_message`/`react` 가 대체) |
| `bridge/core.py` (`_queue_lock`, `_flush_completed*`, `_emergency_flush_before_input`) | 삭제 |
| `bridge/receiver.py` approval pane 처리 | 삭제 (`permission_request` relay 로 대체) |
| `bridge/tmux.py` | TS 재작성 (`tmux/session.ts`) — 의미론 보존 |
| `bridge/config.py` | TS 재작성 — config schema 동일 유지 |
| `bridge/dump.py` | TS 재작성 (opt-in 진단 채널 유지, anomaly 와 병존) |
| `bot.py` | 삭제 — 엔트리포인트는 `server.ts` (MCP stdio) |
| `tests/` Python | MCP 서버 테스트로 재작성 (Bun `test`) |

---

## §4 멀티 세션 UX

### 4.1 원칙

Telegram 채팅 1개 ↔ **활성 세션 1개**. 나머지는 백그라운드에서 조용히 돌고, 행동이
필요한 이벤트 (승인 / 완료 / 에러) 만 깨어남.

사용자 멘탈 모델: "지금 A 랑 대화 중". 전환은 **항상 명시적 커맨드**.

### 4.2 슬래시 커맨드

| 커맨드 | 동작 |
|---|---|
| `/sessions` | 전체 세션 리스트 + 각 상태 (active / busy / idle / pending-approval / error) |
| `/new [label]` | 새 세션 스폰, 활성으로 전환. label 없으면 자동 할당 (`s1`, `s2`, …) |
| `/switch <id\|label>` | 활성 전환 — 이 순간부터 해당 세션 출력만 채팅으로 흐름 |
| `/kill <id>` | 지정 세션 종료 (확인 단계 포함) |
| `/current` | 현재 붙어있는 세션 라벨 |
| `/backlog [id]` | 전환 시 생략된 백로그 전체 열람 (기본: 현재 세션) |

커맨드 이외의 경로 (예: 자연어 "B 로 가줘") 는 **명시적으로 지원 안 함** — 오인식
위험.

### 4.3 메시지 라우팅 규칙

| 이벤트 | 활성 세션 | 백그라운드 세션 |
|---|---|---|
| Claude 의 `reply()` | 채팅에 그대로 전달 | **무시** (로컬 로그에만 남음, 백로그에 보관) |
| `permission_request` | 채팅으로 relay (기존 UX) | 채팅으로 relay + `[라벨]` 태그 |
| 작업 완료 (busy→idle 전이) | 상태판 갱신만 (새 메시지 없음) | **1회 알림 카드** 발생 |
| 에러 · limit · compact error · crash | 즉시 채팅으로 (태그 포함) | 즉시 채팅으로 (태그 포함) |

핵심: **백그라운드 reply() 는 버린다**. 행동 유도 이벤트만 새어 나옴 = Telegram
뱃지의 실질적 대체.

### 4.4 알림 카드 포맷

**정상 완료**:
```
✅ [B] 완료 · 2m 38s
   마지막: "빌드 통과, 테스트 12개 초록"
   [🔀 B로 전환]  [📋 backlog]
```

**에러**:
```
🛑 [B] 에러 — "Context limit reached"
   [🔀 전환]  [♻️ resume]
```

**승인 대기 (백그라운드 발생)**:
```
🔔 [B] 승인 필요
   파일 편집: src/x.py
   [✅ 허용]  [❌ 거부]  [🔀 전환]
```

**합치기 규칙**: 직전 배경 알림 이후 10초 안에 또 배경 이벤트가 발생하면 **기존 카드
`edit_message`** 로 병합 (새 카드 생성 금지).

### 4.5 상단 pinned 상태판

```
📋 Sessions                    (silent edit, 푸시 없음)
  ▶ A (active) — "lint 고치는 중"
  🔔 B (busy)  — 2분 경과
  ⏸ C (idle · 5m 전 완료)
```

- 상태 변화 시 `edit_message` 로 갱신. `disable_notification=true` 로 푸시 억제.
- 편집 rate: 200ms debounce + Telegram API 제한 (분당 20회 안팎) 준수.

### 4.6 전환 시 backlog

```
/switch B
━━━━━━━━━━━━━━━━━━━━━
▶ [B] 로 전환됨
  마지막 활동: 3분 전
  백그라운드 중 3개 메시지 생략
  [📋 전체 backlog 보기]
━━━━━━━━━━━━━━━━━━━━━
```

- 전체 스트리밍 재생 **안 함**. 요약 + on-demand 열람 (`/backlog B`).
- 백로그 저장: 세션별 링버퍼 (최근 N 메시지), TTL N 시간.

### 4.7 Liveness 인디케이터 (Pattern B 대응)

Spike 관찰: Claude 는 긴 작업 중 **침묵** 하고 최종 `reply()` 만 호출. 터미널에서는
`⏺ ...` 로 흘러가지만 Telegram 에는 아무것도 안 옴 → 사용자는 bot 이 죽었는지
작업 중인지 구분 불가.

대책 (브릿지 측 주도, Claude 관여 X):

- **활성 세션 상태판** 에 ⏳ 아이콘 + busy 경과 시간 (`edit_message` silent).
- 30초 동안 `reply()` 없이 busy 상태면 **inline 로딩 라인** 을 본문이 아닌 상태판에
  한 줄 추가 (`▶ A — ⏳ 작업중 · 45s`).
- idle 복귀 (busy→idle 전이 감지) 시 상태판 원상복구.
- **본문 채팅은 건드리지 않음** — 진짜 메시지 (reply / permission) 만 본문에.

감지 근거: `capture-pane` 주기 샘플링 + `observer.ts` 의 busy 판정 (비-`⏺` 관찰).

### 5.1 문제

기존 `dump/` 는 `CB_DUMP=1` 일 때만 동작 → 프로덕션 운영에서 silent failure 발생
시 **아무 로그도 남지 않음**. 오늘 버그도 `take_new()` 가 `[]` 반환하는 경로에서
로그 없이 증상만 발생.

### 5.2 설계

| 속성 | 값 |
|---|---|
| 활성화 | **항상 ON** (env 스위치 없음) |
| 위치 | `~/.claude-bridge/anomaly.jsonl` (기존 `panes/` 와 동일 부모) |
| 로테이션 | 5 MB 초과 시 `.1 → .2 → .3` 슬라이드, 3개 보관 |
| 쓰기 실패 | silent (bot 로직 영향 0) |
| API | `anomaly.log(kind, **ctx)` |
| CB_DUMP=1 병행 | `dump/events.jsonl` 에 복제 기록 (타임라인 상관분석) |

### 5.3 기록 대상

| 카테고리 | kind | 컨텍스트 |
|---|---|---|
| 채널 전달 실패 | `channel_reply_failed` | chat_id, session_id, error, retry_count |
| permission_request timeout | `permission_timeout` | tool_name, session_id, elapsed |
| 세션 스폰 실패 | `session_spawn_failed` | reason, tmux_error |
| 인바운드 라우팅 실패 | `inbound_no_active_session` | chat_id, message_preview |
| busy watchdog | `busy_timeout` | session_id, duration |
| compact 에러 | `compact_error` | session_id |
| 알 수 없는 MCP 요청 | `mcp_unknown_method` | method name |
| 상태판 edit 실패 | `status_panel_edit_failed` | error, backoff |

### 5.4 가시성 (옵션, 후속)

- `/status` 슬래시 커맨드에 "최근 24h anomaly 카운트 (kind 별)" 노출.
- rate 임계 초과 시 운영자 DM 노티 (`anomaly.kind == X 가 분당 Y 초과`).

---

## §6 점진 이행 로드맵

**병행 없음**. Python 브릿지는 살려두고 TS 브릿지를 독립 기동하다가, Stage 4 에서
프로덕션 봇 토큰을 TS 쪽으로 전환하는 **일회성 컷오버**. 병행이 없으므로 각 단계는
"테스트 봇으로 완결시키기" 목표.

```
Stage 0 (완료)    공식 플러그인 spike → 스펙 이해 · 범위 확정
Stage 1 (진행중)  TS 스켈레톤 + reply + anomaly — 테스트 봇으로 ping
Stage 2           permission · edit · react · attachment + tmux · observer
Stage 3           멀티 세션 디스패처 + 슬래시 커맨드 + 상태판
Stage 4           프로덕션 컷오버 — bridge/*.py 삭제, 봇 토큰 이관
```

### Stage 0 — Spike (완료)

`spike-log.md` 참조. 공식 `server.ts` 미러 기준 확정.

### Stage 1 — 스켈레톤 + `reply` + anomaly (현재)

목표: 테스트 봇에 "hello" 한 줄 날아가면 끝.

- `channels/telegram/` 패키지 생성 (Bun + TS)
- deps: `@modelcontextprotocol/sdk`, `grammy`, `zod`
- `src/server.ts` — stdio MCP 서버, `reply` 툴만 등록
- `src/anomaly.ts` — always-on JSONL 로거 (Stage 1부터 기록 보장)
- `src/access.ts` — `~/.claude/channels/telegram/access.json` 재사용
- `src/telegram/client.ts` — grammy wrapper (sendMessage 만)
- 기동: `claude --channels ./channels/telegram/src/server.ts`
- 검증: 테스트 봇 (`8565536972`) 으로 reply 수신

### Stage 2 — 핵심 MCP 표면 + 상태 감지

- `download_attachment` · `edit_message` · `react` 툴 추가
- `notifications/claude/channel/permission_request` → InlineKeyboard relay
- `src/tmux/session.ts` — `tmux new-session`/`send-keys`/`capture-pane` 래퍼
- `src/observer.ts` — compact / limit / resume picker / busy→idle 감지 이식
  (기존 `parser.py` 의 비-⏺ 정규식 포트)
- 단위 테스트: Bun `test` — 승인 · 거부 · 타임아웃 · busy 전이

### Stage 3 — 멀티 세션 + UX

- `src/registry.ts` — 세션 레지스트리, active 포인터
- `src/dispatcher.ts` — 슬래시 커맨드 (`/new`, `/switch`, `/kill`, `/sessions`, `/current`, `/backlog`)
- 라우팅: 인바운드 → 활성 세션의 MCP stdio 만. 백그라운드 `reply()` 는 세션 링버퍼로.
- 상단 pinned 상태판 (`edit_message` silent) + liveness ⏳ (§4.7)
- 완료 카드 / 에러 카드 / 승인 카드 (§4.4)

### Stage 4 — 컷오버 (일회성)

- 프로덕션 봇 (`8711328028`) 의 polling consumer 를 Python bridge → TS 서버로 전환
- `bot.py` · `bridge/parser.py` (부분) · `bridge/stream_queue.py` · `bridge/sender.py` ·
  `bridge/core.py` · `bridge/receiver.py` · `bridge/tmux.py` · `bridge/config.py` ·
  `bridge/dump.py` · `bin/*.py` · `tests/*.py` **삭제**
- `bridge/` 디렉토리 비우고 `channels/telegram/` 이 유일한 코드 루트.
- 테스트 봇 (`8565536972`) 은 staging 으로 **유지** — 이후 변경은 여기서 선검증.
- 롤백 플랜: `git revert <cutover-sha>` → Python 봇 바로 기동 가능 (컷오버 직전 커밋 기준).

---

## §7 스펙 안정성 대응

`claude/channel` 은 MCP `experimental:` 네임스페이스 — Anthropic 이 깨는 변경 가능.

### 7.1 추적 방법

- 레퍼런스: [anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official) `external_plugins/telegram/server.ts`
- CI 주기 검사 (예: 주 1회):
  ```
  gh api repos/anthropics/claude-plugins-official/commits?path=external_plugins/telegram
  ```
  최근 커밋 SHA 가 우리 `docs/plans/20260420-mcp-channel-adapter/spec-baseline.sha`
  와 다르면 diff 검토.
- 우리 MCP 서버의 capability 문자열 / 툴 시그니처 / notification 메서드명은
  **공식 server.ts 와 1:1 미러링**. 변경 생기면 알람.

### 7.2 시그니처 변경 대응

- **툴 파라미터 추가**: 무시하면 됨 (forward-compatible).
- **툴 파라미터 삭제 / 이름 변경**: anomaly 채널에 `mcp_unknown_method` 기록 후
  호환 레이어 작성.
- **capability 이름 변경**: 우리 쪽에서 양쪽 지원 (구·신 병기) 하는 adapter 패턴.

---

## §8 폐기 · 이식 인벤토리

Stage 4 컷오버에서 Python 브릿지 전체 제거. 아래 표는 **무엇이 TS 로 넘어가고
무엇이 사라지는지**.

### 8.1 TS 로 이식 (의미론 보존)

| 원본 (Python) | 대상 (TS) | 비고 |
|---|---|---|
| `bridge/parser.py` — `COMPACT_RE`, `CONTEXT_LIMIT_RE`, `COMPACT_ERROR_RE`, `_STATUS_LINE_RE`, `is_busy`, `has_compaction` | `src/observer.ts` | pane 외부 관찰 (compact/limit/busy) 유지 |
| `bridge/tmux.py` | `src/tmux/session.ts` | `tmux new-session`/`send-keys`/`capture-pane`/`pipe-pane` 래퍼. Bun `spawn` |
| `bridge/config.py` | `src/config.ts` | 스키마 동일. `~/.claude-bridge/config.json` 기존 경로 재사용 |
| `bridge/dump.py` | `src/dump.ts` | opt-in 진단 — `CB_DUMP` env. anomaly 와 병존 |
| `tg_images/` inbox | `src/inbox.ts` + `~/.claude-bridge/inbox/` | 경로 · 규칙 재사용 |

### 8.2 완전 삭제

| 경로 | 이유 |
|---|---|
| `bridge/parser.py::_ACTIVE_BLOCK_RE` · `_is_block_active` · `extract_response_blocks` · `_response_region` | ⏺ 분류 레이어 자체 제거 |
| `bridge/stream_queue.py` (전체) | 앵커 · idx · slip 개념 불필요 |
| `bridge/sender.py` (전체) | MCP `reply`/`edit_message`/`react` 가 대체 |
| `bridge/core.py::_queue_lock` · `_flush_completed*` · `_emergency_flush_before_input` | 스트리밍 폐기와 함께 |
| `bridge/core.py` (잔여) | 엔트리포인트는 `server.ts` |
| `bridge/receiver.py` | pane 기반 approval → `permission_request` relay |
| `bot.py` | 엔트리포인트 이동 |
| `bin/*.py` | 운영 스크립트도 TS 재작성 or 쉘로 축소 |
| `tests/` Python 전체 | Bun `test` 로 재작성 |
| `tests/test_bug_20260417_*.py`, `tests/test_bug_20260420_*.py` | 새 경로 기반 회귀로 재작성 |

### 8.3 신규 (TS 전용)

| 경로 | 역할 |
|---|---|
| `src/server.ts` | MCP stdio 엔트리 |
| `src/telegram/poller.ts`, `client.ts` | grammy long-polling + Bot API |
| `src/dispatcher.ts`, `src/registry.ts` | 멀티 세션 디스패처 |
| `src/anomaly.ts` | always-on JSONL 로거 |
| `src/access.ts` | allowlist · pairing (`~/.claude/channels/telegram/access.json` 재사용) |

### 8.4 롤백 안전망

컷오버 직전 커밋이 Python 봇 기동 가능 상태. 문제 발생 시 `git revert` 1방으로
복구. 테스트 봇은 staging 으로 상시 가용 (`channels/telegram/` 만 기동).

---

## §9 미해결 항목

1. **세션 간 컨텍스트 공유** — `/switch B` 한 뒤 "방금 A 에서 본 에러를 B 에게
   보여줘" 같은 교차 요청을 어떻게 처리할지. 일단은 **수동 복사-붙여넣기** 로
   시작, 필요 시 `/share A→B <range>` 커맨드 도입.
2. **Telegram 편집 rate 한계 vs 상태판 반응성** — 상태 변화가 잦을 때 debounce
   200ms 가 충분한지 운영 데이터로 튜닝.
3. **백그라운드 링버퍼 크기 / TTL** — 초기값은 메시지 100개 / 24h. 실데이터로
   조정.
4. **단계 4 직전 회귀 테스트 전략** — pane 스트리밍 삭제 시 과거 3개 버그가 새
   경로에서 재발 가능한지 골든 dump 기반 검증 필요. 설계 시 단계 4 착수 전
   하위 계획 문서로 상세화.

---

## 부록 A. 참고 링크

- [anthropics/claude-plugins-official/external_plugins/telegram](https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/telegram)
- [server.ts](https://github.com/anthropics/claude-plugins-official/blob/main/external_plugins/telegram/server.ts)
- [Claude Code channels 문서](https://code.claude.com/docs/en/channels.md)
- [Claude Code permission modes](https://code.claude.com/docs/en/permission-modes.md)

## 부록 B. 관련 과거 기록

- `docs/plans/20260418-esc-flush-recovery/` — ESC flush 땜질
- `docs/plans/20260419-pipe-pane-redesign-poc/` — pipe-pane raw 수집 (유지)
- `bugreport/20260418_094744/` — ESC flush 증상
- `dump/20260420/123305_claude-bridge2/` — 본 설계 트리거 버그 증거
