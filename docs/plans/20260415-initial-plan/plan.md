# claude-bridge 구현 계획서

**작성일**: 2026-04-15  
**기준**: 오늘 세션 논의 + 12인 전문가 리뷰 (reviews/SUMMARY.md)

---

## 완료된 항목 (오늘 구현)

- [x] 사용량 한도 감지 (LIMIT_RE) + Telegram 알림 + 리셋 시각 파싱
- [x] 세션 락 시스템 (`_acquire_lock`, `_release_lock`, `_is_locked`, `_my_locks`, `_parse_lock`)
- [x] 락파일에 chat_id 저장 (소유권 추적)
- [x] `/unlock` Telegram 커맨드 (본인 chat_id 소유 락만 해제)
- [x] `stop.sh` 락 정리 (봇 종료 시 인스턴스 소유 락 전부 해제)
- [x] `restart.sh` 생성
- [x] `stop()` 슬립 1.5s → 5s
- [x] `stop()` `_my_locks()` 기반 전체 락 해제 (크래시 후 재연결 케이스 대응)
- [x] 테스트 스위트 76개 (단위/동시성/시나리오/tmux 죽음)
- [x] cb1(`claude-bridge`) ↔ cb2(`claude-bridge2`) 코드 동기화

---

## Phase 1 — 긴급 (HIGH)

### P1-1. tmux_run 비동기화
**문제**: `subprocess.run()` 동기 호출이 asyncio 이벤트 루프를 블로킹  
**구현**:
- `tmux_run()` 유지 (쉘 스크립트용 동기 버전)
- `tmux_run_async()` 추가: `asyncio.get_event_loop().run_in_executor()` + timeout=5.0
- monitor 루프 내 tmux 호출을 `await tmux_run_async()`로 교체
- spawn/kill 등 시작/종료 경로는 동기 유지 가능

### P1-2. Telegram Polling 재연결 루프
**문제**: 네트워크 오류 시 봇 좀비 상태 — 메시지 수신 불가  
**구현**:
- `run_polling()` 감싸는 exponential backoff 루프
- 최대 10회 재시도, 간격: 1s → 2s → 4s → ... → 60s 상한
- 10회 실패 시 `sys.exit(1)` (systemd/launchd 자동 재시작 트리거)
- 재연결 시 allowed_ids에 "재연결됐습니다" 알림

### P1-3. 타임아웃 전반 설정
**문제**: tmux 무한 대기, Telegram Request 타임아웃 없음  
**구현**:
- `subprocess.run(timeout=5.0)` — tmux_run 전체 적용
- `Application.builder().request(Request(connect_timeout=10, read_timeout=15))` 추가
- monitor 루프 `pane_output()` 호출에 timeout 감싸기

### P1-4. asyncio.Lock으로 Bridge 상태 직렬화
**문제**: `was_busy`, `awaiting_approval` 등 비동기-unsafe 상태 공유  
**구현**:
- `Bridge.__init__`에 `self._lock = asyncio.Lock()` 추가
- 상태 변경이 일어나는 핸들러(`on_callback`, `monitor`)에 `async with self._lock` 적용
- `awaiting_approval` 플래그 변경 구간 보호

### P1-5. Monitor CancelledError 명시 처리
**문제**: task.cancel() 후 CancelledError가 무시됨 → 리소스 leak  
**구현**:
```python
try:
    while self.running:
        ...
except asyncio.CancelledError:
    _log("MONITOR", "cancelled — cleanup")
    _release_lock(self.current_session_id)
    raise  # 반드시 re-raise
```

### P1-6. Monitor 연속 오류 카운터 + 알림
**문제**: 예외 무시 후 재시도 — 무한 silent failure  
**구현**:
- `error_count` 카운터, 정상 동작 시 리셋
- 5회 연속 → Telegram 경고
- 10회 연속 → Telegram 알림 + monitor 루프 종료

---

## Phase 2 — 승인 프롬프트 재연결 (신규 핵심 기능)

### P2-1. 승인 컨텍스트 저장
**구현**:
```python
# Bridge.__init__에 추가
self.last_approval_context: str = ""   # 도구명 + 요약
self.last_approval_full: str = ""      # pane 전체 내용 (재연결 시 전달용)
```
승인 감지 시 저장:
```python
self.last_approval_context = summarize_approval(clean)
self.last_approval_full = clean[-800:]   # 승인 박스 내용
```

### P2-2. approve_yes — 늦은 응답 처리
**구현** (`on_callback` → `approve_yes` 분기):
```
1. pane 확인: is_approval(pane_output()) ?
   → Yes: 기존 Enter 처리 (정상)

2. 프롬프트 없음 + is_alive() True:
   → Claude에 전송: "사용자가 '{context}' 작업을 승인했습니다. 이어서 진행해주세요."
   → 사용자에게: "✅ 승인 — 프롬프트가 만료되어 Claude에 재요청했습니다"

3. 세션 죽음 (is_alive() False):
   → bridge.start(current_session_id, chat_id) 로 --resume 재시작
   → 재시작 후 Claude에 전송: "사용자가 '{context}' 작업을 승인했습니다. 이어서 진행해주세요."
   → 사용자에게: "✅ 승인 — 세션 재시작 후 이어갑니다"
```

