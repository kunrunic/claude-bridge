# claude-bridge

**Telegram 에서 내 PC 의 Claude Code 를 원격 조작**한다.
외출 중에도, 자기 전 침대에서도 — 진행 중인 개발 세션을 이어간다.

PC 가 켜져 있고 봇이 폴링 중이면 어디서든 동작. Bun + TypeScript.

---

## claude-bridge 가 이런 순간을 해결합니다

- 💡 외출 중 갑자기 떠오른 수정을 **바로 지시**
- 📱 노트북 없이 폰만 들고 **PR 리뷰·수정·배포** 진행
- 🔀 리서치 / 구현 / 리뷰 여러 세션을 **동시에** 띄워놓고 오가기
- 🌙 긴 빌드·테스트·배포를 시켜놓고 폰으로 **진행상황** 확인
- 🔐 원격이지만 **각 도구 호출마다 승인**을 직접 컨트롤
- 🏢 업무용 / 개인용을 **서로 다른 봇 인스턴스**로 분리 운영

---

## 빠른 시작

```bash
git clone https://github.com/kunrunic/claude-bridge.git
cd claude-bridge
bun install
./bin/setup.sh          # 대화형 — 토큰 입력 + user_id 한 번
./bin/start.sh          # 백그라운드 기동
```

Telegram Bot Token ([@BotFather](https://t.me/botfather)) 과 본인
Telegram user_id (@userinfobot 으로 확인) 만 있으면 준비 완료. `setup.sh`
가 `tg_channel` MCP 서버를 user scope 로 등록하므로 어떤 디렉토리에서 세션을
spawn 해도 바로 동작한다.

이후 봇에 `/new` 를 보내 새 세션을 만들거나, `/resume` 으로 과거 세션을
이어갈 수 있다.

---

## 동작 흐름

```
┌──────────────┐       ┌─────────────────────────────────────┐
│ Telegram     │ HTTPS │  dispatcher.ts (Bun)                │
│ Bot API      │<─────>│  polling + session routing          │
└──────────────┘       └──────────────┬──────────────────────┘
                                      │ IPC (unix socket)
                      ┌───────────────┼───────────────┐
                      ▼               ▼               ▼
              ┌──────────────┐┌──────────────┐┌──────────────┐
              │ MCP stdio s1 ││ MCP stdio s2 ││ MCP stdio s3 │
              │ reply/react  ││ reply/react  ││ reply/react  │
              │ edit/attach  ││ edit/attach  ││ edit/attach  │
              └──────┬───────┘└──────┬───────┘└──────┬───────┘
                     │ stdio         │ stdio         │ stdio
                     ▼               ▼               ▼
              ┌──────────────┐┌──────────────┐┌──────────────┐
              │ Claude Code  ││ Claude Code  ││ Claude Code  │
              │ (tmux cb-s1) ││ (tmux cb-s2) ││ (tmux cb-s3) │
              └──────────────┘└──────────────┘└──────────────┘
```

Telegram 메시지는 dispatcher 가 long-polling 으로 받아 **현재 활성 세션**
의 MCP 서버로 전달한다. Claude 는 답할 준비가 되면 `reply()` 도구를
호출하고, 그 호출이 다시 dispatcher 를 거쳐 Telegram 으로 나간다. 각 세션은
독립된 tmux 프로세스 + 독립된 MCP 서버를 갖는다.

---

## 주요 기능

### 멀티 세션 오케스트레이션

dispatcher 가 여러 Claude Code 세션을 허브처럼 관리한다. 각 세션은 자신의
tmux, 자신의 MCP 서버, 자신의 작업 디렉토리를 가지고 **독립적으로**
돌아간다. Telegram 챗창 하나에서 모든 세션의 라이프사이클을 컨트롤한다.

- `/new [label] [cwd]` — 새 세션 spawn. 라벨과 작업 디렉토리 지정 가능
- `/resume` — `~/.claude/projects` 의 **모든 과거 Claude Code 세션** 복원.
  컨텍스트, 파일 히스토리, cwd 가 그대로 살아남. 어제 노트북에서 하던 작업을
  오늘 폰에서 이어간다
- `/fork` — 기존 세션의 **컨텍스트를 상속받은 분기 세션** 생성. 실험적
  방향을 원본 안 건드리고 시도하거나, 무거워진 세션에서 가벼운 갈래로
  탈출할 때
- `/sessions` — 활성 세션 목록 (고정폭 포맷: 인덱스·상태·라벨). **목록에서
  원하는 세션을 탭하면 즉시 활성 세션으로 전환** — 리서치 → 구현 → 리뷰를
  한 챗창에서 오감
- `/kill` — 세션 종료 (세션 picker 에서 선택)
- **활성 세션은 Telegram 상단에 pin 으로 고정** — 지금 어떤 세션과
  대화하는지 항상 보임. pin 은 세션 전환 시 자동 갱신

### 세션별 퍼미션 모드 선택

`/new` · `/resume` · `/fork` 할 때 **🔒 Normal / 🔴 Skip permissions**
중 하나를 인라인 키보드로 선택. 세션마다 다르게 갈 수 있다 — 신뢰하는
로컬 작업은 skip, 원격 배포 세션은 normal.

### 작업 중 시각 피드백

메시지 전달부터 응답까지 Claude 가 뭘 하는지 눈으로 바로 확인 가능 —
IPC 기반 상태 머신으로 구동하므로 터미널 파싱 누락 없이 동작:

- 메시지 전달 직후 ✍ **reaction** 이 내 메시지에 붙음 (Claude가 받았다)
- 🤔 → 💭 → 🧐 → 🤓 → 💡 → 🤯 **얼굴 이모지 애니메이션** 이 5초마다 순환
  (Claude 작업 중)
- Claude가 응답하면 ✅ 로 바뀌고 5초 후 자동 삭제 (깔끔)
- 긴 작업일 때 Claude 가 `edit_message` 로 중간 진행상황을 같은 메시지에
  업데이트하도록 **프롬프트로 유도** (완전 강제는 아니며 Claude 판단에
  따라 실제 호출 빈도가 다를 수 있음)

### 안정적인 메시지 교환 — MCP 채널 프로토콜

Claude 가 네 가지 MCP 도구를 직접 호출해 Telegram 과 대화한다:

| 도구 | 용도 |
|---|---|
| `reply` | 메시지 전송 (푸시 알림 발생) |
| `react` | 이모지 반응 |
| `edit_message` | 기존 메시지 무음 수정 (진행상황 업데이트) |
| `download_attachment` | 첨부 파일 다운로드 |

이 방식은 [anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official)
의 Telegram 플러그인과 **동일한 MCP 채널 프로토콜**을 따른다. Claude 가
"답할 차례" 라고 판단했을 때 정확한 도구 호출로 응답하므로, 터미널 출력을
파싱하는 방식보다 안정성이 보장된다 — 메시지 누락, 레이아웃 깨짐, 타이밍
race 없음.

### 원격 승인

Claude 가 Bash, Edit 같은 도구를 사용하려 할 때, Telegram 에
**Allow / Deny 버튼**이 뜬다. 한 번 탭하면 Claude 에 즉시 전달. 민감한
작업에 대해선 Normal 모드로 운영하면서 모든 호출을 세밀히 컨트롤 가능.

### 이미지 / 파일 첨부

Telegram 에서 보낸 이미지·파일은 로컬에 저장되고, Claude 가 경로를
받아 `Read` 또는 이미지 도구로 분석한다. 스샷 첨부해서 "이 UI 버그
고쳐줘" 같은 지시가 바로 된다.

### 재기동 안전

dispatcher 재기동 시 기존 세션은 **정상 종료** — 각 Claude 에 `/exit`
를 보내 `~/.claude/projects` 세션 JSONL 을 저장하고, 그 뒤 tmux 를
정리한다. 재기동 후 `/resume` 으로 이어가면 된다. 비정상 종료
(SIGKILL, 전원 차단 등) 로 남은 orphan tmux 는 다음 startup 시
자동으로 청소된다.

IPC 연결이 끊겨도 **자동 재연결 (2s → 5s → 10s 백오프, 최대 3회)** 을
시도하고, 복구되면 🔄 알림이 Telegram 에 뜬다. 3회 모두 실패 시 해당
세션은 수동 `/new` 또는 `/resume` 으로 복구.

### 여러 인스턴스 동시 운영

환경변수 `CB_HOME` 으로 전체 런타임 루트를 옮길 수 있다:

```bash
CB_HOME=~/cb-work    ./bin/start.sh   # 업무용 (봇 토큰 A)
CB_HOME=~/cb-private ./bin/start.sh   # 개인용 (봇 토큰 B)
```

각 인스턴스는 독립된 config, 소켓, 로그, registry, workspace 를 갖는다.
tmux 세션 이름도 `cb-<instance>-s1` 형태로 구분되어 섞이지 않는다.

### 진단·관찰성 내장

- `~/.claude-bridge/anomaly.jsonl` — **always-on JSONL** 이상 신호 로거
  (세션 스폰, IPC 연결, rate limit, reaction 실패 등 모든 상태 전환 기록)
- `./bin/capture.sh` — dispatcher + tmux pane + anomaly 스냅샷 한 번에
- `./bin/bugreporter.sh` — 재현 즉시 한 줄로 증거 수집 → GitHub Issue 에
  그대로 붙여넣기 가능
- `./bin/repairer.sh` — Claude 가 bugreport 를 읽고 직접 소스 수정

---

## 구조

```
src/
  core/                          # channel-agnostic infrastructure
    paths.ts                     # CB_HOME + all runtime paths
    dispatcher-core.ts           # spawn / kill / handleSlash (pure funcs)
    dispatcher-handlers.ts       # IPC hello / permission / inbound handlers
    SessionManager.ts            # spawn / resume / fork / gracefulKill
    TickObserver.ts              # periodic pane signal observer
    registry.ts                  # active session tracking + persistence
    sessions.ts                  # ~/.claude/projects scanner (resume)
    slash.ts                     # command parser + BOT_COMMANDS
    ipc.ts                       # dispatcher <-> MCP server protocol
    lifecycle.ts                 # PID lock / orphan watchdog / shutdown
    observer.ts                  # pane status regexes (busy / idle)
    anomaly.ts                   # always-on JSONL logger + rotation
    channel-prompt.ts            # channel instructions + mcp.json writer
    pin.ts                       # active session pin helper
    tmux/session.ts              # tmux wrapper (new-session / send-keys)
  channels/
    telegram/                    # Telegram-specific implementation
      server.ts                  # MCP stdio server
      IpcBridge.ts               # MCP ↔ dispatcher socket (auto-reconnect)
      SlashHandler.ts            # slash command handler w/ inline keyboards
      tools/                     # reply / react / edit_message / download
      client.ts                  # grammy wrapper
      poller.ts                  # long-polling + permission callback
      access.ts                  # allowlist gate
      permissions.ts             # permission request InlineKeyboard
      inbox.ts                   # attachment storage
      config.ts                  # channel config + chmod
  dispatcher.ts                  # main entry (wiring + animation state machine)
tests/                           # Bun test suite (177 cases)
```

신규 채널 (Slack, Discord 등) 추가 시 `src/channels/<name>/` 을 하나 더
만들면 된다. `src/core/` 는 채널 무관성을 유지한다.

---

## 요구사항

- macOS / Linux
- [Bun](https://bun.sh) 1.2+
- tmux
- Claude Code CLI (`claude` 커맨드가 PATH 에)
- Telegram Bot Token

## 설정

`./bin/setup.sh` 가 대화형으로 진행한다. 결과는
`~/.claude-bridge/config.json` 에 저장:

```json
{
  "botToken": "123:ABC...",
  "allowlist": ["YOUR_TELEGRAM_USER_ID"],
  "defaultChatId": "YOUR_TELEGRAM_USER_ID"
}
```

디렉토리 권한은 자동으로 `0700` / `0600` 으로 강화된다. `tg_channel`
MCP 서버는 `claude mcp add -s user` 로 user scope 에 등록되므로 어떤
cwd 에서 spawn 해도 동작한다.

## 실행

```bash
./bin/start.sh                # 백그라운드 기동
./bin/start.sh --fg           # 전경 (Ctrl+C 로 종료)
./bin/stop.sh                 # dispatcher 종료 (graceful /exit → 5s → kill)
./bin/stop.sh --all           # dispatcher + 모든 cb-* tmux 정리
./bin/restart.sh              # stop → start
```

기동 시 Telegram 봇 메뉴 (`/`) 가 자동 등록된다.

## 명령어

| 커맨드 | 동작 |
|---|---|
| `/sessions` | 활성 세션 목록 + 탭해서 **전환** |
| `/new [label] [cwd]` | 새 Claude 세션 spawn |
| `/resume` | 과거 Claude Code 세션 복원 (picker) |
| `/fork` | 기존 세션 컨텍스트 상속한 분기 세션 (picker) |
| `/kill` | 세션 종료 (picker) |

## 문제 해결 도구

문제가 생겼을 때를 위한 세 개의 도구가 포함되어 있다.

### 한 줄로 증거 수집 — `bugreporter.sh`

```bash
./bin/bugreporter.sh
```

재현 직후 실행하면 타임라인, anomaly 로그, dispatcher 로그, 소스 스냅샷,
설정(토큰 마스킹) 을 `bugreport/<타임스탬프>/` 에 모은다. 수집된 내용을
**그대로 GitHub Issue 에 붙여넣을 수 있어**, 재현 환경을 일일이 설명하지
않아도 된다. 내부적으로 Claude 대화형 세션이 열려 타임라인 분석과 원인
가설을 `BUG_REPORT.md` 초안으로 작성한다.

### 상세 스냅샷 — `capture.sh`

```bash
./bin/capture.sh                           # dispatcher 상태 + 세션 목록 + anomaly
./bin/capture.sh --session cb-s1           # 특정 세션 tmux 패널 last 500
./bin/capture.sh --follow --session cb-s1  # read-only 실시간 관찰
```

### 그 자리에서 자가 수리 — `repairer.sh`

```bash
./bin/repairer.sh                             # 가장 최근 bugreport
./bin/repairer.sh bugreport/YYYYMMDD_HHMMSS   # 특정 리포트
```

bugreporter 가 만든 리포트를 읽고 **Claude 가 직접 소스 파일을 수정**한다.
수리 범위를 먼저 요약해 승인을 요청하고, 승인 후 Edit 을 적용하고 테스트를
돌린다. 커밋은 자동으로 하지 않으며 사용자가 직접 확인 후 수행한다.

---

## 설계 문서

- 아키텍처·프로토콜·세션 생명주기·슬래시 커맨드·접근 제어·관찰성:
  [`docs/design/`](docs/design/README.md)

## 라이선스 & Attribution

MIT License — 자세한 내용은 [LICENSE](LICENSE) 참조.

MCP 채널 프로토콜 표면과 라이프사이클 패턴(PID lock / orphan watchdog 등)
은 [anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official)
의 Telegram 플러그인을 레퍼런스로 삼아 독립 구현했다. 코드 사본은 포함되지
않음.
