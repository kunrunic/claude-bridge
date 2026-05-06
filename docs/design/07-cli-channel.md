# 07. cb CLI · cb-menu · tmux status

세 가지 컴포넌트가 한 흐름에 묶여 있어 같은 문서에서 다룬다:

- **cb 클라이언트** (`src/channels/cli/cli.ts`) — 사용자 머신의 SSH wrapper. 호스트 등록, 접속, 원격 dispatcher 제어
- **cb-menu** (`src/channels/cli/menu/`) — 호스트의 영속 Ink TUI. 세션 picker / new / resume
- **TmuxStatusChannel** (`src/channels/tmux/`) — 각 cb-* tmux 세션의 status bar 에 minimap 출력

모두 [Channel](08-channel-abstraction.md) 추상화 또는 그 RPC 위에 동작한다.

---

## 토폴로지

```
[client machine]                                     [host machine]

$ cb add home
   └─> ~/.cb/hosts.json + ~/.ssh/config 동기화

$ cb home
   └─> ssh -t home 'tmux attach -t cb-menu'
                                                     dispatcher (always-on)
                                                       │
                                                       │ spawn cb-menu (start 시점)
                                                       ▼
                                                     cb-menu tmux session
                                                       │
                                          ┌──── attach ─────┐
                                          │                  │
                                       cb-menu Ink TUI       │
                                       (sessions / new /     │ cli_request RPC
                                        resume modes)        │ (UDS dispatcher.sock)
                                          │                  ▼
                                          │             dispatcher
                                          │             (registry, spawn,
                                          │              kill, resume...)
                                          │
                              Enter / 1-9 │ switch-client
                                          ▼
                                       cb-s1, cb-s2, ... (Claude tmux sessions)
                                          │
                                          │ status-right minimap
                                          │ (TmuxStatusChannel set-option)
                                          └─ ▶s1·my-app  s2⠋backend  s3⚠util
```

---

## cb 클라이언트 (`src/channels/cli/cli.ts`)

사용자가 자기 노트북에서 호출하는 CLI. 본질적으로 SSH wrapper + 호스트 영속.

### 명령어

| 명령 | 동작 | 코드 |
|---|---|---|
| `cb` 또는 `cb list` | 등록 호스트 표 출력 + 도움말 | `cmdList` (cli.ts:239) |
| `cb add [name]` | 대화형/플래그 호스트 등록 | `cmdAdd` (cli.ts:125) |
| `cb <name>` | SSH 접속 + cb-menu attach | `cmdConnect` (cli.ts:73) |
| `cb remove <name>` | 호스트 등록 해제 | `cmdRemove` (cli.ts:272) |
| `cb start \| stop \| restart <name>` | 원격 dispatcher 제어 | `cmdDispatcherControl` (cli.ts:201) |

`cb <name>` 의 핵심은 다음 한 줄:

```bash
ssh -e none -o StrictHostKeyChecking=accept-new -t <ssh-args> '<REMOTE_ENTRY_CMD>'
```

`REMOTE_ENTRY_CMD` (`cli.ts:54`) 는 호스트에서 실행될 한 줄짜리 셸 스크립트:

```sh
PATH="$HOME/.local/bin:/opt/homebrew/bin:$HOME/.bun/bin:$PATH";
MENU_NAME="cb${CB_INSTANCE:+-$CB_INSTANCE}-menu";
command -v tmux >/dev/null 2>&1 || { echo "tmux 가 호스트에 설치돼 있지 않습니다." >&2; exit 1; };
tmux has-session -t "=$MENU_NAME" 2>/dev/null || { echo "cb-menu 세션($MENU_NAME) 미가동 ..." >&2; exit 1; };
tmux set -g extended-keys on 2>/dev/null || true;
exec tmux attach-session -t "=$MENU_NAME"
```

설계 메모:
- `-e none` — SSH escape character 비활성. tmux prefix `Ctrl-b` 가 SSH 클라이언트에서 가로채지지 않게
- `-t` — force tty (Ink TUI 가 interactive pty 필요)
- `=` prefix — exact-match (cb-menu 가 cb-menu1 같이 prefix 매칭으로 잘못 잡히지 않게)
- 별도 cb-tui 프로세스 spawn 없음 — 직접 tmux attach 라 stdin 인계 버그 회피

### 호스트 영속 (`hosts.ts`)

`~/.cb/hosts.json` 이 source of truth:

```json
{
  "hosts": {
    "home": {
      "host": "home",
      "user": "alice",
      "key": "~/.ssh/id_ed25519"
    }
  }
}
```

