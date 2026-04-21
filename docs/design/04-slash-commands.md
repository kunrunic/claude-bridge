# 04. Slash 커맨드

Telegram 봇 명령어 목록, 파싱 로직, dispatcher 처리 경로.

## 커맨드 목록

**BOT_COMMANDS** (`slash.ts`)

| 명령어 | 설명 |
|--------|------|
| `/sessions` | 세션 목록 / 탭해서 전환 |
| `/new` | 새 Claude 세션 spawn — `/new [label] [cwd]` |
| `/resume` | 세션 복원 (같은 ID) — `/resume [N\|id]` |
| `/fork` | 복원 (새 ID, 컨텍스트 상속) — `/fork [N\|id]` |
| `/kill` | 세션 종료 — `/kill <id\|label>` 또는 picker |

Dispatcher 시작 시 `tg.setCommands(slash.BOT_COMMANDS)` 호출.

`/backlog` 는 BOT_COMMANDS 에 없지만 parser 는 인식 (수동 입력 지원).

## 파싱

**parse()** (`src/core/slash.ts`)

정규식: `/^\/([a-z]+)(?:\s+(.+))?$/`

예:
- `/sessions` → `{ kind: "sessions" }`
- `/new` → `{ kind: "new" }`
- `/new myproject` → `{ kind: "new", label: "myproject" }`
- `/new myproject /path/to/cwd` → `{ kind: "new", label: "myproject", cwd: "/path/to/cwd" }`
- `/resume 1` → `{ kind: "resume", target: "1" }`
- `/fork abc12` → `{ kind: "fork", target: "abc12" }`
- `/kill s1` → `{ kind: "kill", target: "s1" }`
- `/backlog` → `{ kind: "backlog" }`

**경로 vs 라벨 판별**
- 인자가 `/` 또는 `~` 로 시작 → cwd (경로)
- 아니면 → label

**인수 파싱**
- `/new label cwd` — 2개 이상 인수 시, 첫 번째는 label, 나머지를 joined cwd
- `/new /path/to/cwd` — 단일 경로 → cwd만, label 없음
- `/new myproject` — 단일 비경로 → label만

## Dispatcher 처리

**flow**: Telegram 메시지 → slash.parse() → SlashHandler.handle() → 경우에 따라 인라인 키보드 OR `core.handleSlash()`

**SlashHandler** (`src/channels/telegram/SlashHandler.ts`)
- `sessions` / `new` / `resume` / `fork` / `kill(no target)` → Telegram 인라인 키보드 출력 후 callback 대기
- `resume(target)` / `fork(target)` / `kill(target)` / `backlog` → `core.handleSlash()` 위임

**handleSlash()** (`src/core/dispatcher-core.ts`)

순수 함수. Dependency injection 으로 동작:
- `registry` — 세션 추적
- `spawn` — `/new` 처리
- `resume` — `/resume`, `/fork` 처리
- `listRecent` — 최근 세션 목록
- `kill` — `/kill` 처리

## 커맨드별 처리

### `/sessions`

인라인 키보드로 세션 목록 출력. 각 버튼 탭 시 `switch:<id>` callback 발화 →
`SlashHandler.onSessionAction("switch", ...)` 에서 `registry.setActive()` + pin 갱신.

**버튼 포맷** (`src/channels/telegram/SlashHandler.ts`)
```
▶  1 🟢 backend        ← 활성
   2 🔵 research       ← busy
   3 ⏸ rate-limited    ← rate limit
✖ cancel
```

### `/new`

인라인 키보드로 퍼미션 모드 선택 UI 표시.
- 🔒 권한 확인 포함 → `new_normal:`
- 🔴 권한 확인 스킵 → `new_skip:`
- ✖ cancel → `cancel:`

선택 시 `onSessionAction("new_normal"|"new_skip", ...)` → `sessions.spawn({ skipPermissions })`

### `/resume`

인수 없으면 `sessions.refreshPickerCache()` → 최근 세션 picker 표시 (최대 8개).
각 버튼 탭 시 `resume:<id>` → 퍼미션 모드 선택 (2단계) → `sessions.resume(target, false, skipPermissions)`.

인수 있으면 `core.handleSlash()` 로 바로 위임.

### `/fork`

`/resume` 과 동일 흐름. `sessions.resume(target, true, skipPermissions)` 호출 (fork=true).

### `/kill`

인수 없으면 세션 picker (🗑 prefix) 표시. 탭 시 `kill:<id>` callback → `sessions.kill()`.
인수 있으면 `core.handleSlash()` 로 바로 위임.

### `/backlog [id|label]`

최근 20개 메시지 backlog 조회. 인수 없으면 활성 세션 대상.

**형식**
```
(myproject) backlog:
← /sessions
← hello world
```

## Known Fragility

- Telegram 인라인 키보드 버튼은 **proportional font** 로 렌더링 — 고정폭 정렬이 시각적으로 완벽하지 않음
- 매우 긴 session label (50자+) 은 fmtLabel 로 10자 + … 절삭 표시됨

## 참고

- 파싱 및 타입: `src/core/slash.ts`
- 순수 처리 함수: `src/core/dispatcher-core.ts`
- 인라인 키보드 UI + 세션 picker: `src/channels/telegram/SlashHandler.ts`
- 디스패처 조립: `src/dispatcher.ts`
