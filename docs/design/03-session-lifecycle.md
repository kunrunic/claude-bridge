# 03. 세션 생명주기

세션 spawn, resume, fork, kill. Registry 추적, workspace 격리, PID lock, orphan watchdog.

## 세션 ID 및 라벨

**세션 ID**
- 형식: `s{N}` (N = 자동 증가 카운터)
- 유니크 식별자. Claude Code 세션 아이디와는 무관
- 예: `s1`, `s2`, `s3`, ...

**라벨**
- 사용자 친화적 이름 (기본값 = 세션 ID)
- `/new myproject` → 라벨 = "myproject"
- 라벨은 유니크하지 않음 (제약 없음)

**tmux 세션명**
- `cb-{sessionId}` 형식. 예: `cb-s1`, `cb-s2`
- tmux는 이름으로만 조작 (exact-match)

## Spawn (새 세션)

### 명령어
```
/new [label] [cwd]
```

예:
- `/new` → 라벨 없음, cwd = ~/.claude-bridge/workspaces/bot/
- `/new myproject` → 라벨 "myproject", cwd 기본값
- `/new myproject /Users/user/my-repo` → 라벨 "myproject", cwd = /Users/user/my-repo

### 코드 흐름

1. **Registry.create()** (registry.ts 에 구현)
   - 새 Session 생성
   - 자동 seq 증가: `s{++seq}`
   - tmuxName = `cb-s{N}`
   - state = "spawning", signal = "idle"
   - backlog = []

2. **spawnSession()** (dispatcher-core.ts:43-87)
   - Claude Code CLI 시작 명령어 구성
   - tmux.newSession() 호출:
     ```
     tmux new-session -d -s cb-s1 -c {cwd} \
       -e CB_DISPATCHER_SOCKET=/path/to/dispatcher.sock \
       -e CB_SESSION_ID=s1 \
       -e CB_POLL_DISABLED=1 \
       claude --disallowedTools ... --allowedTools ... \
       --channels server:tg_channel
     ```
   - 환경변수: CB_DISPATCHER_SOCKET, CB_SESSION_ID, CB_POLL_DISABLED
   - 플래그: --disallowedTools, --allowedTools (도구 화이트리스트), --channels (MCP 채널 등록)

3. **onSpawned 콜백** (dispatcher.ts)
   - confirmTrustDialog() — "Trust this folder?" 프롬프트 자동 확인 (polling)

4. **hello 메시지** (server.ts)
   - MCP server가 startup 후 dispatcher에 hello 메시지 전송
   - Registry.attachSocket(session_id, socket_id) — socket 등록

5. **Registry.updateState()** (dispatcher.ts)
   - state = "idle" (hello 수신 후)

### 실패 시 cleanup
세션 생성 실패 시 Registry에서 제거 (dispatcher-core.ts:80-82)

## Resume (기존 세션 복원)

### 명령어
```
/resume [N|id]
```

예:
- `/resume` → 최근 8개 세션 목록 표시
- `/resume 1` → 목록의 1번 항목 복원
- `/resume abc12` → 세션 ID `abc12` 로 시작하는 세션 복원

### 코드 흐름

1. **findSessions()** (sessions.ts 에 구현)
   - ~/.claude/projects/ 스캔 (디렉토리 목록)
   - 각 디렉토리의 *.jsonl 파일 (세션 기록) 파싱
   - JSONL 해석:
     - 첫 user 메시지 (의미 있는 텍스트, 15자 이상) → title
     - 마지막 user 메시지 → last
     - timestamp 필드의 최신값 → activity time
   - 최신순 정렬, 상위 N개 반환

2. **formatSessionList()** (sessions.ts 에 구현)
   - "1. [MM/DD HH:MM] project · title" 형식 표시

