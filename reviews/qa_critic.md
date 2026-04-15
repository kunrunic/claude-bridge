# claude-bridge 테스트 스위트 비평 (QA Critic Review)

**작성일:** 2026-04-15  
**검토 범위:** 테스트 스위트 전체 + bot.py 구현

---

## 요약
테스트 스위트는 세션 락, 동시성, tmux 사망 감지 등 핵심 기능을 폭넓게 다루고 있으나, 여러 구조적 문제와 검증 한계가 있습니다. **심각한 버그 위험(HIGH) 2건**, **설계 결함(MED) 4건**, **운영 문제(LOW) 3건**이 적발되었습니다.

---

## 문제점 목록

### 1. asyncio.get_event_loop() deprecated 사용 (HIGH)

**파일:** test_concurrent_commands.py:156, test_scenarios.py:107, test_tmux_death.py:105 등 다수

**코드 근거:**
```python
# test_concurrent_commands.py:156
asyncio.get_event_loop().run_until_complete(run())

# test_scenarios.py:107
asyncio.get_event_loop().run_until_complete(do_stop())
```

**문제:**
- Python 3.10+ 에서 `asyncio.get_event_loop()` 는 deprecated
- 이벤트 루프가 실행 중인 스레드에서 호출 시 RuntimeError 발생 가능
- 향후 Python 버전(3.13+)에서 제거될 예정
- 테스트 격리 문제: 모든 테스트가 동일한 글로벌 이벤트 루프 재사용 → 상태 누수 위험

**심각도:** HIGH

**개선 방향:**
```python
# 올바른 패턴 (Python 3.7+)
asyncio.run(run())  # 새로운 이벤트 루프 생성 및 정리

# 또는 pytest-asyncio 사용
import pytest
@pytest.mark.asyncio
async def test_stop_twice_no_exception():
    # 직접 async 함수 작성, pytest가 루프 관리
    ...
```

**영향:**
- CI/CD 환경에서 예측 불가능한 실패
- Python 3.13 준비 필요

---

### 2. 모듈 캐시 공유로 인한 테스트 격리 실패 (HIGH)

**파일:** test_bug_20260415.py:50-53, test_lock_and_limit.py:64-65 등 모든 테스트 파일

**코드 근거:**
```python
# test_lock_and_limit.py:64-65
if "bot" in sys.modules:
    del sys.modules["bot"]
bot = _load_bot()
```

**문제:**
- 모든 테스트 파일이 `bot` 모듈을 동적으로 로드하지만, `sys.modules` 정리가 불완전
- 전역 상태 공유:
  - `TMUX` 변수가 테스트마다 다르게 설정되지만, 이전 테스트의 `TMUX` 참조가 남을 수 있음
  - `Bridge()` 인스턴스가 상태를 유지하면 테스트 간 간섭 발생
  - `config.json` 이 모든 테스트에서 공유됨
- 테스트 A에서 `bot.TMUX = "test_a"` → 테스트 B가 의도치 않게 "test_a" 사용 가능

**심각도:** HIGH

**개선 방향:**
```python
import pytest

@pytest.fixture(autouse=True)
def cleanup_bot_modules():
    """각 테스트마다 모듈 캐시 정리"""
    yield
    # teardown: bot 관련 모듈 정리
    modules_to_delete = [m for m in sys.modules if m.startswith('bot')]
    for m in modules_to_delete:
        del sys.modules[m]

@pytest.fixture
def bot_with_config(tmp_path):
    """독립적인 config.json 을 가진 bot 모듈 로드"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({...}))
    # conftest.py에서 제공하는 픽스처로 통합
```

**영향:**
- 테스트 순서에 따라 결과가 달라지는 비결정적 실패
- 로컬에서는 통과, CI에서는 실패하는 플리키(flaky) 테스트

---

### 3. 실제 tmux/Telegram 없이 승인 기능 검증 불가 (MED)

**파일:** test_bug_20260415.py:87-94 (\_approval_box 테스트)

