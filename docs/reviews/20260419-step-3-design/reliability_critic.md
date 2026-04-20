# Reliability/Ops 실패 모드 분석 — Pipe-Pane Redesign Step 3

## 요약

파이프라인 설계가 기존 boundary/dedup 회귀를 해결하더라도, **새로운 failure chain** 이 Step 4 cutover 시 나타날 수 있다. 특히:
1. **Pipe-pane 파일 손상 & rotation race** → 이벤트 누락 (복구 불가능)
2. **Analyzer crash 또는 state 손상** → offset 무결성 깨짐 (재기동 불가)
3. **BridgeAdapter 도입 시 in-flight state 오염** → 이벤트 중복 또는 누락
4. **Offset 불변식 위반** → truncation, inode reuse, send_input 상관 실패

아래는 각 시나리오별 구체적 영향, 감지 방법, 완화책을 기술한다.

---

## 1. Pipe-Pane 파일 손상 & Rotation Race

### 1a. Disk Full 시 파일 truncation

**시나리오**:
- 세션 진행 중 disk full 오류 발생 (`/home/.claude-bridge/` 가 차 버림).
- tmux `pipe-pane` 이 `cat >> raw-<timestamp>.log` 를 실행 중인데 disk write 실패.
- 파일의 마지막 1-2 줄이 incomplete (ANSI escape 시퀀스 절반).

**관측 증상**:
- Tokenizer.feed() 가 incomplete escape sequence 를 처리 → 토큰 생성 실패 또는 오탐.
- 그 이후 token stream 이 desynchronized → row 위치 어긋남 → region_tagger 가 wrong envelope 탐색.
- 사용자 측에서는 "한동안 응답이 오지 않다가 갑자기 블록 2-3개가 한 번에" 나타나는 현상.

**감지 방법**:
- Tokenizer 의 incomplete 토큰 카운트를 event 로 기록 → `{"source": "analyzer", "kind": "tokenizer_incomplete_escape", "count": N}`.
- dump.events.jsonl 에 연속 N > 5 인 구간 → alert 필요.
- 대안: 파일 write 전에 disk space check (설정에서 `BRIDGE_PIPE_PANE_MIN_FREE_MB` 추가).

**완화**:
- **사전**: Step 4-α 개시 전 disk 용량 점검 guide 작성. cron 으로 `find ~/.claude-bridge/panes -mtime +7 -delete` (R2 risk 참조).
- **사후**: Tokenizer 가 incomplete sequence 를 보면 **다음 chunk 를 기다리지 말고** buffer 에 남김. 차 이상 chunk 에서 이어받기 (robust streaming).

**심각도**: HIGH — 이벤트 영구 누락 가능. 단, 현재 `BRIDGE_PIPE_PANE_MAX_BYTES=20MB` 정책과 7일 보존 (side-effects.md R2) 이면 프로덕션에서 매달 1-2 회 정도 가능성.

---

### 1b. Rotation 시 1-2 line loss (race condition)

**시나리오**:
- `_pipe_maybe_rotate()` 에서 20MB 도달 → `stop_pipe_pane()` + `start_pipe_pane(new_file)` 사이 gap (50-500ms).
- 그 동안 Claude 가 output 을 계속 생성 → tmux 가 기록할 곳이 없어 버퍼 오버플로우 또는 손실.

**관측 증상**:
- 로그 offset 에 gap: `offset_A` 이후 `offset_A + 1000` (중간 200 bytes 미포함).
- Tokenizer 가 gap 을 감지할 수 없음 (raw bytes 만 봄) → 그 구간의 라인이 완전히 누락.
- 최악의 경우: "⏺ 완료" 문자열이 gap 에 걸려 block 이 두 파일에 split → 한쪽 파일만 분석하면 중복 또는 누락.

**감지 방법**:
- core.py `_pipe_maybe_rotate()` 에서 rotate 직전 `capture-pane` snapshot 을 보조 로그로 기록 (side-effects.md R5 참조).
- Analyzer: raw 파일 시작 시 inode check → 이전 파일과 다르면 "rotation detected, offset bridge 필요" 로깅.
- 비교: offset 과 파일 크기로 "예상 바이트 수" vs "실제" 검증.

