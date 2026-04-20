# claude-bridge

**텔레그램에서 내 PC 의 Claude Code 를 원격 조작**하는 MCP 채널 호스트.
외출 중에도, 자기 전 침대에서도 진행 중인 개발 세션을 이어갈 수 있다.

Bun + TypeScript. Claude Code 가 공개한 `claude/channel` MCP 프로토콜 위에서
동작 — Claude 가 `reply()` / `react()` / `edit_message()` /
`download_attachment()` 툴을 **직접** 호출해 대화한다. 터미널 에뮬레이션을
사이에 끼워넣지 않는다.

---

## 왜 이걸 쓰나

### 1. 터미널 스크래핑이 아니라 프로토콜이다

기존 Telegram ↔ Claude 브리지는 대부분 tmux `pipe-pane` 이나 `capture-pane`
출력을 regex 로 긁어서 채팅에 보낸다. 터미널은 시각적 표시용 버퍼라 구조가
없고, 래핑·ANSI 시퀀스·커서 이동·중간 재렌더링이 섞여 돌아간다. 이 위에서
"지금 Claude 가 뭘 했는지" 를 추론하려면 휴리스틱이 계속 늘어나고, 한 곳을
고치면 다른 곳이 깨지는 구조적 취약함이 쌓인다.

본 저장소는 그 레이어를 폐기하고 Claude Code 가 직접 제공하는 MCP 채널
프로토콜을 사용한다. Claude 가 "답장 할 차례" 라고 판단했을 때 정확히
`reply()` 툴을 호출한다. 스트리밍 타이밍, 래핑 복원, busy 감지 오탐, 앵커
stranding, pre-busy flush 같은 고질적 이슈가 **원천적으로 존재하지 않는다.**

### 2. 멀티 세션이 1급 시민이다

`/new`, `/fork`, `/switch`, `/kill`, `/sessions` 로 여러 Claude 세션을
동시에 띄우고 오간다. 각 세션은 자신의 tmux 세션, 자신의 MCP 서버, 자신의
workspace 디렉토리를 갖는다.
- 한 세션에서 리서치, 다른 세션에서 구현, 또 다른 세션에서 PR 코멘트 응답을
  병렬로 돌릴 수 있다.
- `/fork` 는 기존 세션의 컨텍스트를 이어받은 새 세션을 만든다 — 실험적
  방향을 원본을 망가뜨리지 않고 시도할 수 있다.

### 3. workspace 가 격리돼 있다

봇이 띄우는 세션의 cwd 는 `~/.claude-bridge/workspaces/<label>/` 하위로
분리된다. 사용자가 평소 쓰는 프로젝트 디렉토리와 섞이지 않아서 `claude
--resume` picker 가 봇 세션으로 채워지는 일이 없다. 원격 작업이 로컬 작업의
흐름을 방해하지 않는다.

### 4. 관찰 가능한 실패

**항상 켜진** JSONL anomaly 로그 (`~/.claude-bridge/anomaly.jsonl`) 가
shutdown, stale_instance_evicted, orphan_detected, socket 에러, MCP 호출
실패, tmux 상태 이상 등을 타임스탬프와 함께 기록한다. "뭔가 이상한데
재현이 안 돼요" 를 방지한다 — dump 를 켜두지 않았어도 이벤트는 남는다.

### 5. 봇 재기동과 독립된 Claude 세션

봇이 죽거나 배포 재기동돼도 tmux 세션과 Claude 프로세스는 살아있다.
재기동 시 `lifecycle.ts` 의 PID lock 이 중복 실행을 막고, orphan watchdog
이 버려진 세션을 감지·정리한다. Telegram polling 은 exponential backoff
로 자동 재연결.

### 6. 구조적 · 명시적 UX

- 슬래시 커맨드 (`/new`, `/resume`, `/fork`, `/switch`, `/kill`,
  `/sessions`, `/current`, `/backlog`, `/status`) 는 Telegram `setMyCommands`
  로 봇 메뉴에 자동 등록된다. 자연어 해석 없음.
- 승인 프롬프트는 InlineKeyboard. 버튼 클릭 → `permission_request` IPC →
  Claude 에 정확한 허용/거부 신호.
- 긴 응답은 청크로 잘라 `[1/3]`, `[2/3]` 로 보낸다.

### 7. 보안

- `config.json` 의 allowlist 에 있는 user/chat id 만 접근. 봇 발견자가
  임의로 등록할 수 없다.
- `~/.claude-bridge/` 디렉토리 0700, 파일 0600 으로 자동 강화.
- Token 충돌 감지: 같은 bot token 으로 다른 머신에서 동시에 폴링이
  시작되면 즉시 탐지해 경고.

---

## 구조

```
src/
  core/                          # channel-agnostic infrastructure
    anomaly.ts                   # always-on JSONL logger
    dispatcher-core.ts           # spawn / kill / handleSlash (pure funcs)
    ipc.ts                       # dispatcher <-> server socket protocol
    lifecycle.ts                 # PID lock / orphan watchdog / shutdown
    observer.ts                  # pane status (busy / compact / limit)
    registry.ts                  # active session registry
    sessions.ts                  # ~/.claude/projects scanner (resume)
    slash.ts                     # command parser + BOT_COMMANDS
    tmux/session.ts              # tmux new-session / send-keys / capture
  channels/
    telegram/                    # Telegram-specific implementation
      server.ts                  # MCP stdio server (reply/react/edit/dl)
      client.ts                  # grammy wrapper
      poller.ts                  # long-polling + permission callback
      access.ts                  # allowlist gate
      permissions.ts             # InlineKeyboard UI
      inbox.ts                   # attachment storage
      config.ts                  # channel config load + chmod
  dispatcher.ts                  # main entry (core + channel wiring)
tests/                           # Bun test (62 cases, expanding)
```

