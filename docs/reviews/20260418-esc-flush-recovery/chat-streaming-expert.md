# 채팅 스트리밍 전문가 리뷰 — ESC flush 회귀

리뷰 모델: Claude Opus 4.7. 관점: **채팅 스트리밍 / 터미널 UI 파서 전문가**. 중점: 앵커 기반 스트리밍의 원리적 한계, bypass vs persistent approval 플로우 차이, 반복 회귀의 물리적 원인.

## 한 줄 평

> 이 버그는 "경계 감지 실수" 가 아니라, **"terminal scrollback 을 상태 소스로 삼는 파서" 의 본질적 한계**. 단기 수정은 맞지만, 장기적으로 **delta 추적 기반**으로 옮기지 않으면 3개월마다 같은 회귀가 반복된다.

## 왜 이 버그가 반복되는가

Claude Code TUI 파서의 근본 구조:

```
                tmux capture-pane -p -S -500  (매 0.5s)
                         │
                         ▼
                ┌────────────────────┐
                │  pane_text (500줄) │  ← 상태의 단일 소스
                └────────────────────┘
                         │
         ┌───────────────┼───────────────┐
         ▼               ▼               ▼
    is_approval     _response_region   is_busy
    (tail 60)       (전체 스캔)         (divider 기반)
         │               │               │
         └── 같은 텍스트를 각기 다른 tail/조건으로 판정 ──┘
```

**문제**: pane 은 "현재 상태" 가 아니라 **"과거 + 현재가 섞인 스냅샷"** 이다. 그런데 판정 함수들은 "이 텍스트가 라이브냐?" 를 각자의 휴리스틱으로 결정. 한 함수의 tail 정책이 다른 함수와 어긋나면 즉시 분기 일관성이 깨진다.

이번 회귀:
- `_response_region` 경계 A: **전체 pane** 스캔 → scrollback 의 "Do you want to proceed" 를 라이브로 오판
- `is_approval`: **tail 60** 스캔 → 같은 텍스트를 과거로 판단
- 두 함수가 **반대 결론**을 내려 flush 가 봉쇄

**이건 "은근히 똑같은 버그" 가 계속 나는 게 아니라, 구조가 그런 버그를 산출하게 되어 있다**.

## bypass 모드 vs persistent approval 모드

| 항목 | bypass (`--dangerously-skip-permissions`) | persistent approval |
|------|-------------------------------------------|---------------------|
| 승인 박스 | 뜨지 않음 | 매 도구 호출마다 뜸 |
| `is_approval` | 항상 False | 라이브 시 True |
| `_response_region` 경계 A | **무의미** (승인 박스 자체가 없음) | 본 회귀의 무대 |
| `awaiting_approval` | 항상 False | set/clear 생명주기 |
| monitor 분기 | busy-stream 만 사용 | busy-stream + approval + response |
| ESC 동작 | "현재 출력을 멈춤" | "승인창을 닫음" |

**시사점**:

1. bypass 모드에서는 본 회귀가 **원리적으로 발생 불가** — 승인 박스가 없으니 경계 A 도 없음. 사용자가 bypass 로만 쓴다면 Fix 1 은 불필요.
2. persistent 모드에서 ESC 는 "취소" 가 아니라 **"승인창 dismiss"** — Claude 내부 상태는 "도구 거부됨" 으로 전환되며, 이후 Claude 가 자체 판단으로 응답을 이어갈 수 있다.
3. `cmd_esc` 의 역할 분기:
   - bypass: Claude 의 현재 출력을 멈춘다 (stop-generate)
   - persistent: 승인 박스만 닫는다 (modal dismiss)

   **하나의 `/esc` 명령이 두 의미를 모두 커버**한다는 게 UX 단순화 이점이자, 상태 머신 상 혼란의 원인. 본 Fix 2 의 `awaiting_approval=False` 는 **persistent 모드에서만 의미**가 있음 — 정확한 대응.

## 앵커 기반 스트리밍의 원리적 한계