**완화**:
- **즉시**: `stop_pipe_pane()` 이후 즉시 `capture-pane -p` 로 현재 화면을 dump → 교차 검증용 backup.
- **설계**: rotation 시 **gap 을 명시적으로 기록** → `{"offset": X, "gap_bytes": 200, "reason": "rotation"}` 이벤트 → offset resume 시 "gap 이후부터" 시작하도록 logic 추가.
- **테스트**: rotation 중에 intentional 1초 sleep → 그 동안 Claude 가 계속 생성하도록 시뮬레이션.

**심각도**: MEDIUM — 통상적 프로덕션 에서는 gap 이 1-2 줄 정도 (큰 블록은 split 가능성 낮음). 하지만 72h shadow run 에서 반복되면 누적 손실 가능.

---

## 2. Analyzer Crash & State 복구 불가능

### 2a. Analyzer 프로세스 OOM / segfault

**시나리오**:
- pipe_pane_analyzer.py 가 450KB 이상 raw 파일 분석 중 VTScreen 가 메모리 누적 (행 캐시 미정리 등) → OOM killer 에 의해 프로세스 종료.
- 또는 Tokenizer 의 특수 ANSI sequence 처리에서 stack overflow.

**관측 증상**:
- Analyzer process exit code != 0.
- events.jsonl 파일이 불완전 (마지막 몇 줄이 없음).
- Step 4-α shadow run 에서 "기존 parser 는 정상인데 analyzer 만 silence" → diff 는 무한정 불일치.

**감지 방법**:
- BridgeAdapter (신규) 가 analyzer subprocess 를 spawn 할 때 `returncode` check → non-zero → `dump.event("analyzer", "crash", returncode=X)`.
- 동시에 마지막 offset 확인 → "offset Y 이후 누락" 으로 기록.
- Telegram 에 alert: "분석기 비정상. 기존 parser 로 fallback 중".

**완화**:
- **사전**: tools/ 모듈의 메모리 프로파일링 (step 2 에서 case-04 450KB 에 대해 이미 수행했으므로 기준선 확보 가능).
  - VTScreen: row 캐시가 session 수명 동안 계속 메모리에? → snapshot() 은 언제 clear?
  - LineCommitter: `_pending` dict 의 크기 상한은? (현재 rows × cols 크기만 해야 함).
  - EventClassifier: 내부 상태 누적? (step-2 에서 추정 메모리 footprint 측정).
- **사후**: crash 발생 시 auto restart (최대 3회) → 매번 failure 는 `--until-offset N` 으로 narrowing.

**심각도**: HIGH — 복구 불가능 (analyzer 가 떨어진 점 이후는 이벤트 재생 불가).

---

### 2b. Analyzer 상태 파일 손상 (`last_offset` 기록)

**시나리오**:
- BridgeAdapter 가 분석기의 progress 를 `~/.claude-bridge/panes/<session>/analyzer_state.json` 에 기록 중인데, disk full → JSON 파일이 incomplete (valid JSON 아님).
- 재기동 시 이 파일을 읽어 `last_offset` 복구하려 하지만 parse error.

**관측 증상**:
- Analyzer restart 시 offset 복구 실패 → 파일 처음부터 다시 분석 → 중복 이벤트 발화 → Telegram 에 과거 메시지 재전송.
- 또는 offset 을 0 으로 reset → 그 이후 사용자가 "왜 옛날 메시지가 또 와?" 혼란.

**감지 방법**:
- `json.load(state_file)` 전 `try-except JSONDecodeError` → catch 시 log + fallback to offset=0.
- 동시에 `dump.event("analyzer", "state_load_fail", filename=..., error=...)`.
- 파일을 atomic write 로 보호: tmpfile → rename (Python 의 `atomicfile` 라이브러리 또는 manual two-phase).

**완화**:
- **즉시**: state 파일을 **2개 버전 관리** (current + backup) → 하나 손상되면 다른 쪽 사용.
- **설계**: state 파일이 아니라 **events.jsonl 의 마지막 offset 을 grep 으로 추출** → 별도 상태 파일 불필요.
  ```python
  # Pseudo
  with open(events_file, 'rb') as f:
      f.seek(-1000, 2)  # 끝에서 1KB
      for line in f:
          try: evt = json.loads(line); last_offset = evt['offset']
          except: pass
  ```

**심각도**: MEDIUM-HIGH — 중복 이벤트 발화로 사용자가 같은 내용을 여러 번 받을 수 있음.

---

## 3. BridgeAdapter Cutover 시 In-Flight State 오염

### 3a. 환경 플래그 전환 중 이벤트 source 불일치

