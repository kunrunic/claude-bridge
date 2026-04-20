# claude-bridge

**Telegram 에서 내 PC 의 Claude Code 를 원격 조작**하는 MCP 채널 호스트.
외출 중에도, 자기 전 침대에서도 진행 중인 개발 세션을 이어갈 수 있다.
Bun + TypeScript.

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

### 진행 중인 작업 이어가기

현재 Claude Code 로 작업하던 세션을 Telegram 봇을 통해 그대로 이어갈 수
있다.

- `/resume` 을 누르면 과거 세션 목록이 뜬다. 선택하면 해당 세션의
  **컨텍스트와 파일 히스토리가 그대로 복원**된다.
- Claude 가 원래 작업하던 디렉토리(cwd) 도 자동으로 맞춰진다.

### 여러 세션 동시 운영

`/new`, `/fork`, `/switch`, `/kill` 로 여러 Claude 세션을 동시에 띄우고
오간다. 각 세션은 자신의 tmux, 자신의 MCP 서버, 자신의 작업 디렉토리를
가지고 독립적으로 돌아간다.

- 한 세션에서 리서치, 다른 세션에서 구현, 또 다른 세션에서 PR 리뷰 —
  병렬 진행 가능.
- `/fork <id>` 는 기존 세션의 **컨텍스트를 상속받은 새 세션**을 만든다.
  실험적 방향을 원본을 건드리지 않고 시도하거나, 무거워진 세션에서 가벼운
  갈래로 탈출할 때 쓴다.

### 작업 폴더 지정

`/new [label] [cwd]` 로 원하는 디렉토리에서 새 세션을 시작.

- cwd 명시 → 그 디렉토리에서 Claude 실행. 진행 중이던 프로젝트에 바로 투입.
- 생략 → `~/.claude-bridge/workspaces/<label>/` 격리 공간에서 실행. 내 평소
  프로젝트 폴더가 봇 세션으로 섞이지 않는다.

### 안정적인 메시지 교환

Claude 가 네 가지 MCP 도구를 직접 호출해 Telegram 과 대화한다:

- `reply` — 메시지 전송
- `react` — 이모지 반응
- `edit_message` — 기존 메시지 수정
- `download_attachment` — 첨부 파일 다운로드

이 방식은 [anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official)
의 Telegram 플러그인 구현을 레퍼런스로 삼아 만들었다. Claude 가 "답할
차례"라고 판단했을 때 정확한 도구 호출로 응답하므로, 터미널 출력을 파싱하는
방식보다 안정성이 보장된다 — 메시지 누락, 레이아웃 깨짐, 타이밍 race 같은
이슈가 없다.

### 원격 승인

Claude 가 Bash, Edit 같은 도구를 사용하려 할 때, Telegram 에
**Allow / Deny 버튼**이 뜬다. 한 번 탭하면 Claude 에 즉시 전달된다.

### 이미지 / 파일 첨부

Telegram 에서 보낸 이미지나 파일은 로컬에 저장되고, Claude 가 경로를
받아 `Read` 또는 이미지 도구로 분석한다.

### 재기동 안전

봇이 재기동되면 dispatcher 는 기존 세션들을 **정상 종료**시킨다 — 각
Claude 에 `/exit` 를 보내 `~/.claude/projects` 세션 JSONL 을 저장하고,
그 뒤 tmux 를 정리한다. 재기동 후 `/resume` 으로 이어가면 된다. 비정상
종료(SIGKILL, 전원 차단 등) 로 남은 orphan tmux 는 다음 startup 시
자동으로 청소된다.

### 여러 인스턴스 동시 운영

환경변수 `CB_HOME` 으로 전체 런타임 루트를 옮길 수 있다:

```bash
CB_HOME=~/cb-work    ./bin/start.sh   # 업무용
CB_HOME=~/cb-private ./bin/start.sh   # 개인용
```

각 인스턴스는 독립된 config, 소켓, 로그, registry, workspace 를 갖는다.
Telegram bot token 만 서로 다르게 주면 동시에 폴링 가능.

---

## 구조

```
src/
  core/                          # channel-agnostic infrastructure
    paths.ts                     # CB_HOME + all runtime paths
    dispatcher-core.ts           # spawn / kill / handleSlash (pure funcs)
    registry.ts                  # active session tracking + persistence
    sessions.ts                  # ~/.claude/projects scanner (resume)
    slash.ts                     # command parser + BOT_COMMANDS
    ipc.ts                       # dispatcher <-> MCP server protocol
    lifecycle.ts                 # PID lock / orphan watchdog / shutdown
    observer.ts                  # pane status monitor (busy / idle)
    anomaly.ts                   # always-on JSONL logger + rotation
    tmux/session.ts              # tmux wrapper (new-session / send-keys)
  channels/
    telegram/                    # Telegram-specific implementation
      server.ts                  # MCP stdio server
      client.ts                  # grammy wrapper
      poller.ts                  # long-polling + permission callback
      access.ts                  # allowlist gate
      permissions.ts             # InlineKeyboard UI
      inbox.ts                   # attachment storage
      config.ts                  # channel config + chmod
  dispatcher.ts                  # main entry (core + channel wiring)
tests/                           # Bun test suite
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
| `/new [label] [cwd]` | 새 Claude 세션 spawn |
| `/resume [N\|id]` | 최근 세션 나열 / 동일 session-id 로 이어 실행 |
| `/fork [N\|id]` | 기존 세션 컨텍스트 상속 + 새 session-id |
| `/sessions` | 활성 세션 목록 |
| `/switch <id\|label>` | 활성 세션 전환 |
| `/kill <id\|label>` | 세션 종료 |
| `/current` | 현재 활성 세션 |
| `/backlog [id\|label]` | 백그라운드 누락 메시지 열람 |
| `/status` | 24h 문제 요약 |

## 개발

```bash
bun run typecheck              # tsc --noEmit (strict mode)
bun test                       # unit + integration (70 cases)
bun tests/smoke-spawn.ts       # tmux spawn → IPC hello 왕복
bun tests/smoke-reply.ts       # Claude → reply() → Telegram 왕복
bun tests/smoke-permission.ts  # permission_request 왕복
bun tests/smoke-mcp-stdio.ts   # MCP stdio handshake
```

`.git/hooks/pre-commit` 이 변경 포함 시 typecheck + test 를 자동 실행한다.

---

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