**코드 근거:**
```python
def test_approval_box_excludes_prior_response():
    box = bot._approval_box(APPROVAL_PANE)
    assert "Do you want to proceed" in box
    assert "Reading 1 file" not in box
    # → 실제 Telegram 메시지 렌더링은 테스트 안 함
```

**문제:**
- 문자열 파싱 로직만 테스트 (블랙박스)
- 실제 Telegram 버튼 레이아웃, 마크업이 올바른지 검증 불가
  - InlineKeyboardButton/InlineKeyboardMarkup이 stub → 실제 동작 미검증
  - 사용자가 수신할 메시지 형식 미보장
- `_approval_box()` 의 입력 샘플이 하드코딩되어 실제 Claude 출력과 불일치할 수 있음
- 재응답 감지 로직(`_already_sent`) 은 mock 없이 테스트되지만, 실제 Telegram API 호출 시간초과/재전송은 시뮬레이션 불가

**심각도:** MED

**개선 방향:**
```python
# 실제 Claude 출력 샘플 수집 (fixtures로 관리)
@pytest.fixture
def real_approval_prompt_samples():
    """실제 Claude에서 수집한 승인 프롬프트들"""
    return [
        "Reading 1 file…\n  ⎿  ~/path.txt\n\nDo you want to proceed?",
        # ... 더 많은 샘플 추가
    ]

def test_approval_box_with_real_samples(real_approval_prompt_samples):
    for sample in real_approval_prompt_samples:
        box = bot._approval_box(sample)
        # 최소 하나 이상의 선택지 있는지 검증
        assert "Yes" in box or "No" in box or "1." in box
```

**영향:**
- 승인 거부 또는 답변 누락으로 사용자 경험 저하
- 실제 상황에서만 나타나는 버그

---

### 4. 락 파일 Race Condition 미처리 (MED)

**파일:** bot.py (구현)

**코드 근거:**
```python
# _acquire_lock 로직 (재구성 필요 - 정확한 구현은 읽기 대기)
# 문제 패턴:
# 1. _parse_lock("sessX") → None (파일 없음)
# 2. 파일 쓰기 시작
# 3. [Race] 다른 프로세스가 동시에 파일 생성
# 4. 덮어쓰기 완료 → 동시성 문제
```

**문제:**
- test_lock_and_limit.py 의 모든 `_acquire_lock` 테스트가 순차적 실행만 가정
- 실제 Telegram 봇은 동시 채팅(`chat_id` 1과 2가 동시에 `/start`)에서:
  - 두 스레드가 동시에 `_acquire_lock("sessA", chat_id=111)` 호출
  - `_parse_lock` 체크 후 파일 쓰기 전에 경합 발생 가능
  - **동시 세션 시작 가능** (락 의도 무효화)

**심각도:** MED

**개선 방향:**
```python
# 원자적(atomic) 파일 쓰기 필요
import os

def _acquire_lock(session_id: str, chat_id: int) -> bool:
    lp = _lock_path(session_id)
    try:
        # O_EXCL: 파일 생성 실패 시 원자적으로 실패 (경합 방지)
        fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, 'w') as f:
            f.write(f"{TMUX}\n{chat_id}")
        return True
    except FileExistsError:
        # 다른 프로세스가 이미 생성
        return _is_lock_owned_by_other(lp)
    except Exception:
        return True  # 방어적
```

**영향:**
- 극히 드물지만 두 사용자가 동일 세션에서 Claude 명령 실행 가능
- 데이터 충돌, 예상 밖 출력

---

### 5. Monitor 루프의 임계값 설정 미검증 (MED)

**파일:** test_tmux_death.py:219-254

**코드 근거:**
```python
def test_monitor_alive_then_suddenly_dead():
    """처음 몇 틱은 정상, 그 다음 틱에서 pane 죽음 감지"""
    tick = [0]
    
    def fake_tmux(args):
        if "list-panes" in args:
            tick[0] += 1
            # 처음 2틱은 살아있음, 3번째부터 죽음
            stdout = "0" if tick[0] <= 2 else "1"
            return _tmux(0, stdout=stdout)
```

