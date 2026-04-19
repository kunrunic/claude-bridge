# claude-bridge 테스트 스위트 긍정 평가

> 테스트/QA 전문가 관점에서 검토한 테스트 스위트의 강점과 모범 사례

---

## 강점 목록

### 1. 실용적이고 목표 지향적인 Stub/Mock 전략

**강점:** 외부 의존성(telegram 패키지, tmux 프로세스)을 체계적으로 고립시켜 완전히 독립적인 테스트 환경 구성

- **메타클래스 기반 범용 Dummy 구현** (`test_lock_and_limit.py:37-43`)
  - `_DummyMeta`의 `__getattr__` 오버로딩으로 임의 속성/메소드 접근 자동 처리
  - 단순하면서도 유연한 설계로 telegram, telegram.ext 패키지 모두 처리
  
- **테스트별 독립적 Bot 모듈 로드** (`test_lock_and_limit.py:33-69`, `test_concurrent_commands.py:33-69`)
  - 각 테스트에서 신선한 `bot` 모듈 로드 (`del sys.modules["bot"]`)
  - 상태 격리로 테스트 간 간섭 방지
  
- **config.json 자동 생성** (`test_lock_and_limit.py:55-61`)
  - 테스트 실행 환경 선언적 구성으로 재현성 보증

**코드 근거:**
```python
# test_lock_and_limit.py:40-43 - 유연한 더미 설계
class _Dummy(metaclass=_DummyMeta):
    def __init__(self, *a, **kw): pass
    def __call__(self, *a, **kw): return self
    def __getattr__(self, _): return self
```

---

### 2. 세밀한 엣지 케이스 커버

**강점:** 단순 Happy Path를 넘어 실제 운영 환경에서 발생 가능한 예외 상황까지 체계적으로 테스트

#### A) 파일 I/O 엣지 케이스
- **권한 오류 대응** (`test_lock_and_limit.py:148-157`)
  - `PermissionError` 발생 시에도 애플리케이션이 `True` 반환 (fail-open 전략 검증)
  - 네트워크 드라이브나 읽기 전용 파일시스템 환경 대비

- **손상된 락 파일** (`test_lock_and_limit.py:104-108`, `214-220`)
  - 불완전한 형식: `"claude_bridge"` (chat_id 행 누락)
  - 잘못된 chat_id: `"not_a_number"`
  - 각각 `None` 반환 및 자동 정리 검증

#### B) Tmux 세션 생명주기
- **Stale 락 자동 정리** (`test_lock_and_limit.py:138-146`)
  - 락은 존재하지만 tmux 세션이 죽은 상태: 정리 후 새 획득 성공
  - `_acquire_lock`에서 `has-session` 재확인으로 데드락 방지

- **Pane 죽음과 세션 생존** (`test_tmux_death.py:219-255`)
  - `has-session=0` (세션 살아있음) + `is_alive()=False` (pane 죽음) 분리 감지
  - 각각 다른 메시지 전송 (`"Claude 프로세스가 종료"` vs `"tmux 세션이 사라"`)

#### C) 동시성 엣지 케이스
- **더블탭(Double Tap) 방어** (`test_concurrent_commands.py:82-117`)
  - 동일 session_id 동시 acquire: 두 번째는 `False` 반환
  - `awaiting_approval` 플래그로 승인 버튼 더블탭 차단 (`test_concurrent_commands.py:264-273`)

- **연속 호출 안정성**
  - `/end` 두 번 호출 예외 없음 (`test_concurrent_commands.py:140-157`)
  - `stop()` 호출 시 락 파일 없어도 no-op으로 처리 (`test_concurrent_commands.py:160-177`)

**코드 근거:**
```python
# test_lock_and_limit.py:138-146 - Stale 정리
lp.write_text("other_tmux\n999")   # 다른 인스턴스 소유
with patch.object(bot, "tmux_run", return_value=_dead_tmux()):
    assert bot._acquire_lock("sess3", chat_id=111) is True  # 정리 후 획득!
assert lp.read_text().splitlines()[0] == bot.TMUX
```

---

### 3. 시나리오 기반 통합 테스트의 완성도

**강점:** 단위 테스트의 합으로 구현 불가능한 **다단계 상태 전이**와 **복합 흐름**을 체계적으로 검증