`StreamQueue` (stream_queue.py:1-104) 는 두 가지 좌표로 스트림을 추적:

- `idx`: 몇 번째 블록까지 송출했나
- `last_fp`: 마지막 송출 블록의 fingerprint (scrollback eviction 시 복원용)

이 구조의 가정:
1. pane 의 `⏺` 블록은 시간 순 append-only.
2. scrollback eviction 시 과거 블록이 사라지더라도, 살아남은 블록 중 `last_fp` 와 매칭되는 것을 찾으면 `idx` 를 복원 가능.

**이 가정이 깨지는 경우**:

- **가정 1 실패**: Claude Code UI 업데이트로 `⏺` 블록 포맷이 변함 (실제 발생 이력 있음: `bugreport/20260417_*`).
- **가정 2 실패**: 같은 블록 preview 가 scrollback 에 여러 번 등장 (동일 질문 반복 세션) → `last_fp` 가 여러 위치에 매칭 → 엉뚱한 idx.

이번 회귀는 **가정 1/2 와는 무관** — 오히려 `extract_response_blocks` 가 **빈 리스트** 를 리턴해서 `StreamQueue.take_new` 의 입력 자체가 비어버린 것. 즉:

```
본 회귀의 병목:  _response_region(text) → [] → extract_response_blocks → []
                ↑ 이 지점에서 이미 실패
                
앵커 복원:       StreamQueue.detect_slip(...) → 사용되기 전에 입력이 empty
                → slip 감지 자체 미실행
```

**앵커는 이번에 무죄**. 하지만 **구조적으로 깨지기 쉬운 위치**에 있는 것은 맞다:

- 파서 → 앵커 → 송출이 파이프라인으로 연결
- 앞단(파서) 의 판정 오류가 뒷단(앵커) 를 우회한다
- 앵커는 정상 상태를 전제로 설계됨 — 입력이 비면 "아 정상인가?" 라고 해석

## 제안 Fix 에 대한 평가

### Fix 1 (parser 게이트) — 즉시 반영 필수

`is_approval(text)` 게이트는 **tail 정책을 두 함수가 공유하게 만드는 1차 수렴**. 이것만으로도 구조적 일관성이 개선됨. 단 한계:

- `is_approval` 의 tail 60 이 "깊이" 의 유일한 기준 — 이 값이 다른 함수(`_response_region` 의 내부 재스캔 tail 60)와 우연히 일치하는 것. 상수로 뽑아 공유 필수.
- 좁은 터미널/scrollback 설정 변경 시 tail 60 이 부족해질 가능성. 환경 변수로 override 가능하게 하는 게 안전.

### Fix 2 (cmd_esc 상태 복원) — 즉시 반영 필수

상태 머신 관점에서 **명백한 누락** 보정. 다만 `cmd_esc` 뿐 아니라:

- `cmd_model`, `cmd_agents`, `cmd_esc` 외의 키 주입 명령들도 점검 필요 — `send_key("Escape")` 가 다른 경로로 실행될 가능성
- Telegram 에서 `/esc` 외에 **pane 에서 사용자가 직접 ESC** 를 쳤을 때의 감지는? — 본 봇은 감지 안 함. 이 케이스는 monitor 의 `is_approval=False` 전이로 자연 복구 (`_recalc_awaiting` 같은 헬퍼 없음, `on_message` 시 reset 으로 커버).

### Fix 3 (관측 로그) — 조건 강화본 승인

`slip is not None` AND 조건 추가는 정상. 추가 권고:

- **Telegram 경보** — 이 로그가 1분 내 3회 이상 찍히면 admin chat 에 `⚠️ FLUSH-EMPTY 반복` 알림. 장래 회귀 조기 감지.
- **dump 연동** — `dump.event("core", "flush_empty_detected", ...)` 도 같이. CB_DUMP=1 세션에서 evidence 수집 자동화.

본 작업 범위 밖이지만 후속 권고.

## 리빌딩 옵션

### 옵션 A — Delta-only 파싱 (강력 추천)