**시나리오**:
- Step 4-γ 에서 `BRIDGE_DISPATCH_SOURCE=parser` (old) 에서 `analyzer` (new) 로 전환 중.
- 그런데 몇몇 메시지 처리 중에 flag 값이 바뀜 (예: hot-reload 또는 실수로 손 대면) → 같은 turn 안에 old/new 분기가 섞임.

**관측 증상**:
- 같은 `⏺` 블록이 old parser 경로와 new adapter 경로에서 **각각 detect** → Telegram 에 2번 전송.
- 또는 한 경로만 감지해서 한쪽은 누락.
- dump.events.jsonl 에 `source=parser` 와 `source=analyzer` 이벤트가 같은 offset 범위에서 혼재.

**감지 방법**:
- BridgeAdapter init 시 flag 값을 기록 → session lifetime 동안 **절대 변경 금지** (runtime immutable).
- 만약 변경 시도하면 exception throw.
- Receiver 의 `/start` / `/switch_session` 핸들러에서 **session 재기동** (새 flag 값 적용) 강제.

**완화**:
- **설계**: flag 는 environ 이 아니라 session config (~~.claude-bridge/panes/<session>/config.json~~) 에 저장 → session 별 고정.
- **운영**: Step 4-γ 기간 동안 env flag 핫 수정 금지. 변경 필요 시 bridge 를 `stop()` → `start()` 로 강제 재시작.

**심각도**: MEDIUM — 사용자 관점에서 "같은 내용 2번" 또는 "내용 누락" 으로 보이는 혼란.

---

### 3b. Rollback 중 held_blocks & _modal accumulator 상태 미복원

**시나리오**:
- Step 4-γ 에서 adapter 로 처리 중, 사용자가 approval modal 보고 대기 중 (bridge.awaiting_approval=True, held_blocks=[...]).
- 그런데 회귀 발견 → `BRIDGE_DISPATCH_SOURCE=parser` 로 즉시 rollback.
- 기존 parser 경로는 저 상태를 **모르기 때문에** (새 state 가 adapter-only) → 모달 이후 응답을 detect 못함.

**관측 증상**:
- Rollback 직후 사용자가 모달을 승인 → 응답이 오지 않음 (기존 parser 는 `awaiting_approval` 를 본 적이 없어서 pre-approval flush 건너뜀).
- 1-2분 대기 후 사용자가 "뭐해?" → 그제야 `queue.reset()` 에 의해 복구 (새 turn).

**감지 방법**:
- rollback env flag 변경 시 **현재 bridge 상태 snapshot** → dump.event("bridge", "rollback", state=...).
- 동시에 `held_blocks` 가 있으면 "pending state 존재" 경고.

**완화**:
- **설계**: rollback 은 env flag 만이 아니라 **프로세스 재시작** 으로만 허용 (core.restart()).
- 또는 adapter 와 parser 경로를 **완전히 독립적 상태** 로 관리 (각각 자신의 awaiting_approval 를 가짐) → merge 불필요.
  - (권장 아님 — 복잡도 증가.)
  
**심각도**: MEDIUM — 롤백 시 서비스 일시 중단 (1-2분), 자동 복구 가능하지만 사용자 체감 bad.

---

## 4. Offset 불변식 위반

### 4a. File Truncation (inode reuse)

**시나리오**:
- Analyzer 가 offset 12345 까지 진행함 → 그 이후 offset resume 기록.
- 그런데 어떤 이유로 raw 파일이 **정확히 offset 12345 로 truncate** 됨 (tmux rotation + 수동 정리).
- 이후 파일 크기가 커지다 → inode 는 그대로 (같은 파일 path).
- 재기동 시 "offset 12345 부터" 읽으려 하지만, 현재 파일의 내용이 **완전히 다른 data** (offset 0 ~ 12344 가 truncate 된 후의 새 output).

**관측 증상**:
- Tokenizer 가 new data 를 old offset 으로 해석 → offset 무결성 깨짐.
- `{"offset": 12345, "t": "block_commit", "text": "..."}` 라는 이벤트가 실제로는 new data 인데 old offset 으로 recorded.
- 나중에 Telegram dispatch 시 "offset 12345 의 이벤트는 3시간 전" 이라고 착각 → dedup 실패 또는 과거 메시지 재송.

**감지 방법**:
- inode (file identity) + size + mtime 을 offset 함께 기록 → `{"offset": X, "inode": Y, "size": Z, "mtime": T}`.
- resume 시 현재 파일의 inode/size 와 비교 → 다르면 "file 이 변경됨" alert.
- 같은 path 라도 inode 가 다르면 offset reset (offset 0 부터 재시작).