### P2-3. approve_no — 늦은 응답 처리
**구현** (`on_callback` → `approve_no` 분기):
```
1. pane 확인: is_approval(pane_output()) ?
   → Yes: 기존 Down+Enter 처리 (정상 거부)

2. 프롬프트 없음 + is_alive() True:
   → Claude에 전송: "사용자가 '{context}' 작업을 거부했습니다. 해당 작업을 취소하고 대기해주세요."
   → 사용자에게: "❌ 거부 — Claude에 취소 요청했습니다"

3. 세션 죽음 (is_alive() False):
   → Telegram 메시지:
     "세션이 종료되었습니다.
      마지막 작업: {context}
      이 세션을 다시 시작하시겠습니까?"
     [이 세션 재시작] [취소]
   → callback_data: "resume_after_no:{session_id}"
```

### P2-4. resume_after_no 콜백 처리
**구현**:
```python
elif data.startswith("resume_after_no:"):
    session_id = data[len("resume_after_no:"):]
    ok = bridge.start(session_id, chat_id=q.message.chat_id)
    if ok:
        bridge.task = asyncio.create_task(bridge.monitor(...))
        await ctx.bot.send_message(chat_id, "세션을 다시 시작했습니다.")
    else:
        await ctx.bot.send_message(chat_id, "재시작 실패")
```

---

## Phase 3 — 코드 품질 (MED)

### P3-1. 오류 메시지 UX 개선
- "⚠️ 이 세션은 다른 인스턴스에서 사용 중입니다." → "⚠️ 이미 사용 중인 세션입니다. /unlock으로 해제 후 다시 시도하세요."
- 잠금 오류 시 `/unlock` 인라인 버튼 제공

### P3-2. Bare except → 구체적 예외 + 로깅
- `except Exception as e: pass` → `except Exception as e: _log("ERROR", str(e))`
- 핵심 경로(monitor, tmux_run, lock)에 우선 적용

### P3-3. 하드코딩 상수 정의
```python
# bot.py 상단
TMUX_SCROLL_LINES   = 200
APPROVAL_SCAN_LINES = 30
SETTLE_TICKS        = 2
MAX_SENT_HISTORY    = 20
STOP_WAIT_SEC       = 5
MAX_MSG_CHARS       = 3500
BUSY_CHECK_TAIL     = 20
```

### P3-4. Config 로드 검증
```python
def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        sys.exit(f"config.json 없음: {_CONFIG_PATH}")
    try:
        cfg = json.load(...)
    except json.JSONDecodeError as e:
        sys.exit(f"config.json 파싱 오류: {e}")
    for key in ("token", "claude_path", "allowed_ids"):
        if key not in cfg:
            sys.exit(f"config.json 필수 항목 누락: {key}")
    return cfg
```

### P3-5. 함수 내부 import → 파일 상단 이동
- `monitor()` 내부의 `import time` 제거 (상단으로)
- `find_sessions()` 내부의 `import time` 제거

### P3-6. CancelledError 전파 보장 (테스트 포함)
- 모든 `except Exception:` 블록에서 CancelledError가 묻히지 않도록

---

## Phase 4 — 테스트 개선

### P4-1. asyncio.get_event_loop() → asyncio.run() 마이그레이션
- `test_scenarios.py`, `test_concurrent_commands.py`, `test_tmux_death.py` 전체
- pytest-asyncio 도입 또는 helper 함수 통일

### P4-2. conftest.py 작성
```python
# tests/conftest.py
import pytest, sys

@pytest.fixture(autouse=True)
def clean_bot_module():
    yield
    sys.modules.pop("bot", None)   # 테스트 간 모듈 캐시 격리
```

### P4-3. P2 승인 재연결 테스트 추가
- `test_approval_reconnect.py`
  - 정상 (프롬프트 있음) Yes/No
  - 프롬프트 없음 + 살아있음 Yes/No
  - 세션 죽음 + Yes (자동 재시작)
  - 세션 죽음 + No (재시작 메뉴 → 선택)
  - resume_after_no 콜백 처리

---

## 구현 순서 요약

```
1단계 (P1)  : tmux_run_async → CancelledError → Lock → 오류카운터 → 폴링 재연결 → 타임아웃
2단계 (P2)  : 승인 컨텍스트 저장 → approve_yes 재연결 → approve_no 재연결 → resume_after_no
3단계 (P3)  : UX 메시지 → 상수 → Config 검증 → bare except → import 정리
4단계 (P4)  : 테스트 asyncio.run() 마이그레이션 → conftest → P2 테스트
```

---

## 파일별 변경 범위

| 파일 | 변경 내용 |
|------|---------|
| `bot.py` | P1-1~6, P2-1~4, P3-1~6 |
| `tests/test_scenarios.py` | P4-1 |
| `tests/test_concurrent_commands.py` | P4-1 |
| `tests/test_tmux_death.py` | P4-1 |
| `tests/conftest.py` | P4-2 (신규) |
| `tests/test_approval_reconnect.py` | P4-3 (신규) |
| `stop.sh` | 이미 완료 |
| `restart.sh` | 이미 완료 |