`cb add` / `cb remove` 는 두 곳을 동시에 갱신:
1. `~/.cb/hosts.json` — 자체 config
2. `~/.ssh/config` — `cb-` 라벨 블록으로 등록

`~/.ssh/config` 동기화 덕분에 `ssh home` 직접 호출도 동작 (cb 안 거쳐도 일반 SSH 도구로 접근 가능).

### 원격 dispatcher 제어

`cb start home` 등은 SSH 로 호스트의 claude-bridge 디렉토리를 자동 탐색 (`~/claude-bridge`, `~/claude-bridge2`, `~/claude-bridge3`) 후 `bash bin/start.sh` 실행:

```sh
PATH="$HOME/.bun/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH";
DIR="";
for d in ~/claude-bridge ~/claude-bridge2 ~/claude-bridge3; do
  [ -f "$d/bin/start.sh" ] && DIR="$d" && break;
done;
[ -z "$DIR" ] && { echo "✗ claude-bridge 디렉터리를 찾을 수 없음" >&2; exit 1; };
cd "$DIR";
bash bin/${action}.sh
```

---

## cb-menu (`src/channels/cli/menu/`)

호스트에서 dispatcher 가 spawn 하는 영속 Ink TUI. tmux 세션 이름은 `cb-menu` (또는 `cb-<inst>-menu`).

### 세 가지 모드 (`App.tsx`)

```typescript
type Mode = "sessions" | "new" | "resume";
```

| 모드 | 컴포넌트 | 입력 |
|---|---|---|
| sessions (기본) | `SessionList` | ↑↓/jk · Enter/1-9 · n / r / x / q |
| new | `DirBrowser` | ↑↓ · Enter (하위 진입) · 글자 (필터) · Esc |
| resume | `ResumePicker` | type-ahead 검색, ↑↓ · Enter (resume) · Ctrl-R · Esc |

`list_sessions` 는 1.5초 polling (`POLL_MS = 1500`). SessionEvent 구독으로 push 변환은 향후 작업.

### dispatcher RPC (`menu/rpc.ts`)

cb-menu 는 매 액션마다 짧은 UDS 연결을 연다:

```typescript
const SOCKET_PATH = process.env.CB_DISPATCHER_SOCKET ?? DEFAULT_SOCKET_PATH;
// connect → cli_request → cli_response → close
```

요청 종류 (`core/ipc.ts`):

| command | 동작 |
|---|---|
| `list_sessions` | 활성 세션 목록 + activeId 반환 |
| `spawn` | new mode 에서 세션 생성 |
| `kill` | 세션 종료 |
| `list_recent` | resume picker 의 `~/.claude/projects` 스캔 결과 |
| `resume` | 선택 ID 로 resume (메타 유지, 새 session_id) |
| `clear_active` | active 해제 |

응답은 `{ kind, ... }` discriminated union. 10초 timeout.

### F-key 단축키 (`menu/keybindings.ts`)

cb-menu mount 직전 1회 셋업. tmux server-wide root 테이블에 등록.

| 키 | tmux 명령 | 의미 |
|---|---|---|
| F1 | `switch-client -t =cb-menu` | sessions 모드로 |
| F2 | `switch-client -t =cb-menu ; send-keys -t =cb-menu n` | new 모드 자동 진입 |
| F3 | `switch-client -p` | 이전 세션 |
| F4 | `switch-client -n` | 다음 세션 |
| F5 | `switch-client -t =cb-menu` | 메뉴로 (Claude 백그라운드 유지) |
| F6 | `run-shell <handoff>` | 현재 Claude 세션을 Telegram active 로 + 메뉴 복귀 |

F6 의 `<handoff>` 는 다음 셸 명령:
```sh
SESS=$(tmux show-environment -t '#S' CB_SESSION_ID 2>/dev/null | cut -d= -f2);
[ -z "$SESS" ] && exit 0;
printf '{"op":"set_active_request","session_id":"%s"}\n' "$SESS" \
  | nc -U -w 1 '<dispatcher-socket>' >/dev/null 2>&1;
tmux switch-client -t '=cb-menu'
```

dispatcher 의 `set_active_request` IPC op 가 active session 을 전환 → Telegram pin 재갱신 + announce.

### Spinner 일관성 (`menu/spinner.ts`)

cb-menu 의 minimap 과 SessionList 의 sigil, TmuxStatusChannel 의 minimap 모두 같은 `SPINNER_FRAMES` 를 공유:

```typescript
export const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
```

서로 다른 채널의 minimap 이라도 같은 frame index 로 맞물려 보임 (호출 주기는 다르지만 frame set 이 같음).

