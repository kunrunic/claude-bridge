# 04. Slash 커맨드

Telegram 봇 명령어 목록, 파싱 로직, dispatcher 처리 경로.

## 커맨드 목록

**BOT_COMMANDS** (`slash.ts:60-70`)

| 명령어 | 설명 |
|--------|------|
| `/sessions` | 활성 세션 목록 표시 |
| `/new` | 새 Claude 세션 spawn — `/new [label] [cwd]` |
| `/resume` | 세션 목록 / 복원 (같은 ID) — `/resume [N\|id]` |
| `/fork` | 복원 (새 ID, 컨텍스트 상속) — `/fork [N\|id]` |
| `/switch` | 활성 세션 전환 — `/switch <id\|label>` |
| `/kill` | 세션 종료 — `/kill <id\|label>` |
| `/current` | 현재 활성 세션 표시 |
| `/backlog` | 메시지 backlog 조회 — `/backlog [id\|label]` |
| `/status` | 24h anomaly 요약 |

Dispatcher가 시작할 때 `tg.setMyCommands(slash.BOT_COMMANDS)` 호출 (`dispatcher.ts:364`)

## 파싱

**parse()** (`slash.ts:18-58`)

정규식: `/^\/([a-z]+)(?:\s+(.+))?$/` (`slash.ts:12`)

예:
- `/sessions` → `{ kind: "sessions" }`
- `/new` → `{ kind: "new" }`
- `/new myproject` → `{ kind: "new", label: "myproject" }`
- `/new myproject /path/to/cwd` → `{ kind: "new", label: "myproject", cwd: "/path/to/cwd" }`
- `/switch s1` → `{ kind: "switch", target: "s1" }`
- `/resume 1` → `{ kind: "resume", target: "1" }`
- `/fork abc12` → `{ kind: "fork", target: "abc12" }`

**경로 vs 라벨 판별** (`slash.ts:14-16, 31-33`)
- 인자가 `/` 또는 `~` 로 시작 → cwd (경로)
- 아니면 → label

**인수 파싱**
- `/new label cwd` — 2개 이상 인수 시, 첫 번째는 label, 나머지를 joined cwd
- `/new /path/to/cwd` — 단일 경로 → cwd만, label 없음
- `/new myproject` — 단일 비경로 → label만

## Dispatcher 처리

**flow**: Telegram 메시지 → slash.parse() → handleSlash() → 적절한 핸들러

**위치** (`dispatcher.ts:308-313`)
```typescript
const slashCmd = slash.parse(evt.content);
if (slashCmd) {
  const reply = handleSlash(slashCmd, evt.meta.chat_id);
  void tg.sendMessage(evt.meta.chat_id, reply).catch(() => {});
  return;
}
```

**handleSlash()** (`dispatcher-core.ts:99-168`)

순수 함수. Dependency injection으로 동작:
- `registry` — 세션 추적
- `spawn` — `/new` 처리
- `resume` — `/resume`, `/fork` 처리
- `listRecent` — 최근 세션 목록
- `kill` — `/kill` 처리
- `renderStatus` — `/status` 응답 생성

## 커맨드별 처리

### `/sessions`
현재 메모리의 모든 세션 표시.

**출력**
```
▶ s1 (myproject) — idle/idle
  s2 (other) — busy/busy
```

▶ = 활성 세션
상태: {state}/{signal}

**코드** (`dispatcher-core.ts:104-113`)

### `/new [label] [cwd]`
새 세션 spawn.

**처리** (`dispatcher-core.ts:115-124`)
1. opts 구성 (label, cwd)
2. deps.spawn(opts) 호출 → spawnSession()
3. "spawned {id} ({label})" 응답

**에러**: spawn 실패 시 예외 catch, 에러 메시지 반환

### `/resume [N|id]`
최근 세션 목록 / 복원.

**인수 없음** → 최근 8개 세션 목록 표시 (`dispatcher-core.ts:127`)
```
Recent Claude sessions:
1. [04/20 10:30] myproj · First user message
2. [04/20 09:15] other · Another message
```

**인수** (숫자 또는 ID) → `resumePicked(target, false)` 호출 (`dispatcher-core.ts:128`)

**에러**
- 인덱스 범위 초과: "pick index out of range..."
- 세션 없음: "no such session..."
- cwd 없음: "session cwd missing on disk..."

### `/fork [N|id]`
새 session_id로 복원 (컨텍스트 상속).

`resumePicked(target, true)` 호출 (`dispatcher-core.ts:131-132`)

차이점: spawnSession 옵션에 `forkSession: true` 추가 (`dispatcher.ts:264`)

### `/switch <id|label>`
활성 세션 변경.

**처리** (`dispatcher-core.ts:134-143`)
1. Registry.get() 또는 getByLabel()
2. Registry.setActive(s.id)
3. "▶ switched to {label}" + backlog 갯수 표시

### `/kill <id|label>`
세션 종료.

**처리** (`dispatcher-core.ts:145-148`)
1. deps.kill(target) 호출 → killSession()
2. 성공: "killed {target}"
3. 실패: "no such session..."

### `/current`
현재 활성 세션 표시.

**처리** (`dispatcher-core.ts:150-152`)
- "active: {label}"
- 활성 세션 없으면: "no active session"

### `/backlog [id|label]`
최근 20개 메시지 backlog 조회.

**처리** (`dispatcher-core.ts:154-162`)
1. 인수 있으면 해당 세션, 없으면 활성 세션
2. backlog 출력 (최근 20개)
3. 비어있으면: "(label) no backlog"

**형식**
```
(myproject) backlog:
← /status
← hello world
```

### `/status`
24시간 anomaly 요약.

**처리** (`dispatcher.ts:286-302`)
1. `anomaly.summary(WINDOW_MS)` — 24h 윈도우
2. anomaly 없으면: "📊 24h anomalies: none"
3. 있으면: kind별 카운트 + 마지막 발생 시각
4. token_collision_detected → ⚠️ 배지

**예**
```
📊 24h anomalies: 5
  channel_reply_failed: 2  (last 10:30:45)
  session_spawn_failed: 1  (last 09:15:22)
⚠️ token_collision_detected: 2  (last 08:45:10)
log: /Users/user/.claude-bridge/anomaly.jsonl

help:
Commands:
  /sessions            list all sessions
  ...
```

Telegram command hint 도 함께 표시 (`slash.help()`)

## help()

`slash.help()` — 모든 커맨드 요약. `/status` 응답의 마지막에 포함 (`slash.ts:72-85`)

## Known Fragility

- 매우 긴 session label (50자+)이 있으면 Telegram UI에서 잘릴 수 있음 (하지만 기능은 정상)
- 동시에 같은 session_id를 resume 하려는 경우 race condition 가능 (권장 아님)

## 참고

- 파싱 및 타입: `src/core/slash.ts`
- 처리 함수: `src/core/dispatcher-core.ts`
- 디스패처: `src/dispatcher.ts`