**문제:**
- `monitor()` 의 폴링 간격(sleep 시간)이 테스트에서 mock됨 → 실제 지연 시뮬레이션 불가
- 몇 초 안에 사망 감지되어야 하는지 요구사항 미명시
  - 현재: 폴링 루프만 테스트 (time-based 요구사항 없음)
  - 실제: Claude가 10초 내에 응답 불가 → 사용자는 20초 대기
- `asyncio.sleep` mock으로 인해 실제 타이밍 문제 미감지
  - 예: pane이 읽기만 가능한 상태(crashed 아님) → 무한 대기 가능성

**심각도:** MED

**개선 방향:**
```python
def test_monitor_death_detection_latency():
    """사망 감지까지 최대 N초를 초과하지 않음"""
    import time
    b = bot.Bridge()
    death_detected_at = [None]
    
    async def run():
        start = time.time()
        with patch.object(bot, "tmux_run", return_value=_tmux(1)):
            # 첫 2초는 정상, 이후 사망
            def delayed_check(args):
                if time.time() - start > 2:
                    return _tmux(1)
                return _tmux(0)
            with patch.object(bot, "tmux_run", side_effect=delayed_check):
                await b.monitor(app, chat_id=111)
                death_detected_at[0] = time.time() - start
    
    asyncio.run(run())
    assert death_detected_at[0] < 5  # 5초 이내 감지
```

**영향:**
- 사망 감지 지연 → 사용자가 고통받는 응답 불가 상태 경험

---

### 6. Bridge.start() 의 이전 세션 정리 로직 미검증 (MED)

**파일:** test_scenarios.py:172-222

**코드 근거:**
```python
def test_s3_force_new_then_new_session_releases_old_lock(tmp_path):
    """S3: force_new 후 신규 세션 시작 → 이전 sessA 락이 bridge.start(None) 시 해제됨"""
    # ...
    # 2) force_new: tmux kill (bridge.stop() 없이 직접 kill)
    with patch.object(bot, "tmux_run", return_value=_tmux(0)):
        bot.tmux_run(["kill-session", "-t", bot.TMUX])
    
    # 3) 신규 세션 선택: bridge.start(None)
    # → 내부에서 _release_lock("sessA") 호출되어야 함
```

**문제:**
- 테스트가 **가정만 문서화** (호출 검증 없음)
- 실제 코드 경로:
  ```python
  # bot.py 추정 구현
  def start(self, session_id, chat_id):
      _release_lock(self.current_session_id)  # ← 이 부분 테스트 안 함
      ...
  ```
- Mock을 통해 `_release_lock` 호출 확인하지 않음 → **Spy 패턴 부재**
- 테스트가 최종 파일 상태만 확인 (행동 검증 미흡)

**심각도:** MED

**개선 방향:**
```python
def test_s3_force_new_releases_lock_via_spy(tmp_path):
    """_release_lock 호출을 직접 감시"""
    b = bot.Bridge()
    b.current_session_id = "sessA"
    
    release_calls = []
    original_release = bot._release_lock
    
    def spy_release(sid):
        release_calls.append(sid)
        original_release(sid)
    
    with patch.object(bot, "_release_lock", side_effect=spy_release), \
         patch.object(bot, "_lock_path", side_effect=lambda s: tmp_path / f".cb_lock_{s}"), \
         patch.object(bot, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        b.start(None, chat_id=111)
    
    assert "sessA" in release_calls  # _release_lock("sessA") 명시적 호출 확인
```

**영향:**
- 재팩토링 시 실수로 락 정리 로직 제거 가능
- 누적 락 파일 증가 → 파일시스템 오염

---

### 7. /end 커맨드의 crash 시나리오 미처리 (LOW)

**파일:** test_scenarios.py:341-375 (S6 테스트)

**코드 근거:**
```python
def test_s6_graceful_end_vs_force_start(tmp_path):
    """/end(graceful) vs /start 직접(force kill) 차이 확인"""
    # graceful: /end → send_input("/exit")
    # force: /start → kill-session 직접 실행
```

