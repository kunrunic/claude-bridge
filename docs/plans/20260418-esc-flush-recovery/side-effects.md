# 사이드이펙트와 대응

3-에이전트 리뷰(architect / senior-engineer / chat-streaming-expert)에서 발굴된 리스크와 본 작업의 대응.

## Fix 1 — `_response_region` 가드

### 위험도 표

| # | 시나리오 | 위험도 | 본 작업 대응 |
|---|---------|-------|-------------|
| 1.1 | `is_approval` false-negative → 경계 truncation 스킵 → 실제 라이브 모달 아래에 blocks 가 섞임 | 저 | monitor 의 `is_approval` 분기(`core.py:438-460`)도 함께 False → pre-approval flush 도 스킵. **기존 대비 퇴보 없음** (원 리포트 line 207-211 분석). 추가 대응 없음. |
| 1.2 | 중첩 모달 (ESC 닫힌 구 모달 + 새 라이브 모달) — tail 60 안에 "Do you want to proceed" 2개 | 중 | 역방향 탐색이 **가장 최근 매칭**을 잡음. `test-plan.md` 의 nested-modal 케이스로 확증 |
| 1.3 | stale `end` 값 재계산 없음 — 게이트가 False 면 이전 tick 의 truncation 이 그대로? | 저 | `_response_region` 은 순수 함수 — 매 호출마다 `end = len(lines)` 로 초기화 후 조건부로만 당김 (`parser.py:219, 225`). **문제 아님** |
| 1.4 | 좁은 터미널 (<80컬럼) 에서 divider 가 10자 미만 → `_INPUT_DIVIDER_RE` 가 매칭 못해 `is_approval` false-negative | 저 | 현재 divider 폭은 10 이상 허용 (`parser.py:26`). 추가 대응 없음 |
| 1.5 | 기존 `tests/test_bug_20260418_010300.py` 의 Welcome 배너 truncation 테스트가 깨지는가? | 저 | Fix 1 은 경계 A 만 건드리고 경계 B/C(Welcome) 는 그대로. **회귀 없음**. `python -m pytest tests/ -q` 로 확증 |

### 기각된 대응

- **승인 경계를 `_response_region` 에서 완전히 제거**: busy-stream 중 승인 박스가 뜨면 승인 박스 상단까지만 송출해야 하므로 경계 truncation 자체는 필요. 제거 불가.
- **`is_approval` 의 tail 60 을 `_response_region` 경계 A 에도 그대로 적용 (게이트 없이)**: ESC 직후 tail 60 안에 stale 텍스트가 겹치는 시점이 여전히 존재 (2~3 tick 간격). 게이트 없이 tail 60 만으로는 불충분.

## Fix 2 — `cmd_esc` 상태 복원

### 위험도 표

| # | 시나리오 | 위험도 | 본 작업 대응 |
|---|---------|-------|-------------|
| 2.1 | ESC 직후 사용자가 과거 승인 메시지의 Yes/No 버튼 지연 클릭 → late-approved/denied 경로로 Claude 에 의도치 않은 텍스트 주입 | 저 | **기존 정책** (ESC 없이도 프롬프트 만료 시 발생 가능). 이번 Fix 로 새로 생기는 리스크 아님. 근본 대응 (승인 메시지 즉시 edit) 은 범위 밖 |
| 2.2 | `queue.reset()` 을 하지 않음 → ESC 후 monitor 가 이어서 flush 하는데 `idx` 가 이전 turn 의 값 | 저 | 의도적. 같은 turn 내 Claude 이어 응답 가능성 유지. `last_fp` 가 유효한 동안 slip 감지로 자연 복구 |
| 2.3 | `await update.message.set_reaction("⚡")` 가 Telegram 지연에 블로킹 → `/esc` 명령 긴 점유 | 저 | 기존 코드 그대로. Fix 2 는 reaction 호출 **앞에** 삽입되므로 지연 영향 없음 |
| 2.4 | `/esc` 가 동시에 여러 번 트리거 (사용자 연타) → 중복 `awaiting_approval=False` 설정 | 무 | 멱등 (bool 대입). 부작용 없음 |