3. **resumePicked()** (dispatcher.ts 에 구현)
   - 인덱스 또는 ID로 세션 lookup
   - getSessionCwd(sessionId) — ~/.claude/projects 에서 cwd 복구
   - cwd 체크: 디스크에 존재하는가?
   - spawnSession() 호출, 옵션:
     - `resumeId: info.id` — Claude Code에 `--resume {id}` 플래그 전달
     - `label` — 기존 project 정보 사용 (선택사항)

4. **Claude Code 내부 처리** (`--resume` 플래그)
   - ~/.claude/projects/{project}/{session_id}.jsonl 로드
   - 대화 이력 복구
   - 같은 session_id 유지

### 폴더 신뢰 및 권한 자동 승인
resume 시에도 "Trust this folder?" 프롬프트 가능. dispatcher가 자동 확인함

## Fork (새 세션 ID로 resume)

### 명령어
```
/fork [N|id]
```

resume과 동일하되, Claude Code에 `--fork-session` 플래그 추가:
```
claude --resume {old_id} --fork-session ...
```

### 효과
- 새 session_id로 시작하지만, 기존 대화 컨텍스트는 유지
- 예: 기존 세션 s1 의 컨텍스트를 s2로 계속 진행

## Kill (세션 종료)

### 명령어
```
/kill <id|label>
```

예:
- `/kill s1`
- `/kill myproject`

### 코드 흐름

1. **Registry.get() / Registry.getByLabel()** (dispatcher-core.ts:96-97)
   - 세션 lookup

2. **tmux.killSession()** (dispatcher-core.ts:99)
   - `tmux kill-session -t =cb-s1`

3. **socket.close()** (dispatcher-core.ts:100)
   - IPC 소켓 종료 (active MCP server)

4. **Registry.remove()** (dispatcher-core.ts:101)
   - 메모리에서 제거
   - 활성 세션이라면, 다음 세션으로 전환

## 워크스페이스 격립

세션은 workspace 디렉토리에서 실행:
- 기본: `~/.claude-bridge/workspaces/bot/`
- 명시: `/new label /path/to/cwd` 시 `/path/to/cwd` 사용

**용도**
- Claude Code 세션 데이터 분리
- 사용자 `/resume` 와 봇 spawn 세션 격리
- ~/.claude/projects 는 모든 Claude 세션의 공용 저장소

## 상태 천이

```
spawning → idle ↔ busy
             ↓
           error
             ↓
            dead (socket close)
```

**State**
- `spawning` — tmux 세션 생성 중, hello 메시지 대기
- `idle` — Claude Code 입력 대기 (프롬프트 표시)
- `busy` — Claude Code 실행 중 (Running… / Waiting… / Compacting…)
- `error` — Compaction failed 또는 context limit reached
- `dead` — MCP server 종료 (socket close)

**Signal** (pane observation)
- `idle` — 입력 대기 (❯ 프롬프트)
- `busy` — 실행 중 (Running…, Waiting…)
- `compact` — 압축 진행 중
- `compact_error` — 압축 실패
- `context_limit` — 컨텍스트 한계 도달
- `trust_prompt` — 폴더 신뢰 프롬프트
- `resume_picker` — resume 선택지 표시

`observe()` 함수가 pane tail 20줄을 정규식으로 분석 (observer.ts 에 구현)

## PID Lock (중복 실행 방지)

**파일**: `~/.claude-bridge/telegram/bot.pid`

**acquirePollingLock()** (lifecycle.ts 에 구현)
1. 기존 PID 파일 읽기
2. 기존 PID가 살아있는가?
   - Yes: SIGTERM 전송, 2초 대기
   - 여전히 살아있으면: SIGKILL 전송
   - anomaly log: "stale_instance_evicted"
3. 자신의 PID 파일에 쓰기

**releasePollingLock()** (lifecycle.ts 에 구현)
- 자신의 PID와 일치하면 파일 삭제

사용 위치: dispatcher 시작 시

## Orphan Watchdog

**startOrphanWatchdog()** (lifecycle.ts 에 구현)