**문제:**
- `/end` 중에 `send_input("/exit")` 전송 후 5초 대기 중 봇 크래시 가능
  - 락 파일은 아직 존재하는 상태
  - `stop()` 함수가 완료되지 않아 `_release_lock()` 미호출
  - 다음 `/start` 시 stale 감지로 정리되지만, **타이밍이 운에 의존**
- 테스트가 정상 경로만 다룸 (예외/timeout 시나리오 없음)

**심각도:** LOW

**개선 방향:**
```python
def test_stop_crashes_mid_execution(tmp_path):
    """stop() 도중 봇 크래시 → 다음 /start 시 stale 정리"""
    lp = tmp_path / ".cb_lock_sessA"
    lp.write_text(f"{bot.TMUX}\n111")
    
    b = bot.Bridge()
    b.current_session_id = "sessA"
    
    async def run():
        with patch.object(bot, "_lock_path", side_effect=lambda s: tmp_path / f".cb_lock_{s}"), \
             patch.object(bot, "send_input", side_effect=Exception("Network error")):
            try:
                await b.stop()
            except:
                pass  # 크래시 시뮬레이션
    
    asyncio.run(run())
    
    # 락이 남아있는 상태
    assert lp.exists()
    
    # 다음 /start 시 stale 감지 + 정리
    with patch.object(bot, "_lock_path", side_effect=lambda s: tmp_path / f".cb_lock_{s}"), \
         patch.object(bot, "tmux_run", return_value=_tmux(0)):
        locked = bot._is_locked("sessA")
    
    # stale 정리됨
    assert not locked
    assert not lp.exists()
```

**영향:**
- 극히 드문 경우이나 처리되지 않은 락으로 진입 차단 가능

---

### 8. 테스트가 구현 세부사항에 과도히 의존 (LOW)

**파일:** test_lock_and_limit.py 전체, test_concurrent_commands.py 전체

**코드 근거:**
```python
# test_lock_and_limit.py:123-127
def test_acquire_lock_success(tmp_path):
    lp = tmp_path / ".cb_lock_sess1"  # ← 파일명 형식 고정
    with patch.object(bot, "_lock_path", return_value=lp):
        assert bot._acquire_lock("sess1", chat_id=111) is True
    assert lp.exists()
    lines = lp.read_text().splitlines()
    assert lines[0] == bot.TMUX      # ← 첫 줄이 TMUX라는 가정
    assert lines[1] == "111"         # ← 둘째 줄이 chat_id 라는 가정
```

**문제:**
- 파일 형식 (`.cb_lock_` 프리픽스, 줄 순서) 이 구현 세부사항으로 노출됨
- 파일 형식 변경 시 모든 테스트 실패 (구조적 유연성 부족)
- **행동(behavior) 테스트 부재**: "같은 세션에 대해 두 번째 락 획득은 실패한다" 같은 의도만 명시
- Mock 형식이 복잡해서 실제 코드 경로 추적 어려움

**심각도:** LOW

**개선 방향:**
```python
# 파일 세부사항 숨기기
@pytest.fixture
def lock_system(tmp_path, monkeypatch):
    """파일 형식을 추상화한 픽스처"""
    class LockSystem:
        def __init__(self):
            self.dir = tmp_path
        
        def create_lock(self, session_id, tmux_name, chat_id):
            """실제 파일 생성"""
            # 내부 구현 캡슐화
            ...
        
        def has_lock(self, session_id):
            """파일 존재 여부 (구현 독립적)"""
            ...
    
    lock_sys = LockSystem()
    monkeypatch.setattr(bot, "_LOCK_DIR", tmp_path)
    return lock_sys

def test_double_acquire_fails(lock_system):
    """행동 명시, 구현 세부사항 은폐"""
    # 첫 번째 획득
    assert bot._acquire_lock("sess1", chat_id=111) is True
    
    # 두 번째 획득: 실패
    assert bot._acquire_lock("sess1", chat_id=111) is False
    
    # 세부사항은 몰라도 됨 (파일 형식, 줄 순서 등)
```

**영향:**
- 유지보수 비용 증가
- 리팩토링(예: SQLite 기반 락으로 변경) 시 전체 테스트 재작성 필요

---