신규 채널 (예: Slack, Discord) 추가 시 `src/channels/<name>/` 하나 더
만들면 되도록 core/channel 경계를 유지한다.

---

## 요구사항

- macOS / Linux
- [Bun](https://bun.sh) 1.2+
- tmux
- Claude Code CLI (`claude` 커맨드가 PATH 에)
- Telegram Bot Token ([@BotFather](https://t.me/botfather))

## 설치

```bash
git clone https://github.com/kunrunic/claude-bridge.git
cd claude-bridge
bun install
./bin/setup.sh          # 대화형 초기화 — 토큰 입력, chat_id 확인, config 생성
```

설정은 `~/.claude-bridge/config.json` 에 저장된다:

```json
{
  "botToken": "123:ABC...",
  "allowlist": ["YOUR_TELEGRAM_USER_ID"],
  "defaultChatId": "YOUR_TELEGRAM_USER_ID"
}
```

디렉토리/파일 권한은 기동 시 `0700` / `0600` 으로 자동 강화.

## 실행

```bash
./bin/start.sh                # 백그라운드 기동
./bin/start.sh --fg           # 전경 (Ctrl+C 로 종료)
./bin/stop.sh                 # 봇 종료 (tmux/Claude 는 살아있음)
./bin/stop.sh --all           # 봇 + 모든 tmux 세션 종료
./bin/restart.sh              # stop → start
```

기동 시 Telegram 봇 메뉴 (`/`) 가 자동 등록된다.

## 명령어

| 커맨드 | 동작 |
|---|---|
| `/new [label] [cwd]` | 새 Claude 세션 spawn (컨텍스트 0) |
| `/resume [N\|id]` | 최근 세션 나열 / 동일 session-id 로 이어 실행 |
| `/fork [N\|id]` | 기존 세션 컨텍스트 상속 + 새 session-id |
| `/sessions` | 활성 세션 목록 |
| `/switch <id\|label>` | 활성 세션 전환 |
| `/kill <id\|label>` | 세션 종료 |
| `/current` | 현재 활성 세션 |
| `/backlog [id\|label]` | 백그라운드 누락 메시지 열람 |
| `/status` | 24h anomaly 요약 |

## 개발

```bash
bun run typecheck              # tsc --noEmit (strict mode)
bun test                       # 단위 + 통합 테스트
bun tests/smoke-spawn.ts       # tmux spawn → IPC hello 왕복 확인
bun tests/smoke-reply.ts       # Claude → reply() → Telegram 왕복
bun tests/smoke-permission.ts  # permission_request 왕복
bun tests/smoke-mcp-stdio.ts   # MCP stdio 핸드셰이크
```

`.git/hooks/pre-commit` 이 변경 포함 시 typecheck + test 자동 실행.

## 문제 진단 & 수리

### 현재 상태 엿보기

```bash
./bin/capture.sh                      # 활성 세션 tmux 마지막 500 줄 + anomaly 최근
./bin/capture.sh --session <id>       # 특정 세션 지정
./bin/capture.sh --follow             # read-only attach (detach: Ctrl+b d)
```

### 버그 리포트

증상 재현 직후 증거 수집:

```bash
./bin/bugreporter.sh
```

`bugreport/YYYYMMDD_HHMMSS/` 에 tmux capture, anomaly 로그, IPC 덤프,
config(masked), 소스 스냅샷 수집 후 대화형 Claude 가 타임라인 복원 →
가설 → 재현 방법 → 제안 수정안 으로 `BUG_REPORT.md` 생성.

### 수리

```bash
./bin/repairer.sh                             # 가장 최근 bugreport
./bin/repairer.sh bugreport/YYYYMMDD_HHMMSS   # 특정 리포트
```

리포트를 로드해 Claude 가 범위 요약 → 사용자 승인 → Edit 적용 → 테스트
실행. 커밋은 수행하지 않음.

---

## 설계 문서

- 아키텍처·프로토콜·세션 생명주기·슬래시 커맨드·접근 제어·관찰성:
  [`docs/design/`](docs/design/README.md) (AS-IS 설계)
- 현재 전략: [`docs/plans/20260420-mcp-channel-adapter/`](docs/plans/20260420-mcp-channel-adapter/)

## 이력

Python pane-parsing 시대 구현은 2026-04-20 Stage 4 컷오버에서 전면 폐기.
이전 상태는 태그 [`pre-python-removal-20260420`](https://github.com/kunrunic/claude-bridge/tree/pre-python-removal-20260420)
으로 조회 가능.

## 라이선스 · Attribution

MIT License — 자세한 내용은 [LICENSE](LICENSE) 참조.

MCP 채널 프로토콜 표면과 라이프사이클 패턴 (PID lock / orphan watchdog 등) 은
[anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official)
의 Telegram 플러그인 (Apache 2.0) 을 레퍼런스로 삼아 독립 구현했다. 코드
사본은 포함되어 있지 않다.