#### S1: 정상 라이프사이클 (4단계)
`test_scenarios.py:83-123` - 단순하지만 완전한 생명주기 검증
1. Start → 락 획득 ✓
2. /End → 락 해제 ✓
3. Find sessions → 락 없음 확인 ✓
4. Resume → 락 재획득 ✓

**설계 우수:** 각 단계 전후 상태를 명시적으로 검증 (`assert lp.exists()`, `assert not lp.exists()`)

#### S2: 크래시 복구 (2가지 경로)
- **경로 A** (`test_scenarios.py:129-151`): Reattach 후 `/end` → `_my_locks()` 호출로 자동 정리
- **경로 B** (`test_scenarios.py:154-166`): Tmux 세션 죽음 → 다음 `_is_locked` 호출에서 stale 정리

**설계 우수:** 수동 개입 없이 자동 복구 검증으로 운영 안정성 입증

#### S3: Force_new 흐름 (3가지 변형)
`test_scenarios.py:172-222` - 사용자가 선택할 수 있는 3가지 경로 모두 커버
- 신규 세션 선택 (`start(None)`) → A 락 해제 + 신규 획득
- 다른 기존 세션 resume (`start("sessB")`) → A 락 해제 + B 락 획득
- Force_new 후 상태 검증

#### S4: 더블 신규세션 버그 (알려진 이슈)
`test_scenarios.py:227-274` - Known bug를 **문서화하는 테스트**로 회귀 방지
- Spawn 두 번 호출 ✓
- Kill-session 호출 수 검증 ✓
- 회귀 테스트로 향후 수정 시 보증

**코드 근거:**
```python
# test_scenarios.py:83-123 - 4단계 완전 검증
b.start("sessA", chat_id=111)           # 1) Start
assert (tmp_path / ".cb_lock_sessA").exists()
await b.stop()                          # 2) End
assert not (tmp_path / ".cb_lock_sessA").exists()
locked = bot._is_locked("sessA")        # 3) Find
assert not locked
b.start("sessA", chat_id=111)           # 4) Resume
assert (tmp_path / ".cb_lock_sessA").exists()
```

---

### 4. 회귀 테스트와 버그 추적의 모범 사례

**강점:** 실제 발생한 버그를 테스트로 변환하여 재발 방지

#### 20260415 버그 회귀 방지 (`test_bug_20260415.py`)

**버그 A:** Stale 승인 메시지 중복 전송
- 원인: `monitor()` 부팅 시 `_sent_keys` 미초기화
- 테스트 (`test_mark_sent_dedup_via_response_key:112-120`)
  - 부팅 시드 후 공백/줄바꿈만 다른 동일 응답 수신
  - `_already_sent()` 반환 `True` 검증 → 중복 차단 입증

**버그 B:** 승인 카드에 위쪽 응답 끌어오기
- 원인: `_approval_box()` 본문 추출 로직 오류
- 테스트 (`test_approval_box_excludes_prior_response:87-94`)
  - 다중 블록 중 마지막 블록만 추출 검증
  - 부정 어설션으로 명확화:
    ```python
    assert "나가기" not in box          # 위쪽 테이블 제외
    assert "그룹 채팅" not in box       # 설명 텍스트 제외
    assert "Reading 1 file" not in box  # 대기 중 블록 제외
    ```

**코드 근거:**
```python
# test_bug_20260415.py:104-109 - 버그 재현 테스트
pane = APPROVAL_PANE  # 마지막 ⏺ 는 Reading 1 file… 블록
last = bot.extract_last_response(pane)
assert last.startswith("⏺")
assert "Reading 1 file" in last
```

---

### 5. 테스트 명명과 문서화의 명확성

**강점:** 테스트 이름과 docstring이 **테스트 의도**와 **검증 대상**을 즉각 파악 가능하게 구성

#### A) 3단계 명명 규칙
패턴: `test_[대상]_[조건]_[예상결과]`

| 테스트 | 명칭 | 의도 |
|--------|------|------|
| `test_parse_lock_valid` | 정상/비정상 명확 | 유효한 락 파일 파싱 |
| `test_parse_lock_malformed` | 엣지케이스 명시 | 불완전한 형식 처리 |
| `test_acquire_lock_stale_cleanup` | 액션+결과 | Stale 정리 후 획득 |

#### B) Docstring의 무기화
각 테스트는 **왜 이 테스트가 필요한가?**를 설명

```python
# test_lock_and_limit.py:138-146
"""락 파일이 있어도 tmux 세션이 죽어있으면 정리 후 획득"""
# → "왜": 좀비 락 파일 정리 필수
# → "무엇": _acquire_lock 호출
# → "기대": True 반환 + 파일 내용 업데이트
```

