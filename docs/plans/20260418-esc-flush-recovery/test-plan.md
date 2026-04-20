# 테스트 계획

Fix 1/2/3 에 대한 회귀 테스트 추가. 기존 테스트가 깨지지 않아야 함.

## 실행 방법

```bash
source venv/bin/activate && python -m pytest tests/ -q
# 또는 타겟 파일만
python -m pytest tests/test_parser.py tests/test_receiver_logging.py -q
```

## 신규 테스트

### T1 — `tests/test_parser.py`: stale approval 을 무시한 응답 추출

**의도**: ESC 로 닫힌 승인 모달 텍스트가 scrollback 에 남아있어도 이후 `⏺` 블록이 `extract_response_blocks` 로 추출되어야 함.

```python
STALE_APPROVAL_PANE = """\
⏺ Bash(ls)

────────────────────────────────────────────────────────────────
 Bash command
   ls
 Do you want to proceed?
 ❯ 1. Yes
   2. No
 Esc to cancel · Tab to amend · ctrl+e to explain
────────────────────────────────────────────────────────────────
❯ 여기요?

⏺ 네, 여기 있습니다.

────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────
"""

def test_response_region_ignores_stale_approval_after_esc():
    blocks = parser.extract_response_blocks(STALE_APPROVAL_PANE)
    assert any("네, 여기 있습니다." in b for b in blocks), (
        "stale approval text should not truncate response region"
    )
```

**검증 포인트**:
- Fix 1 이전: `is_approval` 미사용 경계 truncation 으로 `end` 가 stale 모달 앞으로 당겨져 "네, 여기 있습니다." 가 추출 범위 밖 → 빈 리스트.
- Fix 1 이후: `is_approval(STALE_APPROVAL_PANE) == False` (tail 에 `❯ 1. Yes` 없음) → 게이트 차단 → 경계 truncation 스킵 → 정상 추출.

### T2 — `tests/test_parser.py`: 중첩 모달에서 라이브 경계 유지

**의도**: tail 60 안에 "Do you want to proceed" 가 2개 (구 모달 + 현 모달) 있어도 가장 최근 모달 기준으로 경계가 잡혀야 함.

```python
NESTED_APPROVAL_PANE = """\
⏺ Bash(ls)
────────────────────────────────────────────────────────────────
 Bash command
   ls
 Do you want to proceed?
 ❯ 1. Yes
   2. No
────────────────────────────────────────────────────────────────
❯ /esc

⏺ 네, 대기하겠습니다.

❯ rm -rf foo
⏺ Bash(rm -rf foo)
────────────────────────────────────────────────────────────────
 Bash command
   rm -rf foo
 Do you want to proceed?
 ❯ 1. Yes
   2. No
────────────────────────────────────────────────────────────────
"""

def test_response_region_nested_modal_uses_live_boundary():
    # 라이브 모달 상태 (tail 에 ❯ 1. Yes 존재) → 경계는 현재 모달 앞
    assert parser.is_approval(NESTED_APPROVAL_PANE)
    blocks = parser.extract_response_blocks(NESTED_APPROVAL_PANE)
    # "네, 대기하겠습니다." 는 과거 turn 의 응답 — 현재 user turn 의 ❯ rm -rf foo 이후만 추출되어야
    # → 이 샘플에서는 rm -rf foo 승인 직전까지의 ⏺ Bash(rm -rf foo) 블록만 나와야 함
    assert any("rm -rf foo" in b for b in blocks)
```

**검증 포인트**: Fix 1 이후 `range(len-1, tail_start-1, -1)` 의 역방향 첫 매칭이 **가장 최근** 모달을 잡음.

### T3 — `tests/test_receiver_logging.py` (또는 `tests/test_receiver.py` 신규): `cmd_esc` 가 `awaiting_approval` 복원

**의도**: `/esc` 호출 후 `bridge.awaiting_approval` 이 False 가 되고, `queue.idx` 는 **건드리지 않아야** 함.

```python
import asyncio
import bot  # re-export 로 모든 심볼 접근
from unittest.mock import AsyncMock, MagicMock, patch

def test_cmd_esc_clears_awaiting_approval_but_preserves_queue():
    bot.bridge.awaiting_approval = True
    bot.bridge.queue.idx = 5
    bot.bridge.queue.last_fp = "⏺ Bash(previous)"

    update = MagicMock()
    update.effective_chat.id = next(iter(bot.ALLOWED_IDS), 1)
    update.message.set_reaction = AsyncMock()
    ctx = MagicMock()

    with patch.object(bot.tmux, "tmux_run") as m_run, \
         patch.object(bot.tmux, "send_key") as m_sk:
        m_run.return_value.returncode = 0
        asyncio.run(bot.cmd_esc(update, ctx))

    assert bot.bridge.awaiting_approval is False
    assert bot.bridge.queue.idx == 5, "queue.idx should NOT be reset by /esc"
    assert bot.bridge.queue.last_fp == "⏺ Bash(previous)"
    m_sk.assert_called_once_with("Escape")
```

