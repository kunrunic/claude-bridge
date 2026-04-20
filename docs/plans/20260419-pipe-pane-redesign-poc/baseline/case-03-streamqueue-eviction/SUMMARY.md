# case-03 — 500줄 스크롤백 창 밖으로 블록 유실 (StreamQueue eviction)

- 원본: [`bugreport/20260417_095801/`](../../../../../bugreport/20260417_095801/)
- 수집 파일: `BUG_REPORT.md`, `tmux_capture.txt` (이 폴더)
- 부가 증거: `bugreport/20260417_095801/logs/` (`dump/events.jsonl` 이 핵심 — 23회 eviction 재구성 근거)

## 한 줄 현상

10시간 세션 중 `capture-pane -S -500` 창의 윗부분으로 밀려난 `⏺` 블록이 **조용히 삭제**. BUG_REPORT 추정 **최소 23회 eviction × 1블록/건 = 23건 데이터 손실**.

## 현 파서/큐 실패 지점

- `bridge/tmux.py:64-69` — capture-pane 이 최근 500줄만 읽음. 이 창 밖으로 나간 블록은 재해석 불가.
- `bridge/stream_queue.py:39-52` (`detect_slip`) — `idx > len(completed)` 또는 `completed[idx-1]` 의 앵커가 `last_fp` 와 불일치 시 slip 선언. 하지만 eviction 으로 앵커 자체가 창 밖으로 나가버리면 `take_new` 가 `[]` 를 반환 — **로그도 거의 남지 않는 침묵 경로** (`stream_queue.py:54-70`, docs/design/03 Known Fragility #4).
- 근본 구조: capture-pane 은 **유한 창 스냅샷** 이다. 단조 증가 `idx` 가 창 밖 앵커를 추적할 방법이 없다.

## 재설계가 해결해야 할 요구사항

1. pipe-pane raw 로그는 **append-only 무제한** — 500줄 창 개념 자체가 사라짐.
2. 분석기의 cursor 는 파일 offset (bytes). eviction 이 일어날 수 없는 구조.
3. StreamQueue 의 `idx`/`last_fp`/`detect_slip`/`advance_past` 전부 **불필요해짐** — offset 기반 dedup 으로 치환.

## 이 케이스가 baseline 에 포함되는 이유

Step 3 결정 게이트에서 **"재설계로 근본 해결되는 버그 종류"** 를 증명하는 케이스. case-01/02 는 알고리즘 개선으로도 부분 해결 가능하지만, case-03 은 창 구조 자체를 바꿔야만 해결 — pipe-pane 방향의 **존재 이유** 를 설명한다.