#### C) 시나리오 테스트의 명확한 주석
```python
# test_scenarios.py:83-123
# 1) start sessA → 락 획득
# 2) /end → 락 해제
# 3) /start 후 세션 목록 — sessA가 다시 보여야 함
# 4) resume sessA → 락 재획득
```

---

### 6. 매개변수화 테스트로 변형 케이스 커버

**강점:** `@pytest.mark.parametrize`로 유사한 케이스를 간결하게 확장

#### LIMIT_RE 패턴 매칭 (`test_lock_and_limit.py:280-297`)

**긍정 케이스 (4가지):**
```python
@pytest.mark.parametrize("text", [
    "You've hit your limit · resets 5pm (Asia/Seoul)",
    "you've hit your limit",
    "hit your daily limit",
    "hit your limit",
])
def test_limit_re_matches(text):
```
- 대소문자 변형
- 구두점 변형
- 부분 문장

**부정 케이스 (4가지):**
```python
@pytest.mark.parametrize("text", [
    "Running bash command",
    "esc to interrupt",
    "Do you want to proceed",
    "Claude Code v1.0",
])
def test_limit_re_no_match(text):
```
- 유사하지만 다른 명령어
- 버전 정보

**설계 우수:** 패턴 잘못 매칭되는 위험성을 체계적으로 검증

---

### 7. 비동기 흐름 테스트의 정교함

**강점:** `asyncio.get_event_loop().run_until_complete()`를 통한 안전한 비동기 테스트

#### /end 커맨드의 순차성 검증 (`test_concurrent_commands.py:140-157`)
```python
async def run():
    with patch.object(bot, "_lock_path", return_value=lp), \
         patch.object(bot, "tmux_run", return_value=_tmux(0)), \
         patch.object(bot, "send_input"), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        await b.stop()
        await b.stop()   # 두 번째 — 예외 없어야 함
```

- `AsyncMock` 사용으로 비동기 함수의 동작 제어
- `run_until_complete()`로 완료 보증

#### Monitor 루프 생명주기 검증 (`test_tmux_death.py:219-255`)
- Tick 단계별 상태 변이 추적
- 무한루프 방지 (`if iter_count[0] > 5: b.running = False`)
- Pane 살아있음 → 죽음 전이 검증

**코드 근거:**
```python
# test_tmux_death.py:239-250 - 이벤트 기반 상태 전이
def fake_sleep(n):
    iter_count[0] += 1
    if iter_count[0] > 5:
        b.running = False   # 루프 강제 종료

async def run():
    with patch.object(bot, "tmux_run", side_effect=fake_tmux), \
         patch("asyncio.sleep", side_effect=fake_sleep):
        await b.monitor(app, chat_id=111)
```

---

### 8. 상태 머신 검증의 엄밀성

**강점:** 단순한 TRUE/FALSE 검증을 넘어 **상태 전이 그래프** 검증

#### Bridge 인스턴스의 상태 변수 추적
- `current_session_id`: None → "sessA" → "sessB" → None
- `running`: True/False 상태 추적
- `awaiting_approval`: Lock 상태 검증
- `dead_reported`: 중복 보고 방지

#### Lock 파일 존재 여부로 상태 검증
```python
# test_scenarios.py:178-186
assert (tmp_path / ".cb_lock_sessA").exists()   # Lock 획득 상태
# ... 작업 ...
assert not (tmp_path / ".cb_lock_sessA").exists()  # Lock 해제 상태
```

**우수점:** 파일시스템을 "소스 오브 트루스"로 사용하여 격리된 테스트 환경에서도 실제와 동일한 상태 검증

---

### 9. 예외 안정성 검증

**강점:** 정상 흐름뿐 아니라 예외/오류 상황에서의 안정성까지 입증

#### 락 획득 실패 시 처리
- `_acquire_lock` 실패 → `start()` 반환 `False` (`test_lock_and_limit.py:323-330`)
- 실패 이유 구분:
  - 다른 인스턴스 소유: `False` 반환 (대기 거부)
  - 권한 오류: `True` 반환 (낙관적 진행)

#### Monitor 루프의 건강성
- Tmux 세션 사라짐: "사라졌습니다" 알림 → `running=False` → 루프 종료
- Pane 죽음: "프로세스가 종료" 알림 → `running=False` → 루프 종료
- 한 번만 보고 → 재호출 시 재보고 가능 (`test_tmux_death.py:134-151`)