**검증 포인트**:
- `awaiting_approval` 복원 (Fix 2 직접 검증)
- `queue.idx`, `last_fp` 불변 (의도된 정책 — 같은 turn 유지)
- `send_key("Escape")` 호출 (기존 동작 보존)

### T4 — `tests/test_receiver_logging.py`: `cmd_esc` 가 세션 없을 때 조기 리턴

**의도**: 세션 없을 때 `awaiting_approval` 복원도 하지 않고 reply 만 보냄. (기존 동작 보존 검증)

```python
def test_cmd_esc_no_session_no_state_change():
    bot.bridge.awaiting_approval = True

    update = MagicMock()
    update.effective_chat.id = next(iter(bot.ALLOWED_IDS), 1)
    update.message.reply_text = AsyncMock()
    ctx = MagicMock()

    with patch.object(bot.tmux, "tmux_run") as m_run:
        m_run.return_value.returncode = 1  # has-session 실패
        asyncio.run(bot.cmd_esc(update, ctx))

    assert bot.bridge.awaiting_approval is True, "세션 없을 때는 상태 변경 없음"
    update.message.reply_text.assert_awaited_once()
```

### T5 — `tests/test_core_monitor.py` (선택): `FLUSH-EMPTY` 로그 조건

**의도**: Fix 3 로그가 `slip=None` 인 정상 take_new=0 경로에서는 **찍히지 않고**, slip 있을 때만 찍혀야 함.

```python
def test_flush_empty_log_only_on_slip(caplog):
    b = bot.Bridge()
    b.chat_id = 1
    b.queue.idx = 0
    b.queue.last_fp = ""

    app = MagicMock()
    app.bot = MagicMock()
    app.bot.send_message = AsyncMock()

    # 정상 경로: completed 가 비어있고 idx=0 → slip=None, new=[]
    asyncio.run(b._flush_completed(app, 1, "", include_last=True, log_tag="response"))
    assert "FLUSH-EMPTY" not in caplog.text

    # slip 시나리오: idx 가 completed 길이를 앞서감
    b.queue.idx = 3
    asyncio.run(b._flush_completed(app, 1, "⏺ foo", include_last=True, log_tag="response"))
    # completed=[⏺ foo] 길이 1 < idx 3 → slip="idx_past_cc"
    assert "FLUSH-EMPTY" in caplog.text
```

**주의**: `_log` 는 `print` 사용 → `caplog` 대신 `capsys` 또는 `_log` 자체를 patch 해야 함. 팀 컨벤션에 맞춰 조정.

## 기존 회귀 확인 범위

| 테스트 파일 | 확인 포인트 |
|-----------|------------|
| `tests/test_parser.py` | `extract_response_blocks`, `is_approval`, `_response_region` 기존 케이스 |
| `tests/test_bug_20260418_010300.py` | Welcome 배너 경계 C — Fix 1 은 경계 A 만 변경 → 무영향 |
| `tests/test_bug_20260417*.py` | 과거 pane-scrollback 오탐 계열 — 여전히 통과해야 |
| `tests/test_core_monitor.py` | `awaiting_approval` 분기 흐름 — Fix 2 는 monitor 외부 변경 |
| `tests/test_approval_reconnect.py` | `approve_yes/no` 경로 — 본 작업 미변경 |
| `tests/test_concurrent_commands.py` | `/esc` + 다른 명령 동시 — Fix 2 는 멱등이라 race 영향 낮음 |
| `tests/test_scenarios.py` | 전체 플로우 시나리오 |
| `tests/test_compaction_watchdog.py` | 압축/watchdog — 무관 |

## 실행 게이트

- [ ] 신규 T1, T2, T3, T4 통과
- [ ] 기존 `tests/` 전체 회귀 없음 (`-q` 출력에 red 0)
- [ ] T5 는 선택 — 추가 난이도 (caplog/capsys 중 팀 관례 확인 후)

## 참고

- Fix 본문: [fixes.md](fixes.md)
- 사이드이펙트 분석: [side-effects.md](side-effects.md)
