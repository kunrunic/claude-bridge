# 20260418 — ESC 이후 flush 봉쇄 복구

## 배경

2026-04-18 `/esc` 로 승인 모달을 취소한 이후부터 Claude 응답이 Telegram 으로 한 건도 전달되지 않는 회귀 발견. 원본 분석:

- 버그 리포트: [`bugreport/20260418_094744/BUG_REPORT.md`](../../../bugreport/20260418_094744/BUG_REPORT.md)
- 3-에이전트 교차 리뷰: [`../../reviews/20260418-esc-flush-recovery/`](../../reviews/20260418-esc-flush-recovery/README.md)

## 요약된 원인 (리뷰 합의)

1. **주가설 A (확신 높음)**: `bridge/parser.py:227-238` `_response_region` 의 승인 박스 경계 감지가 **tail 제한 없이** 전체 pane 을 역방향 스캔. ESC 로 닫힌 모달의 `Do you want to proceed?` 텍스트가 scrollback 에 남아 오탐 → `end` 가 과거 모달 앞 divider 로 고정 → 이후 `⏺` 블록이 `extract_response_blocks` 범위 밖으로 밀려남 → flush 0 건.
2. **보조가설 B**: `bridge/receiver.py:77-91` `cmd_esc` 가 `bridge.awaiting_approval` 을 복원하지 않음. monitor 가 `awaiting_approval=True` 분기에서 sleep 유지 → ESC 직후 생성된 응답도 관측 못함.

A 단독으로도 재현 가능. B 는 같은 세션의 늦은 late-denied 가 플래그를 풀기 전까지 추가로 응답을 잡아먹음.

## 변경 파일 범위

| 파일 | 변경 요지 | 상세 |
|------|----------|------|
| `bridge/parser.py` | `_response_region` 의 승인 경계 감지를 `is_approval(text)` + tail 60 으로 게이트 | [fixes.md §Fix1](fixes.md#fix-1--bridgeparserpy-_response_region-가드) |
| `bridge/receiver.py` | `cmd_esc` 가 `bridge.awaiting_approval=False` 로 복원 | [fixes.md §Fix2](fixes.md#fix-2--bridgereceiverpy-cmd_esc-의-상태-복원) |
| `bridge/core.py` | `_flush_completed` 에 `FLUSH-EMPTY` 관측 로그 1줄 추가 (조건 강화) | [fixes.md §Fix3](fixes.md#fix-3--bridgecorepy-관측성-보강) |
| `tests/test_parser.py` | stale approval pane 에서도 신규 `⏺` 블록 추출되는지 | [test-plan.md](test-plan.md) |
| `tests/test_receiver.py` | `cmd_esc` 후 `awaiting_approval=False`, `queue.idx` 불변 | [test-plan.md](test-plan.md) |

## 작업 순서 (체크리스트)

- [ ] Fix 1 적용 (`bridge/parser.py` `_response_region` 가드)
- [ ] Fix 1 회귀 테스트 추가 (`tests/test_parser.py`) — stale approval + 중첩 모달
- [ ] Fix 2 적용 (`bridge/receiver.py` `cmd_esc` 상태 복원)
- [ ] Fix 2 회귀 테스트 추가 (`tests/test_receiver.py`) — `awaiting_approval=False`, `queue.idx` 보존
- [ ] Fix 3 적용 (`bridge/core.py` `_flush_completed` 관측 로그) — `slip is not None` AND 조건
- [ ] 기존 테스트 회귀 확인 (`source venv/bin/activate && python -m pytest tests/ -q`)
- [ ] 커밋 메시지 초안 제시 (사용자 승인 전까지 커밋 금지, Co-Authored-By 금지)

## 리뷰 반영 요점 (원 BUG_REPORT 대비 변경)

| 항목 | 원 리포트 | 리뷰 후 |
|------|---------|--------|
| Fix 1 tail 60 게이트 | "방어선" 으로 포함 | 유지. 중첩 모달 회귀 테스트 추가로 보강 |
| Fix 2 `queue.reset()` | 포함하지 않음 | 유지. 다음 `on_message` 의 reset 이 충분하고 idle race 없음이 의도 |
| Fix 3 로그 조건 | `not new_blocks and log_tag == "response"` | **`slip is not None` AND** 추가 — 정상 take_new=0 과 구분 |

## 범위 밖 (scope creep 금지)

- 경계 감지 통합 (`boundary_from_live_box` 헬퍼 도입)
- 상태 머신 일원화 (`Bridge.set_awaiting` / `clear_awaiting`)
- Delta-only 리빌딩 (아키텍트 옵션 A)

위 3건은 `docs/reviews/20260418-esc-flush-recovery/` 에 기록된 권고로 **별도 작업**으로 추적.

## 커밋 정책

- `CLAUDE.md` 규칙: **사용자 지시 없이 커밋 금지**. Co-Authored-By 트레일러 금지.
- 한 작업 단위 하나의 커밋 또는 Fix 별 3 커밋 중 사용자 선택.

## 참고

- 상세 diff: [fixes.md](fixes.md)
- 사이드이펙트와 대응: [side-effects.md](side-effects.md)
- 테스트 상세: [test-plan.md](test-plan.md)