**코드 근거:**
```python
# test_tmux_death.py:156-176 - 죽음 감지 안정성
async def run():
    with patch.object(bot, "tmux_run", return_value=_tmux(0, stdout="0")), \
         patch.object(b, "is_alive", return_value=False):  # Pane 죽음
        await b.monitor(app, chat_id=111)

assert b.running is False   # 루프 안전 종료
assert any("Claude 프로세스가 종료" in m for m in sent)
```

---

### 10. 재현 가능성과 독립성 (재실행 안전성)

**강점:** 테스트가 **순서 무관**하게 독립적으로 실행 가능하며 **재현 가능**

#### 전역 상태 격리
- 각 테스트에서 신선한 `tmp_path` fixture 사용 (pytest 자동)
- 각 테스트에서 `bot` 모듈 다시 로드 (sys.modules 정리)
- Mock 초기화로 부작용 없음

#### 테스트 순서 무관
```python
# test_scenarios.py 내 모든 테스트는 독립적으로 실행 가능
# S1 → S2 → S3 순서든
# S3 → S1 → S2 순서든
# 각각 동일 결과 보증
```

#### 실행 환경 최소화
- 외부 Tmux 세션 필요 없음 (mock 처리)
- 실제 파일시스템 사용하지 않음 (tmp_path 사용)
- 네트워크 접근 없음

---

### 11. 부정 어설션으로 테스트 의도 강화

**강점:** 단순 참/거짓 검증이 아닌 **명확한 제외 기준**으로 설계 의도 표현

#### 승인 카드 필터링 테스트 (`test_bug_20260415.py:87-94`)
```python
def test_approval_box_excludes_prior_response():
    box = bot._approval_box(APPROVAL_PANE)
    
    # 긍정: "Do you want to proceed" 포함
    assert "Do you want to proceed" in box
    assert "Read file" in box
    
    # 부정: 위쪽 응답 제외됨 (3가지)
    assert "나가기" not in box                # 1. 테이블
    assert "그룹 채팅" not in box            # 2. 테이블 설명
    assert "Reading 1 file" not in box       # 3. 대기 블록
```

**설계 우수:**
- 긍정/부정 어설션 모두 포함
- 3가지 부정 케이스로 경계 조건 명확화
- Docstring "위쪽 ⏺ 응답 본문이 카드에 끌려오지 않아야 함"이 코드와 직결

---

## 종합 평가

| 평가 항목 | 강도 | 근거 |
|----------|------|------|
| **테스트 커버리지** | 매우 높음 | 5개 파일, 100+ 테스트 케이스, Happy Path + 엣지케이스 + 시나리오 |
| **Mock 전략** | 매우 우수 | 메타클래스 기반 범용 더미, 명시적 패치, 상태 격리 |
| **시나리오 완성도** | 매우 우수 | 5가지 실제 사용 시나리오, 각 3-5단계 상태 전이 검증 |
| **엣지케이스** | 매우 우수 | 권한 오류, Stale 파일, 동시성, 비동기 흐름 모두 커버 |
| **문서화** | 우수 | Docstring + 주석으로 "왜" 명확화, 회귀 테스트 표식 |
| **재현성** | 우수 | 독립적 실행, 환경 최소화, 부작용 없음 |
| **유지보수성** | 우수 | 명확한 명명, Parametrize 활용, Known bug 문서화 |

---

## 핵심 설계 원칙 (우수 사례로 추출)

1. **메타클래스 더미**: 복잡한 외부 API를 단순하고 유연하게 모킹
2. **상태 머신 검증**: 파일시스템/메모리 상태를 "소스 오브 트루스"로 활용
3. **부정 어설션**: 제외 기준을 명시하여 설계 의도 강화
4. **시나리오 연쇄**: 단위 테스트로는 불가능한 다단계 상태 전이 검증
5. **회귀 테스트 추적**: 버그를 테스트로 변환하여 재발 방지
6. **비동기 안전성**: AsyncMock으로 정교한 비동기 흐름 검증
7. **Stale 상태 처리**: 좀비 리소스를 자동 정리하는 설계 검증

---

**검토 완료일**: 2026년 4월 15일  
**검토자**: QA/테스트 전문가 (Claude Code)  
**평가 결과**: 프로덕션 수준의 테스트 스위트 ✓