5초마다 확인:
1. stdin.destroyed? → orphan 감지 (원 프로세스 종료)
2. ppid 변경되었거나 ppid = 1? → init 프로세스가 parent → orphan 감지

**처리**
- anomaly log: "orphan_detected"
- onOrphan 콜백 실행 (graceful shutdown)

사용: MCP server 내부에서 시작 (예비 안전)

## Graceful Shutdown

**installShutdownHandlers()** (lifecycle.ts 에 구현)

신호 감지 (SIGINT, SIGTERM, SIGHUP) + stdin end/close:
1. 첫 호출만 처리 (중복 방지)
2. onShutdown 콜백 실행
3. 2초 대기 후 process.exit(0)

**dispatcher 의 shutdown()** (dispatcher.ts)

async 흐름 — Claude 가 `~/.claude/projects` JSONL 을 저장할 수 있도록 graceful
`/exit` 을 먼저 보낸다:

1. `tickTimer` clearInterval
2. `poller.stop()`
3. 각 활성 세션에 `gracefulKillTmux(tmuxName)` 병렬 실행:
   - `tmux send-keys -t cb-sN /exit Enter`
   - 최대 5s 동안 200ms 간격으로 `tmux has-session` 폴링
   - 타임아웃 시 `tmux kill-session` force
4. 모든 socket 종료
5. ipcServer 종료
6. registry 완전 비움 (영구 스냅샷이 "세션 0개" 로 기록)
7. `releasePollingLock()`

anomaly log: "shutdown" with reason

## Startup Orphan Reconciliation

**reconcileOrphans(registry)** (dispatcher.ts:57-90)

dispatcher 는 매 startup 시 tmux 를 신뢰원으로 삼아 registry 와 대조:

1. **stale_registry_entry** — registry 에 있는데 `tmux has-session` 실패 → `registry.remove()` (anomaly: orphan_detected, kind: stale_registry_entry)
2. **unknown_tmux** — `tmux list-sessions` 가 반환한 `cb-*` 중 registry 에 없는 것 → `tmux kill-session` (anomaly: orphan_detected, kind: unknown_tmux). MCP stdio 가 이미 끊긴 상태라 재연결 불가, 정리가 정답.
3. 정리 후 registry 를 완전히 비움 — 사용자는 `/new` 또는 `/resume` 으로 새로 시작.

`~/.claude/projects/` 의 JSONL 은 tmux 생명주기와 독립이므로 `/resume` 으로
이전 대화를 이어갈 수 있다.

## Active Session 추적

**Registry.active()** (registry.ts 에 구현)
- 현재 활성 세션 (메시지 수신 대상)

**Registry.setActive()** (registry.ts 에 구현)
- `/switch <id|label>` 또는 새 spawn 시 자동 설정

**세션 제거 시** (registry.ts 에 구현)
- 활성 세션이 제거되면, 남은 첫 번째 세션으로 전환

## Backlog

각 세션은 최근 100개 메시지 backog 저장 (registry.ts):
```
s.backlog.push(entry)  // "← hello world", "← /status" 등
```

**용도**
- `/backlog [id|label]` 으로 조회 (최근 20개)
- 세션 전환 시 얼마나 많은 메시지를 놓쳤는지 표시

## Known Fragility

- Trust dialog 자동 확인이 10초 폴링이라 가끔 놓칠 수 있음 (타임아웃 로그 기록)
- PPID 체크는 모든 Unix 계열에서 작동하지만, macOS 와 Linux 의 ppid 변경 타이밍이 다를 수 있음
- dispatcher 가 SIGKILL 로 강제 종료되면 graceful `/exit` 이 전송되지 않아 Claude 의 JSONL 이 미저장 상태일 수 있음. 재기동 시 orphan cleanup 이 tmux 는 제거하지만 저장되지 않은 컨텍스트는 복구 불가.

## 참고

- Registry 구조: `src/core/registry.ts`
- 세션 스캔: `src/core/sessions.ts`
- tmux 조작: `src/core/tmux/session.ts`