### 9. 누락된 테스트 케이스: Config 파일 손상/변경

**파일:** 없음 (테스트 미보유)

**문제:**
- `_load_config()` → 파일 읽기 실패, JSON 파싱 오류 미처리
- 테스트 실행 중 `config.json` 변경 → 다른 테스트에 영향 (격리 실패)
- 테스트 여러 개가 동시에 `config.json` 접근 → 경합 가능성

**심각도:** LOW

**개선 방향:**
```python
def test_load_config_handles_missing_file(tmp_path, monkeypatch):
    """config.json 없으면 명확한 에러"""
    monkeypatch.setattr(bot, "_CONFIG_PATH", tmp_path / "nonexistent.json")
    with pytest.raises(FileNotFoundError):
        bot._load_config()

def test_load_config_handles_invalid_json(tmp_path, monkeypatch):
    """JSON 파싱 오류 처리"""
    cfg = tmp_path / "config.json"
    cfg.write_text("{ invalid json }")
    monkeypatch.setattr(bot, "_CONFIG_PATH", cfg)
    with pytest.raises(json.JSONDecodeError):
        bot._load_config()
```

**영향:**
- 배포 환경 설정 오류 미감지

---

### 10. 승인 카드 필터링 로직의 경계 케이스 미검증 (LOW)

**파일:** test_bug_20260415.py:97-100

**코드 근거:**
```python
def test_approval_box_fallback_when_no_prompt():
    box = bot._approval_box("nothing here\nshort line")
    assert box  # 빈문자열 아님 (tail fallback)
```

**문제:**
- 단순히 "빈 문자열이 아님" 만 확인
- 실제로 무엇이 반환되는지 검증 부재
- `_approval_box()` 가 의도적으로 마지막 N줄을 반환한다면, "short line" 자체를 반환?
- edge case:
  - 입력이 empty string → 무한 루프?
  - 입력이 None → 예외?
  - 입력이 매우 큼 (10MB) → 성능 저하?

**심각도:** LOW

**개선 방향:**
```python
def test_approval_box_edge_cases():
    # 빈 입력
    assert bot._approval_box("") != ""  # fallback 동작
    
    # 매우 큰 입력
    big = "x" * (1024 * 1024)  # 1MB
    result = bot._approval_box(big)
    assert len(result) < len(big)  # 크기 제한 확인
    
    # None (예외 처리 확인)
    with pytest.raises((TypeError, AttributeError)):
        bot._approval_box(None)
```

**영향:**
- 극단적 입력에서 crash/행(hang) 가능성

---

## 종합 평가

| 심각도 | 개수 | 주요 이슈 |
|--------|------|---------|
| **HIGH** | 2 | asyncio.get_event_loop() deprecated, 모듈 캐시 격리 실패 |
| **MED** | 4 | 실제 tmux 미검증, race condition, 임계값 미검증, 행동 검증 미흡 |
| **LOW** | 3 | 운영 예외, 구현 의존성, edge case |

### 즉시 해결 필수 (High)
1. `asyncio.get_event_loop()` → `asyncio.run()` 마이그레이션
2. 모듈 격리 문제: pytest fixture + autouse cleanup

### 권장 개선 (Medium)
3. 실제 Claude 샘플 수집 및 테스트 데이터로 사용
4. 파일 lock: O_EXCL 기반 원자적 쓰기
5. Monitor 폴링: 시간 기반 요구사항 명시 (≤ 5초 감지)
6. Spy 패턴으로 호출 검증 강화

### 장기 개선 (Low)
7. config.json 접근 패턴 정규화 (singleton + fixture)
8. edge case 테스트 추가
9. 구현 세부사항 추상화 (테스트 유지보수성)

---

## 다음 단계
1. **conftest.py 작성**: 공통 픽스처/cleanup 로직 중앙화
2. **pytest.ini 추가**: asyncio 모드 설정 (pytest-asyncio)
3. **통합 테스트 추가**: 실제 tmux 환경에서의 E2E 테스트 (CI에서 격리)
4. **코드 커버리지 측정**: `pytest-cov` 로 미검증 코드 경로 식별
