# claude-bridge

**텔레그램에서 내 PC의 Claude Code를 원격 제어**하는 브리지 데몬.
외출 중에도, 자기 전 침대에서도 진행 중인 개발 세션을 이어갈 수 있습니다.

---

## 요구사항

- macOS / Linux
- Python 3.11+
- tmux
- Claude Code CLI (기본 경로: `/Applications/cmux.app/Contents/Resources/bin/claude`)
- Telegram Bot Token ([@BotFather](https://t.me/botfather) 에서 발급)

## 설치

```bash
git clone https://github.com/kunrunic/claude-bridge.git
cd claude-bridge
./bin/setup.sh
```

`bin/setup.sh` 가 대화형으로 진행합니다:

1. Python 3.11+ / tmux 확인
2. `venv` 생성 여부 확인
3. `python-telegram-bot` 설치 여부 확인
4. Claude 실행 파일 경로 확인
5. **Bot Token 입력** (입력 시 문자 숨김 처리)
6. 봇 임시 실행 → 텔레그램에서 `/whoami` 로 `chat_id` 확인
7. `chat_id` 입력 → `config.json` 자동 완성

## 실행

```bash
./bin/start.sh           # 백그라운드 시작
./bin/start.sh --dump    # 디버그 덤프 모드 (dump/ 에 0.5초 pane 스냅샷 기록)
./bin/stop.sh            # 봇만 종료 — tmux/Claude 는 독립 유지 (세션 락은 자동 해제)
./bin/stop.sh --all      # 봇 + tmux + Claude 모두 종료 (Claude 에 /exit 먼저 보내 graceful shutdown)
./bin/restart.sh         # 재시작 (stop → start, 인자는 start.sh 로 패스스루)
```

**stop 의 두 모드**
- 기본 `./bin/stop.sh` — 봇만 종료. tmux 세션은 살아있어 다음 `./bin/start.sh` 가 자동 재연결. 봇 재배포/업데이트 용도.
- `./bin/stop.sh --all` — Claude 에 `/exit` 전송 후 5초 graceful wait → tmux kill-session. Claude 대화 이력은 `~/.claude/projects/` 에 저장되므로 다음번 `/start` 에서 `--resume` 으로 이어갈 수 있음. 다만 실행 중이던 Bash/Edit 등 tool 작업은 중단.

**서버에서 현재 상태 엿보기 — `./bin/capture.sh`**

SSH 접속 후 bridge 가 지금 뭘 하고 있는지 빠르게 확인하는 도구:

```bash
./bin/capture.sh                 # 현재 패널 마지막 500줄 출력 (ANSI 제거)
./bin/capture.sh -n 1000         # 줄 수 지정
./bin/capture.sh --raw           # ANSI 색상 코드 포함
./bin/capture.sh --save          # capture/YYYYMMDD_HHMMSS.txt 저장, 경로 출력
./bin/capture.sh --follow        # tmux attach -r (read-only) — 실시간 관찰, Ctrl+B d 로 detach
```

`--follow` 는 read-only attach 라 키 입력이 봇 쪽으로 전달되지 않아 안전합니다.

- 로그: `logs/YYYY-MM-DD.log` (일별, 3일 보관 후 자동 삭제)
- PID: `.bot.pid` (중복 실행 방지)
- 덤프: `dump/YYYYMMDD/HHMMSS_{instance}/` (3일 보관, `--dump` 기동 시에만 생성)

### 여러 인스턴스 동시 운영

폴더를 복제해서 각각 setup 하면 됩니다. 각 폴더는 독립된 `config.json`/`logs/`/`.bot.pid` 를 가지며 `tmux_session` 이름은 **폴더명에서 자동 파생**됩니다.

```bash
# 예: 업무용 / 개인용 2개 운영
cp -r claude-bridge claude-bridge-work
cp -r claude-bridge claude-bridge-personal
# 각각 다른 봇 토큰으로 setup
(cd claude-bridge-work && ./bin/setup.sh)
(cd claude-bridge-personal && ./bin/setup.sh)
# 동시 실행
(cd claude-bridge-work && ./bin/start.sh)
(cd claude-bridge-personal && ./bin/start.sh)
```

→ tmux 세션명: `claude_bridge_work`, `claude_bridge_personal` 로 자동 분리됨.

두 인스턴스가 같은 Claude 세션을 동시에 resume하려 할 때는 **세션 락**이 자동으로 충돌을 막습니다. (아래 [멀티 인스턴스 안전](#멀티-인스턴스-안전) 참고)

## 텔레그램 명령어

| 명령 | 동작 |
|------|------|
| `/start` | 세션 목록(최근 활동순) / 새 세션 / 권한 모드 토글 |
| `/end` | 현재 세션 종료 |
| `/esc` | Escape 키 전송 (승인창 취소, 작업 중단) |
| `/unlock` | 이 인스턴스가 보유한 세션 락 강제 해제 |
| `/whoami` | 내 chat_id 확인 (초기 설정용) |

---

## 문제 진단 & 수리

이상 동작 발견 시 2단계로 진행:

### 1) 버그 리포트 — `./bin/bugreporter.sh`

현장 증거 수집 + Claude 자체 분석 → **수정안 제안서** 작성 (실제 코드 수정은 하지 않음).

```bash
./bin/bugreporter.sh
```

동작:
1. `bugreport/YYYYMMDD_HHMMSS/` 디렉토리에 증거 수집
   - `tmux_capture.txt` — 현재 tmux 패널 마지막 500줄 + 세션 상태
   - `logs/` — 포그라운드 구조화 로그 사본
   - `config_masked.json` — 토큰 마스킹된 설정
   - `bot.py`, `bridge/*.py` — 소스 스냅샷
   - `dump/` — 최근 1시간 내 덤프가 있으면 자동 포함 (스트리밍 이슈 추적용)
2. 해당 디렉토리에 `CLAUDE.md` 자동 작성 (역할/분석 순서/출력 양식 지시)
3. 대화형 Claude Code 실행 → Claude가 증상을 직접 질문
4. 로그 타임스탬프와 tmux 화면, dump 이벤트를 교차 대조해
   **타임라인 복원 → 원인 가설 → 재현 방법 → 제안 수정안** 포함한 `BUG_REPORT.md` 생성

### 2) 수리 — `./bin/repairer.sh`

bugreporter 가 만든 BUG_REPORT.md 의 제안을 실제 코드에 적용.

```bash
./bin/repairer.sh                              # 가장 최근 bugreport 사용
./bin/repairer.sh bugreport/20260416_153012    # 특정 리포트 지정
```

동작:
1. 최근 (또는 지정한) `bugreport/.../BUG_REPORT.md` 로드
2. 프로젝트 루트에 임시 CLAUDE.md 생성 (수리 가이드)
3. 대화형 Claude Code 실행 → 수정 범위를 먼저 요약해 승인 요청 → Edit 적용 → 테스트 실행
4. 종료 시 임시 CLAUDE.md 원복
5. 커밋은 수행하지 않음 (사용자가 직접 수행)

### 디버그 덤프 — `--dump`

텔레그램에 AI 응답 일부가 누락되는 등 **스트리밍/타이밍 이슈** 추적용:

```bash
./bin/stop.sh
./bin/start.sh --dump
# (이슈 재현 대기)
./bin/bugreporter.sh   # dump 자동 포함됨
```

기록 대상:
- `dump/.../pane_tick.jsonl` — 0.5초 주기 tmux pane 스냅샷 (hash-dedup)
- `dump/.../events.jsonl` — tmux send_input/send_key, sender send_output,
  receiver on_message/callback, core flush_peek/flush_block 이벤트 로그

평상시엔 켜지 마세요 — 활성 상태에서는 디스크 사용량이 적지 않습니다 (대략 일당 ~250MB 가정).
3일 지나면 자동 정리됩니다.

### 버그 제보

1. [Issues 탭 → New issue → Bug report](../../issues/new?template=bug_report.md)
2. `bugreport/YYYYMMDD_HHMMSS/BUG_REPORT.md` 내용 복사 → 이슈 본문에 붙여넣기
3. 필요 시 텔레그램 스크린샷 첨부

---

## 주요 기능

### 세션 연속성
- `~/.claude/projects/` JSONL을 스캔해 **실제 마지막 활동 시간** 기준으로 정렬
- 프로젝트별로 세션 구분 표시 (`[blackwell]`, `[env]` 등)
- agent/subagent 세션 자동 필터링 (resume 불가능한 것 숨김)
- 세션 원래 `cwd` 자동 감지 → `cd {경로} && claude --resume {id}` 로 올바른 컨텍스트 복구

### 봇 재시작 안전
- 봇이 죽거나 재시작해도 **tmux 세션은 독립 유지**
- 재기동 시 기존 tmux 세션 자동 감지해 모니터링 재개
- 중복 실행 방지 ("기존 유지 / 새로 시작" 선택 UI)
- **폴링 재연결 루프**: 네트워크 오류 시 exponential backoff(1→2→4→…→60초)로 자동 재연결, 10회 실패 시 프로세스 종료(systemd/launchd 자동 재시작 트리거)

### 멀티 인스턴스 안전

두 개의 봇 인스턴스(cb1/cb2)가 같은 Claude 세션을 동시에 resume하는 것을 **파일 락**으로 방지합니다.

- 락 파일: `~/.claude/.cb_lock_{session_id}` (소유 인스턴스 + chat_id 기록)
- `open("x")` exclusive create로 race condition 없는 원자적 획득
- stale 락 자동 감지: 락 소유자의 tmux 세션이 없으면 자동 해제 후 재획득
- 봇 종료 시(`stop.sh`) 이 인스턴스가 보유한 **모든 락 자동 해제**
- 텔레그램에서 `/unlock` 으로 원격 해제 (본인 chat_id 소유 락만)
- 잠금 충돌 시 인라인 버튼으로 바로 해제 가능

```
⚠️ 이미 사용 중인 세션입니다. /unlock으로 해제 후 다시 시도하세요.
[ /unlock 실행 ]
```

### 스마트 출력 필터링
- 작업 중(`esc to interrupt` 감지)일 때는 **상태 메시지 1개를 계속 edit** → 채팅 스팸 없음
- `⏳ Compacting conversation… (54s)` 처럼 실제 작업 상태 + 경과 시간 라이브 표시
- `⏺` 마커가 있는 Claude 응답만 정확히 추출해 전송
- 긴 응답은 청크로 자동 분할 `[1/3]`, `[2/3]`, `[3/3]`
- 연쇄 tool 호출 사이의 중간 설명도 누락 없이 전달 (pre-busy/pre-approval flush)

### 승인 흐름

Claude Code의 `Do you want to proceed?` 감지 → 텔레그램 인라인 버튼

```
승인 요청:
┌─────────────────────┐
│ Bash                │
│ ls -la              │
│ Do you want to...   │
└─────────────────────┘
[ Yes (승인) ]  [ No (거부) ]
```

- 폴더 신뢰 프롬프트는 본인 PC이므로 **자동 승인**
- 승인 후엔 짧은 요약으로 교체 (`✅ 승인 · Bash`)
- `--dangerously-skip-permissions` 모드 토글 + 세션 재시작 지원

#### 늦은 승인 응답 처리 (승인 프롬프트 만료 후)

몇 시간 뒤에 Yes/No를 탭해도 상황에 맞게 처리합니다:

| 상황 | Yes 탭 | No 탭 |
|------|--------|-------|
| 프롬프트 아직 있음 | Enter 전송 (정상) | Down+Enter 전송 (정상) |
| 프롬프트 만료, 세션 살아있음 | Claude에 "승인했습니다. 이어서 진행해주세요." 전송 | Claude에 "거부했습니다. 취소해주세요." 전송 |
| 세션 죽음 | 세션 자동 재시작 후 승인 메시지 전송 | "세션을 다시 시작하시겠습니까?" 메뉴 표시 |

### 사용량 한도 감지

Claude API 한도 초과 시 즉시 알림 + 리셋 시각 파싱:

```
⚠️ Claude 사용량 한도에 도달했습니다.
5:00 PM (Pacific Time)에 리셋됩니다. 그때 다시 보내주세요.
```

한도 해제 감지 시 자동으로 정상 모드 복귀.

### 이미지 첨부
- 텔레그램에서 이미지+캡션 전송 → 로컬 저장 후 Claude에 경로 전달
- Claude가 Read 도구로 이미지 분석

### 보안
- `allowed_ids` 화이트리스트만 사용 가능 (봇을 통한 등록 불가, `config.json` 직접 편집)
- 타인이 봇 발견해도 `/whoami` 외 모든 기능 차단
- 토큰·ID는 `config.json` 외부 파일로 분리 (`.gitignore` 처리)

### 구조화된 포그라운드 로그
```
[10:35:45] [USER→BOT]    이 테스트 결과 어떻게 나왔어?
[10:35:45] [BOT→AI]      forwarded to Claude (waiting for response)
[10:35:48] [AI-BUSY]     Thinking… (2s)
[10:36:02] [AI→BOT]      response (847 chars)
[10:36:02] [BOT→USER]    delivered
[10:36:15] [AI-APPROVAL] Bash
[10:36:20] [USER-ACK]    approved
[10:38:01] [AI-LIMIT]    5:00 PM (Pacific Time)
[10:38:01] [UNLOCK]      sessABC12345 by chat_id=12345678
```

---

## 아키텍처

```
[Telegram]  ←→  [bot.py 데몬]  ←→  [tmux 'claude_bridge']
  폰/태블릿        asyncio         │
                   │               └─ claude --resume {id}
                   │                    (cwd = 세션 원래 경로)
                   │
                   ├─ monitor task (비동기, tmux_run_async)
                   ├─ polling reconnect loop (exponential backoff)
                   └─ ~/.claude/.cb_lock_* (세션 락)
```

- **비동기 tmux 호출** — `run_in_executor`로 subprocess를 스레드풀에서 실행, 이벤트 루프 블로킹 없음
- **subprocess timeout 5s** — tmux 명령 무한 대기 방지
- **Telegram Request timeout** — `connect_timeout=10s`, `read_timeout=15s`
- **tmux 폭 80 cols** — 모바일 텔레그램에서도 레이아웃 유지
- **HTML `<pre>`** 포맷 — `_변수명_` 같은 Markdown 특수문자 충돌 없음
- **라인 단위 청크 분할** — 긴 응답도 온전히 전달
- **모니터 오류 카운터** — 5회 연속 경고, 10회 연속 모니터 종료 + Telegram 알림

## 테스트

```bash
python -m pytest tests/ -q
```

128개 테스트: 단위 / 동시성 / 시나리오 / tmux 죽음 / 승인 재연결 / 스트림 큐 커버.