### 기각된 대응

- **`queue.reset()` 추가**: 시니어 리뷰가 의문 제기했으나, 정상 turn 내 "ESC 후 이어받음" 케이스에서 `idx` 초기화는 재전송 리스크. 원 리포트의 "다음 `on_message` 가 자동 reset" 로직이 실제로 동작함 (`receiver.py:462, 479`).
- **ESC 시 텔레그램 승인 메시지 즉시 `edit_message_text("❌ 취소됨")`**: 근본 대응이지만 본 작업 범위 밖 — `docs/reviews/20260418-esc-flush-recovery/` 후속 권고로 기록.

## Fix 3 — 관측 로그

### 위험도 표

| # | 시나리오 | 위험도 | 본 작업 대응 |
|---|---------|-------|-------------|
| 3.1 | 정상 take_new=0 경로(이미 다 보낸 상태) 에서 로그 스팸 | 저 | `slip is not None` AND 조건 추가 — 이미 다 보낸 상태는 slip=None 이라 필터링됨 |
| 3.2 | busy-stream 도중 아직 성장 중 블록 하나뿐이면 take_new=0 발생 | 무 | `log_tag == "response"` 필터로 배제 (busy-stream 은 "busy-stream" 태그) |
| 3.3 | CB_DUMP 가 켜진 세션에서는 `flush_peek` 이벤트와 중복되어 보일 수 있음 | 무 | 포그라운드 로그와 dump 는 별개 채널. 중복은 의도된 용장 |

### 기각된 대응

- **모든 `take_new=0` 에 로그**: 정상 경로 스팸. 기각.
- **`dump.event("core", "flush_empty_alert")` 추가**: 이미 `flush_peek` 이 같은 정보 보유. 추가 불필요.

## 적용 후 회귀 확인 매트릭스

| 테스트 파일 | 기대 |
|-----------|------|
| `tests/test_parser.py` | 신규 케이스 포함 통과 |
| `tests/test_receiver*.py` | 신규 케이스 포함 통과, 기존 `cmd_esc` 테스트 유지 |
| `tests/test_core_monitor.py` | `awaiting_approval` 분기 변경 없음 — 무영향 |
| `tests/test_approval_reconnect.py` | `approve_yes/no` 경로 변경 없음 — 무영향 |
| `tests/test_bug_20260418_010300.py` | Welcome 배너 truncation 변경 없음 — 무영향 |
| `tests/test_bug_20260417_*.py` | 과거 pane-scrollback 오탐 계열 — Fix 1 이 덮어씀, 기존 검증도 유지되어야 |

실행: `source venv/bin/activate && python -m pytest tests/ -q`.

## 후속 (본 작업 범위 밖, 별도 추적)

1. **상태 머신 일원화** — `Bridge.set_awaiting(context, ...)` / `clear_awaiting(reason)` 헬퍼 도입. `awaiting_approval` 의 set/clear 경로 6곳 → 2곳으로.
2. **경계 감지 통합** — `boundary_from_live_box(text) -> int | None` 단일 함수로 승인/신뢰/resume picker 경계 판정 일원화.
3. **ESC 후 텔레그램 승인 메시지 edit** — 승인 메시지를 `❌ 취소됨` 으로 edit + 버튼 제거.
4. **Delta-only 아키텍처 검토** — `docs/reviews/20260418-esc-flush-recovery/architect.md` 의 옵션 A.

근거: [`../../reviews/20260418-esc-flush-recovery/`](../../reviews/20260418-esc-flush-recovery/README.md) 의 3명 공통 권고.

## 참고

- Fix 상세 diff: [fixes.md](fixes.md)
- 테스트: [test-plan.md](test-plan.md)