**완화**:
- **즉시**: rotation 정책을 명확히 → 절대 truncate 하지 말고, **rename + new file** 만 사용.
  - Bad: `truncate -s 0 raw-old.log`
  - Good: `mv raw-old.log raw-old.log.rotated` + `touch raw-new.log`
- **설계**: offset resume 시 inode matching check 추가 (tools/pipe_pane_analyzer.py).

**심각도**: HIGH — offset 기반 dedup 의 근본을 위협. 이벤트 중복 또는 영구 누락.

---

### 4b. Send_input 상관 실패 (case-02 재발)

**시나리오**:
- BridgeAdapter 가 `user_prompt` 이벤트를 emit 하려면 send_input log 와 content 를 상관시켜야 함 (step-3-design.md §4).
- 그런데 analyzer 가 off-line 으로 실행되는 경우 (shadow run) → send_input 은 **real-time 에만 available** → 나중에 raw file 을 재분석할 때 send_input 을 다시 볼 수 없음.

**관측 증상**:
- shadow run 중에는 "user-echo 정확하게 감지" → diff 0 인 듯.
- 그런데 나중에 offline 재분석 (bugreport 조사 등) 에서는 send_input 정보 없어서 case-02 (user-echo 판별) 실패 → fixture 재현 불가능.
- **그 외**: 이 상관이 실패하면 user input 이 content 로 나타나는 구간을 "승인" 이나 "응답" 으로 오분류 가능.

**감지 방법**:
- Analyzer 가 content 에서 user-like text (첫 줄이 ">" prefix, 맞춤법 정확함 등 heuristic) 를 감지 → flag.
- 동시에 send_input log 를 필요로 하는 이벤트는 "실제로 send_input 을 봤는가" 를 기록 → 나중에 "incomplete correlation" alert.

**완화**:
- **설계**: send_input log 를 **raw 파일 자체에 embedd** → tmux 의 capture-pane 결과가 아니라 bridge 가 send 한 입력을 별도 stream 으로 기록.
  - 예: `~/.claude-bridge/panes/<session>/send_input.jsonl`
  - `{"offset": X, "text": "...", "t": X}`
  - 그러면 analyzer 가 이 파일도 함께 load → 상관 가능.
- 또는 raw 파일 자체가 "bridge 가 send 한 입력" 을 마킹 → tokenizer 가 감지.

**심각도**: MEDIUM — case-02 는 드물지만 (스크린샷 공유 케이스), 발생 시 false-approval 루프 (bugreport/20260417_164104 참조).

---

## 5. Chunk Processing Backpressure (perf 저하)

### 시나리오

- Step 4-α shadow run: analyzer 와 old parser 를 병행 실행.
- Analyzer 가 64KB chunk 를 process 중 (tokenize + VT + commit + tag + classify) → CPU 100%.
- 그 사이 새 pipe-pane 데이터가 계속 들어옴 → raw 파일 크기 급증.
- 120초 후에야 analyzer 가 현재 상태를 따라잡음 → shadow-diff 계산이 지연 → user 는 "응답이 느려" 체감.

**관측 증상**:
- Telegram 응답 지연이 1-2초 → 5-10초 로 증가 (shadow run 기간에만).
- CPU 모니터링에서 "analyzer + capture-pane + monitor" 이 3개 코어 full.
- 실제 Claude 는 빨리 응답했는데, bridge 분석 지연으로 사용자가 못 받음.

**감지 방법**:
- BridgeAdapter: analyzer 의 CPU/wall-time 을 measure → `dump.event("analyzer", "chunk_latency", duration_ms=X)`.
- shadow-diff 계산 시간도 기록.
- 통상 chunk 당 < 100ms 예상 (step-2-results.md case-04 에서 450KB < 1s offline).

**완화**:
- **설계**: analyzer 를 별도 프로세스 또는 thread 로 분리 → pipe-pane 수집과 비동기.
- **운영**: shadow run 중에는 일단 이벤트 dispatch 를 old parser 로 유지 → analyzer 결과는 비교용만.
- **성능**: chunk size (현재 64KB) 를 tuning → too small → overhead, too large → latency spike.

**심각도**: LOW-MEDIUM — 사용자 체감 느려짐이지만, 실제 손실 없음. 72h shadow 기간이 길어질 수 있음.

---

## 6. 통합 실패 시나리오 & 검출 매트릭스