현재: 매 tick `pane_text` 를 파싱해서 "블록 목록" 재생성.
**Delta-only**: 이전 tick `pane_text` 과 diff 해서 **신규 추가 라인**만 큐에 push.

```python
class DeltaBridge:
    def __init__(self):
        self.prev_lines: list[str] = []
        self.live_buffer: list[str] = []  # 현재 형성 중인 블록
        
    def tick(self, pane_text: str):
        lines = pane_text.splitlines()
        new_tail = lines[len(self.prev_lines):]  # scrollback eviction 고려 필요
        for line in new_tail:
            self.live_buffer.append(line)
            if is_block_end(line):
                yield "\n".join(self.live_buffer)
                self.live_buffer = []
        self.prev_lines = lines
```

**원리적 장점**: scrollback 의 stale marker 가 delta 에 포함되지 않음 → 경계 감지 불필요 → 이번 류 회귀 **발생 불가**.

**난점**:
1. **Scrollback eviction** — `prev_lines` 와 `lines` 가 정렬되지 않을 수 있음. `last_fp` 같은 앵커는 여기서도 필요.
2. **ANSI 라이브 업데이트** — Claude Code TUI 는 승인 박스 내부를 in-place 로 업데이트 (Yes 에 포커스 → No 로 이동 시 같은 라인 재렌더). delta 기반은 "변경된 라인" 도 감지 필요.
3. **tmux capture 주기 0.5s** — delta 는 sub-second 라이브 업데이트 누락. 이는 승인 박스 상태 판정(Yes/No 포커스) 에 영향.

**평가**: 방향은 옳음. POC 비용 높음(2주). 실패 리스크 중간. 전용 브랜치에서 hack day 로 검증 후 결정.

### 옵션 B — Claude Code JSON 출력

`claude --output-format=json` 또는 유사. **불가능**:

- 현재 Claude Code TUI 는 stdout 으로 구조화 JSON 을 내지 않음. 오로지 터미널 ANSI 렌더링만.
- `claude -p` (print mode) 는 JSON 지원하지만 interactive 세션에서는 불가.
- **Claude Code 가 장래 interactive 세션용 machine-readable 출력을 지원하지 않는 한** 이 방향은 차단.

### 옵션 C — tmux pipe-pane + ANSI state machine

`tmux pipe-pane -o 'cat >> /tmp/pane.stream'` 로 터미널 바이트 스트림 직접 수신. 자체 ANSI 파서로 렌더링 상태 재구성.

**평가**:
- 정확도 최고. delta 를 실시간(sub-second) 포착.
- 구현 비용: **매우 높음**. ANSI state machine 은 terminfo 전체 커버가 사실상 불가능. Claude Code 가 사용하는 시퀀스에 국한해도 수천 라인 코드.
- 유지보수: Claude Code 업데이트 시 ANSI 시퀀스 호환성 수시 재확인.

**현실적으로 비추천**. 다만 옵션 A 가 0.5s 주기로 충분치 않다는 게 판명되면 최후 수단.

## 결론

- **즉시**: Fix 1/2/3 반영. 이번 세션 스코프.
- **다음 분기**: `boundary_from_live_box` 헬퍼 + tail 상수 공유. 경계 감지 일관성 확보.
- **반기 내**: Delta-only POC. 성공하면 옵션 A 로 이전, 실패하면 현 구조에서 헬퍼 확장.

반복 회귀를 끊으려면 **파서의 역할을 재정의**해야 한다. "pane 전체를 상태로 본다" 에서 "pane 변화를 이벤트로 본다" 로.

## 참고

- 5 가지 flush 타입 상세: [../../design/02-streaming-pipeline.md](../../design/02-streaming-pipeline.md)
- 경계 감지 tail 정책 표: [../../design/03-parser-and-boundaries.md](../../design/03-parser-and-boundaries.md)
- 승인 플로우 비교: [../../design/05-approval-flows.md](../../design/05-approval-flows.md)
