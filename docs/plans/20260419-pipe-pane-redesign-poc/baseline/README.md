# baseline — Step 0 기준선 아카이브

PoC 분석기가 **해결해야 할 실제 회귀 3건** 을 고정 fixture 로 박제한 폴더.

## 선정 케이스

| 폴더 | 원본 | 축 | 재설계로의 함의 | Gate 기여 |
|------|------|-----|-----------------|-----------|
| [case-01-esc-flush-block/](case-01-esc-flush-block/SUMMARY.md) | `bugreport/20260418_094744/` | 경계 탐색 tail 비대칭 | ESC 잔해 모달이 `_response_region` 을 당겨 flush 봉쇄. 이벤트 기반 분석기로 근본 해결 후보 | 강 |
| [case-02-scrollback-echo/](case-02-scrollback-echo/SUMMARY.md) | `bugreport/20260417_164104/` | 사용자 입력 echo 오탐 | 같은 frame 을 UI 오버레이로 오인. line-commit + box-drawing 식별로 분리 검증 | 중 |
| [case-03-streamqueue-eviction/](case-03-streamqueue-eviction/SUMMARY.md) | `bugreport/20260417_095801/` | 500줄 창 유실 | 유한 창 스냅샷의 구조적 한계. pipe-pane append-only 가 **구조적으로** 해소 | **최강 (keystone)** |
| [case-04-edit-approval-wording/](case-04-edit-approval-wording/SUMMARY.md) | 2026-04-19 17:28 재현 (Step 1 live) | Edit 승인 문구 miss + queue_slip 루프 | 문구 의존 + snapshot flicker. 구조 기반 승인 검출 + event lifecycle 로 해소 | 중 |

4축 (경계탐색 / echo 오탐 / 창 유실 / 문구+flicker) 을 각각 대표하도록 골랐음.
Decision Gate 평가는 각 폴더의 `evaluation.md`, 집계는 [../evaluation.md](../evaluation.md) 참조.

## baseline 에 포함하지 **않은** 리포트 (참고 사례)

분석기 알고리즘 검증 대상은 아니지만 scope 혼동 방지를 위해 링크만:

| 리포트 | 이유 |
|--------|------|
| `bugreport/20260417_172211/` | tmux `-t` prefix-match 로 sibling 세션 조작. 파서/분석기와 무관, 커밋 `591f553`/`eecbbce` 에서 수정 완료 |
| `bugreport/20260417_163701/` (claude-bridge 레포) | `dump.event kind` 인자 충돌 — 이미 `2090abb` 수정. QUEUE-SLIP 부분은 case-03 과 축이 같음 |
| `/Users/kunrunic/claude-bridge/bugreport/20260417_095417/` | `BUG_REPORT.md` 부재, raw capture 만 존재 |

## 파일 구조

```
baseline/
├── README.md                           (이 파일)
├── case-01-esc-flush-block/
│   ├── SUMMARY.md                      (현상 / 실패 code path / 재설계 요구사항)
│   ├── evaluation.md                   (raw+VT 전환 실효성 Category-A/B/C 평가)
│   ├── BUG_REPORT.md                   (원본 복사)
│   └── tmux_capture.txt                (원본 복사)
├── case-02-scrollback-echo/
│   └── ... (동일 구조)
├── case-03-streamqueue-eviction/
│   └── ... (동일 구조)
└── case-04-edit-approval-wording/
    ├── SUMMARY.md
    ├── evaluation.md
    ├── pipe-pane-raw.log               (Step 1 raw ANSI, 452KB)
    ├── events.jsonl                    (dump.event 구조화 로그, 15KB)
    └── pane_tick_keyframes.jsonl       (결정적 3 tick pane snapshot)
```

원본 `logs/` 디렉토리는 크기가 커서 복사하지 않음 — 필요 시 `bugreport/<ts>/logs/` 직접 참조.
case-04 는 Step 1 PoC 로 실시간 수집된 raw+events 가 이미 있어 원본이 이 폴더 안에 있음.

## 이 폴더의 용도

- **Step 2 분석기 fixture**: `tools/fixtures/baseline/` 가 이 폴더를 참조하거나 심볼릭 링크.
- **Step 3 게이트 입력**: 분석기 이벤트 시퀀스를 각 `SUMMARY.md` 의 "재설계가 해결해야 할 요구사항" 과 대조.
- **Step 5 회귀 테스트 fixture 의 원천**: 구 파서 제거 시 `tests/test_bug_*` 의 raw-log 변환 대상.