---

## TmuxStatusChannel (`src/channels/tmux/`)

minimap 출력 전용 채널. 입력 없음 (notify / announce 만 받음).

### 책임

각 cb-* tmux 세션과 cb-menu tmux 세션의 status bar 에 minimap 문자열을 set-option 으로 갱신:

```
status-right: ▶s1·my-app  s2⠋backend  s3⚠util  +2
```

### `render.ts` — pure renderer

`renderMinimap(sessions, viewerSessionId, spin)` 는 순수 함수. 테스트 가능 (`tests/tmux-render.test.ts`).

```typescript
const SIGIL = {
  perm: "⚠",
  compact: "□",
  rate_limit: "⏸",
  dead: "✗",
  error: "!",
  idle: "·",
} as const;
```

규칙:
- 각 세션마다 `{prefix}{sid}{sigil}{label}` 한 토큰
- `prefix` = viewer 본인이면 `▶`, 아니면 공백 (각 tmux 세션이 자기 자신을 ▶ 로 표시)
- busy / spawning / trust_prompt / resume_picker → SPINNER_FRAMES 중 하나
- 6개 초과 시 `+N` 으로 잔여 카운트
- label 8자 초과 시 7자 + `…`
- dead 세션은 minimap 에 안 보임 (cb-menu SessionList 와 일관)

### 갱신 트리거

`channel.ts` 의 TmuxStatusChannel.notify 가 SessionEvent 수신 시:
- 각 cb-* 세션과 cb-menu 에 대해 `tmux/status.ts` 의 `setStatusRight()` 호출
- spinner 가 의미 있는 signal (busy 등) 이면 1초 주기 frame 회전 timer 시작
- 모든 세션이 idle 이면 timer 정지

### tmux/status.ts

set-option 래퍼 모음:
- `setStatusRight(name, text)` — `#` 를 `##` 로 escape (tmux format string 변수 시작 문자)
- `setStatusRightLength(name, length)` — 기본 40 → minimap 용으로 충분히 늘림
- `setStatusPosition(name, "top")` — Claude TUI 가 하단 status bar 를 그리므로 top 에 배치 (충돌 회피)
- `setStatusStyle(name, style)` — `bg=black,fg=white` (tmux default green 은 minimap 색과 너무 비슷)

---

## 데이터 흐름 예: `cb home` → 새 세션

1. **사용자**: 클라이언트에서 `cb home`
2. **cli.ts**: `~/.cb/hosts.json` 에서 home 엔트리 조회 → ssh 명령 조립
3. **ssh 호스트 진입**: 호스트의 zsh 가 `REMOTE_ENTRY_CMD` 실행 → tmux attach
4. **cb-menu attach 됨**: Ink TUI 가 `list_sessions` RPC 1.5초마다 polling
5. **사용자**: `n` 키 → new 모드 → DirBrowser 에서 디렉토리 선택 → Enter
6. **menu/rpc.ts**: `{ op: "cli_request", command: "spawn", cwd: "..." }` 전송
7. **dispatcher**: spawnSession 호출 → 새 cb-s1 tmux 세션 + Claude TUI + MCP server (Telegram 모드)
8. **dispatcher → 모든 채널**: `notify({ type: "spawned", ... })`
   - TelegramChannel: announce "🟢 spawned [s1] my-app"
   - cb-menu: 다음 polling 에서 새 세션 리스트에 등장
   - TmuxStatusChannel: minimap 갱신 (`▶s1⠋my-app`)
9. **사용자**: cb-menu 에서 새 세션 선택 → Enter → tmux switch-client → Claude TUI 진입
10. **사용자**: F5 누르면 cb-menu 복귀, F6 누르면 이 세션을 Telegram active 로 핸드오프

---

## Known Fragility

- cb-menu polling 1.5s — 응답성과 부담의 trade-off. 빠른 키 입력 직후 1초 정도 지연 보일 수 있음
- F-key 가 macOS 시스템 키와 충돌 — 사용자가 시스템 설정에서 표준 기능 키 활성화 또는 fn+Fx 필요
- 인스턴스별 cb-menu 이름 (`cb-<inst>-menu`) — `CB_INSTANCE` 가 클라이언트와 호스트에서 일치해야 정확히 attach 됨

---

## 참고

- 채널 추상화: [08-channel-abstraction.md](08-channel-abstraction.md)
- 세션 라이프사이클: [03-session-lifecycle.md](03-session-lifecycle.md)
- 사용자 가이드: [docs/cli.md](../cli.md)
