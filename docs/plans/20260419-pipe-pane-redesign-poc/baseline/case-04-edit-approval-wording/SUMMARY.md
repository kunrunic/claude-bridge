# case-04 — Edit 승인 다이얼로그 wording 미스매치 + queue_slip 무한루프

- 재현 일시: **2026-04-19 17:28:12 ~ 17:30:30** (Step 1 검증 중 자연 발생, 의도 재현)
- 수집 파일:
  - `pipe-pane-raw.log` — CB2 monitor 의 raw ANSI stream (452 KB)
  - `events.jsonl` — `dump.event()` 구조화 로그 (CB_DUMP=1, 15 KB)
  - `pane_tick_keyframes.jsonl` — 결정적 3 tick 의 pane snapshot (17:28:44, 17:28:56, 17:30:01)
- 원본 log 경로 (비복사):
  - `~/.claude-bridge/panes/claude_bridge2/raw-20260419_172304.log`
  - `dump/20260419/172234_claude-bridge2/pane_tick.jsonl` (6.6 MB, 전체 tick)

## 한 줄 현상

Claude 가 Edit 도구 승인 다이얼로그 (`Do you want to make this edit to reviewer.py?`) 를
띄웠으나, **Telegram 에는 승인 요청이 오지 않고 봇이 조용히 멈춤**. 사용자는 최소
2분간 대기 후 직접 탐지.

## 타임라인 (events.jsonl 기반)

| 시각 | 이벤트 | 메모 |
|------|--------|------|
| 17:28:12 | `on_message` + `send_input` | "응 그러면 그부분 진행해볼까? 이전 버전으로 커밋은 완료된 상태지?" |
| 17:28:13 ~ 34 | `flush_peek busy-stream × 4` | `completed=0 new=0` (Claude thinking) |
| 17:28:44 | `flush_block × 2` | ✓ `⏺ 응, 이전 커밋 0cc8aa7 완료됨...` + `⏺ Reading 1 file…` Telegram 전송 |
| 17:28:45 ~ | (이벤트 없음) | Claude 가 diff 작성 + 승인 다이얼로그 그림 |
| 17:28:56 | `queue_slip idx_past_cc (idx=2 cc=0)` | **첫 slip**. `last_fp='⏺ Reading 1 file…'` |
| ~17:30:29 | `queue_slip × 9` (`idx_past_cc` / `anchor_mismatch` 교차) | completed_count 가 0 ↔ 2 사이 요동 |
| 전 구간 | `is_approval`, `send_approval` | **0건** — 승인 인식 실패 |

## 현 파서 실패 지점

### 가설 A (주, 확신 거의 100%) — `APPROVAL_RE` wording 불일치

`bridge/parser.py:17-20`:
```python
APPROVAL_RE = re.compile(
    r"Do you want to proceed|"
    r"Allow\s+\w+\s+to|Proceed\?|\(Y/n\)|\(y/N\)"
)
```

raw log L18328 (ANSI 제거 후):
```
Do you want to make this edit to reviewer.py?
```

어떤 alternation 에도 매치 안 됨 → `is_approval()` False → `_send_approval` 미호출.

일관성 증거: **같은 파서 내부** `_approval_context` (line 344) 와 `_approval_box`
(line 364) 는 이미 broad `"Do you want to" in line` 로 구현됨. `is_approval` 과
`_response_region` 만 strict 를 쓰는 비대칭.

### 가설 B (부, `_response_region` 도 동일 원인) — queue_slip 소음

`bridge/parser.py:231` 도 strict `"Do you want to proceed"` → Edit 다이얼로그
carve-out 실패 → `end` 가 매 tick 불안정하게 흔들림 → StreamQueue 가 `idx=2` 고정인데
`completed_count` 가 2/0 사이 flicker → `idx_past_cc` 반복.

두 번째 slip 종류 `anchor_mismatch` 는 tail 에서 `⏺ Reading 1 file…` 앵커가 줄 형식
변화로 hash 불일치 되는 것 (TUI 가 본문 폭 재계산으로 재렌더).

## 재설계가 해결해야 할 요구사항

1. 승인 다이얼로그 검출이 **문구 (Claude Code 릴리즈마다 변할 수 있음) 에 독립** 할 것.
2. 승인 박스가 발견되면 그 **생애주기** (`show`/`confirm`/`deny`/`cancel`) 를 명확히
   이벤트화 — 중간 상태에서 queue loop 돌지 않음.
3. 승인 박스 렌더 중 새 `⏺` 블록이 없다는 사실을 **확신** 할 수 있어야 함 — 현재는
   "block 이 있는데 못 본 건지 / 진짜 없는 건지" 구분 불가.

## 이 케이스의 진단 가치

현재 수집된 5개 baseline 중 **유일하게 raw + events + pane_tick 삼각 측량 완료**.
Step 2 분석기의 기대 이벤트 시퀀스를 가장 구체적으로 검증할 수 있는 fixture.