| # | 시나리오 | 관측 증상 | 감지 방법 | 복구 가능? | 심각도 |
|----|---------|---------|---------|----------|--------|
| 1a | Disk full → raw truncation | Tokenizer incomplete escape, 응답 지연 1-2분 | `tokenizer_incomplete_escape` event count | 일부: `--until-offset N` 이후 재분석 | HIGH |
| 1b | Rotation race → line loss | Offset gap, split block | inode/size mismatch alert, gap event | 일부: gap 이후부터 resume | MEDIUM |
| 2a | Analyzer crash (OOM/segfault) | Process exit code != 0, events.jsonl incomplete | Crash event logging, last offset comparison | 아니오 (재시작 후 offset resume, 그 이후만) | HIGH |
| 2b | State file JSON corrupt | Offset 복구 실패, 중복 또는 누락 이벤트 | JSONDecodeError catch + fallback | yes (2-version state, atomic write) | MEDIUM-HIGH |
| 3a | Env flag 전환 중 분기 섞임 | 같은 이벤트 2배 또는 1배, diff 혼재 | Session-level config, hot-reload 금지 | yes (process restart 강제) | MEDIUM |
| 3b | Rollback 중 state 미복원 | awaiting_approval 미적용, 응답 누락 1-2분 | Rollback event logging, state snapshot | yes (automatic reset next turn) | MEDIUM |
| 4a | File truncation (inode reuse) | Offset 무결성 깨짐, 중복/누락 | inode/size/mtime 기록 및 검증 | 부분 (손상 구간 이후만) | HIGH |
| 4b | Send_input 상관 실패 (case-02) | User-echo 오분류, false-approval 루프 | offline embedding, heuristic flag | 부분 (send_input.jsonl 추가 시) | MEDIUM |
| 5 | Chunk backpressure (CPU 100%) | 응답 지연 5-10초, shadow run 길어짐 | Chunk latency measurement | yes (async process 분리, chunk tuning) | LOW-MEDIUM |

---

## 7. 실패 패턴과 선제적 대응

### 가장 가능성 높은 failure (30일 이내)

1. **Disk full → raw file truncation (1a)**
   - 이유: 프로덕션 개발 env 의 디스크 정책이 엄격하지 않음. `~/.claude-bridge` 는 unmanaged.
   - 확률: 72h shadow run 중 **최소 1회** (누적 데이터량 고려 시).
   - 조치: Pre-flight disk check, cron cleanup 강제.

2. **Analyzer crash during shadow run (2a)**
   - 이유: 450KB 는 테스트했으나, 72h × 3-5 MB/h = 200+ MB 누적 데이터.
   - 확률: **중간** (메모리 leak 가능성).
   - 조치: Memory profiling & safe defaults, auto-restart (max 3).

3. **Offset gap (rotation race) (1b)**
   - 이유: rotation 시 50-500ms gap 에서 Claude output 을 tmux 가 못 받음.
   - 확률: **낮음** (gap 이 커야 line 손실), 하지만 72h 누적 시 가능.
   - 조치: rotation 직전 snapshot backup, gap event 명시.

### 최악의 시나리오 (1개월 이상)

- **Multiple failure 조합** (예: disk full + 동시에 analyzer crash + state file corrupt)
  - 결과: 이벤트 영구 누락 (복구 불가).
  - 방어: redundant backup (dual state files, rotation snapshots).

---

## 결론

파이프라인의 신뢰성 위험은 **offset 무결성** 에 집중된다. 기존 parser 의 boundary/dedup 회귀는 해결하지만, new failure mode (rotation, state corruption, inode reuse) 가 offset 기반 dedup 을 무너뜨릴 수 있다.

**Step 4-α shadow run 합격의 전제**:
- [ ] disk full 대응 (pre-flight check + retention policy).
- [ ] analyzer crash 대응 (memory profiling + auto-restart).
- [ ] offset/inode 기록 (gap event + resume 검증).
- [ ] state file atomic write (2-version backup).
- [ ] send_input.jsonl embedding (case-02 재발 방지).

**30일 내 가장 그럴듯한 failure**:
**Disk full 중에 analyzer 가 incomplete token 처리 → Tokenizer 상태 desync → 3-5분 동안 응답 누락 → 사용자가 "뭐해?" 입력 → queue.reset() 으로 자동 복구**. 이 시나리오는 현재 설계로는 **감지 불가능** (FLUSH-EMPTY 로그가 안 나옴, 기존 parser 는 별도로 계속 동작).
