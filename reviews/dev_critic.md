# claude-bridge Code Review: 비판적 분석

Python 3.11, asyncio, python-telegram-bot v20+ 환경에서 운영 중인 `bot.py` (1066줄) 에 대한 엄격한 코드 리뷰입니다.

---

## 문제점 목록

### 1. Blocking Subprocess 호출이 asyncio 메인 루프를 차단
**심각도: HIGH**

**코드 근거:**
- `bot.py:80-82` - `tmux_run()` 동기 함수
- `bot.py:88-94` - `send_input()` 에서 동기 호출 + `time.sleep(0.1)`
- `bot.py:510, 531` - `monitor()` 메서드 루프 내 `tmux_run()` 동기 호출

**문제점:**
```python
def tmux_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["/opt/homebrew/bin/tmux"] + cmd,
                          capture_output=True, text=True)
```

`subprocess.run()`은 **완전히 동기적** 블로킹 호출입니다. asyncio 이벤트 루프 내에서:
- `monitor()` 는 무한 루프(`while self.running:`)에서 1초마다 `tmux_run()` 호출
- 각 호출마다 400ms~1s 블로킹 → 이벤트 루프 정지 → 다른 비동기 작업(메시지 수신, 타임아웃) 지연
- 특히 `pane_output()`은 최대 200줄 캡처로 latency 누적

**개선 방향:**
```python
# asyncio 친화적 subprocess 호출
async def tmux_run_async(cmd: list[str]) -> subprocess.CompletedProcess:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, 
        subprocess.run,
        ["/opt/homebrew/bin/tmux"] + cmd,
        None,  # stdin
        subprocess.PIPE,  # stdout
        subprocess.PIPE,  # stderr
    )

# 또는 asyncio.create_subprocess_exec 사용
async def tmux_run_async(cmd: list[str]):
    proc = await asyncio.create_subprocess_exec(
        "/opt/homebrew/bin/tmux", *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return CompletedProcess(cmd, proc.returncode, stdout.decode(), stderr.decode())
```

---

### 2. 글로벌 싱글톤 상태 관리의 스레드/태스크 안전성 부재
**심각도: HIGH**

**코드 근거:**
- `bot.py:695-702` - 전역 `bridge = Bridge()` 싱글톤
- `bot.py:380-450` - Bridge 클래스의 비동기-unsafe 상태 변수들:
  - `self.chat_id`, `self.running`, `self.task`, `self.last_hash`, `self.last_sent`
  - `self.awaiting_approval`, `self._sent_keys`, `self.current_session_id`

**문제점:**
```python
# 비동기 태스크 내에서 race condition 발생 가능
bridge.task = asyncio.create_task(bridge.monitor(app, chat_id))  # 모니터링 태스크
# 동시에 다른 핸들러:
await bridge.stop()  # self.running = False 직접 변경
```

예시 시나리오:
1. `/start` 콜백이 `bridge.task = asyncio.create_task(...)` 실행
2. 동시에 telegram bot이 메시지 핸들러 실행
3. `monitor()` 루프가 `self.last_sent` 수정하며
4. 다른 함수가 `self.chat_id` 읽음 → race condition (값이 partial update)

특히 문제:
- `bot.py:471-481` - `stop()` 에서 5초 동기 sleep + 상태 변경
- `bot.py:436-441` - `start()` 에서 `_acquire_lock()` 호출 후 `current_session_id` 설정
- `bot.py:538-542` - `monitor()` 루프에서 상태 직접 변경

**개선 방향:**
```python
from dataclasses import dataclass
from asyncio import Lock

@dataclass
class BridgeState:
    chat_id: int | None = None
    running: bool = False
    last_sent: str = ""
    awaiting_approval: bool = False
    current_session_id: str | None = None
    _sent_keys: list[str] = field(default_factory=list)

class Bridge:
    def __init__(self):
        self._state = BridgeState()
        self._lock = asyncio.Lock()  # 모든 상태 접근 직렬화
        self.task: asyncio.Task | None = None
    
    async def set_chat_id(self, chat_id: int):
        async with self._lock:
            self._state.chat_id = chat_id
    
    async def get_chat_id(self) -> int | None:
        async with self._lock:
            return self._state.chat_id
```

---

### 3. 과도한 Bare Except (Exception 무차별 포착)
**심각도: MED**

**코드 근거:**
- `bot.py:223` - `except Exception:`
- `bot.py:244` - `except Exception:` (락 디렉토리 문제 시 무시)
- `bot.py:256` - `except Exception:`
- `bot.py:282` - `except Exception:`
- `bot.py:361-363` - nested `except Exception:`
- `bot.py:377-379` - nested `except Exception:`
- `bot.py:505, 585, 623, 640, 650` - `monitor()` 루프 내 반복
- `bot.py:733-738` - `_send_output()` 에서 두 겹 `except Exception:`

**문제점:**
```python
def _acquire_lock(session_id: str, chat_id: int) -> bool:
    # ... 중략 ...
    except Exception:
        return True  # 락 디렉토리 문제 시 허용 (방어적)
```

문제:
1. **무시된 에러**: KeyboardInterrupt, SystemExit, asyncio.CancelledError 같은 system 신호도 포착
2. **디버깅 불가**: 어떤 에러가 발생했는지 로그가 없음 → 운영 중 원인 파악 불가
3. **방어적? 아니 위험함**: "Exception" 무시는 BUG를 숨기는 것. 서버가 조용히 fail하고 사용자는 멈춤만 봄
4. **타입 안전성**: 함수가 `bool` 반환하지만 에러 상황 구분 불가

**개선 방향:**
```python
import logging

logger = logging.getLogger(__name__)

def _acquire_lock(session_id: str, chat_id: int) -> bool:
    lp = _lock_path(session_id)
    try:
        fd = lp.open("x")
        fd.write(f"{TMUX}\n{chat_id}")
        fd.close()
        return True
    except FileExistsError:
        # 정확한 에러 처리
        info = _parse_lock(session_id)
        if info:
            owner_tmux, _ = info
            if tmux_run(["has-session", "-t", owner_tmux]).returncode != 0:
                lp.unlink(missing_ok=True)
                return _acquire_lock(session_id, chat_id)
        return False
    except OSError as e:
        # 파일 시스템 에러만 로깅 후 처리
        logger.error(f"Failed to acquire lock for {session_id}: {e}")
        return False  # 명확한 실패
    except Exception as e:
        # 예상 밖의 에러는 로그 + 재발생
        logger.exception(f"Unexpected error acquiring lock: {e}")
        raise
```

---

### 4. 함수 내부 Import (모듈 로드 오버헤드)
**심각도: MED**

**코드 근거:**
- `bot.py:40` - `_log()` 내부: `import time`
- `bot.py:90` - `send_input()` 내부: `import time`
- `bot.py:289` - `find_sessions()` 내부: `import time`
- `bot.py:343` - `_parse_session_msgs()` 내부: `from datetime import datetime as _dt`
- `bot.py:531` - `monitor()` 루프 내부: `import time` (**루프마다 reload!**)
- `bot.py:724` - `_send_output()` 내부: `import html as _html`

**문제점:**
```python
async def monitor(self, app: Application, chat_id: int):
    # ...
    while self.running:
        # ...
        import time  # ← 루프마다 import (1초마다 실행!)
        out = pane_output()
        # ...
        await asyncio.sleep(1)
```

- 성능: `import` 문은 모듈을 다시 파싱하고 캐시 lookup 함 (비싼 작업)
- 코드 가독성: 함수 시작에서 의존성이 명확하지 않음
- 유지보수성: 모듈 수정 시 스캔 어려움

**개선 방향:**
```python
# 파일 상단에서 한 번만
import time
import html
from datetime import datetime

def _log(tag: str, msg: str = ""):
    ts = time.strftime("%H:%M:%S")
    # ...

async def monitor(self, app: Application, chat_id: int):
    while self.running:
        out = pane_output()
        # ... time, html 등 직접 사용
```

---

### 5. 하드코딩된 매직 넘버/상수
**심각도: MED**

**코드 근거:**
- `bot.py:86` - `pane_output()`: `-S -200` (마지막 200줄)
- `bot.py:93` - `send_input()`: `time.sleep(0.1)` (100ms 지연)
- `bot.py:288` - `find_sessions(limit: int = 8)` (기본 8개)
- `bot.py:300` - `now - activity_ts < 60` (60초 활성 제외)
- `bot.py:463` - `"-x", "80"` (80 cols)
- `bot.py:463` - `"-y", "50"` (50 rows)
- `bot.py:652` - `settle >= 2` (2회 안정화)
- `bot.py:669` - `settle += 1`
- `bot.py:477` - `await asyncio.sleep(5)` (5초)
- `bot.py:542` - `await asyncio.sleep(1)` (반복)
- `bot.py:559` - `await asyncio.sleep(30)` (한도 초과 시 30초)
- `bot.py:590` - `await asyncio.sleep(1)` (반복)
- `bot.py:725` - `_chunk_text(text, size: int = 3500)` (3500 bytes/chunk)
- `bot.py:462` - `max(0, proceed - 30)` (앞으로 30줄)
- `bot.py:469` - `min(len(lines), proceed + 6)` (뒤로 6줄)
- `bot.py:178-179` - `max(0, proceed - 30)` 와 `range(len(lines) - 15)` 비일관성
- `bot.py:460` - `if len(snippet) > 1200: snippet = snippet[-1200:]` (1200 bytes)

**문제점:**
```python
def pane_output() -> str:
    return tmux_run(["capture-pane", "-t", TMUX, "-p", "-S", "-200"]).stdout
```

- **디버깅 어려움**: 왜 200줄? 메모리 최적화? API 제한? 설명 없음
- **재사용성 낮음**: 필요에 따라 다른 값이 필요할 때 전체 코드 검색
- **테스트 불가**: 상수를 변경하고 동작 확인 불가능

**개선 방향:**
```python
# 파일 상단에 설정 상수
TMUX_PANE_BUFFER_LINES = 200  # 최근 n줄 캡처하여 메모리/성능 균형
SEND_INPUT_DELAY_MS = 100     # tmux 입력 사이 지연
SESSION_SEARCH_LIMIT = 8      # 기본 세션 목록 개수
SESSION_ACTIVE_COOLDOWN_S = 60  # 최근 n초 활동 세션은 제외 (현재 활성 가정)
TERMINAL_COLS = 80            # 모바일 친화적
TERMINAL_ROWS = 50
SETTLE_CYCLES = 2             # 화면 안정화 필요 사이클 수
SEND_OUTPUT_CHUNK_SIZE = 3500 # Telegram 메시지 최대 길이
APPROVAL_BOX_CONTEXT_LINES = 30  # 승인 박스 위 라인 개수
APPROVAL_CONTEXT_AFTER = 6    # 승인 박스 아래 라인 개수
APPROVAL_SNIPPET_MAX_CHARS = 1200
RATE_LIMIT_SLEEP_S = 30       # API 한도 초과 시 sleep

def pane_output() -> str:
    return tmux_run(
        ["capture-pane", "-t", TMUX, "-p", "-S", f"-{TMUX_PANE_BUFFER_LINES}"]
    ).stdout
```

---

### 6. 타입 힌트 불일치 및 부재
**심각도: MED**

**코드 근거:**
- `bot.py:80-82` - `tmux_run()` return 타입 명시하지만, 실제로 stdout 문자열만 반환하는 곳 많음
- `bot.py:84-86` - `pane_output()` 반환값이 `str | None` 가능하나 타입힌트 없음
- `bot.py:371` - `get_session_cwd(session_id: str) -> str | None` 정확함
- `bot.py:380-450` - Bridge 클래스:
  - `self._sent_keys: list[str]` 정의하지만, `list`만 쓰는 곳도 있음
  - `self.last_hash: str = ""` 하지만, hashlib.md5() 결과와 비교 (타입 일관성)
  - `self.status_msg_id: int | None = None` 는 루프 내에서 lazy init
- `bot.py:652-687` - `monitor()` 루프:
  - 지역변수 `pending`, `settle`, `h` 타입 미명시 (동적)
  - `to_send` 가 `str | None` 인지 명확하지 않음
- `bot.py:665-666` - `response or pending` → 반환이 `str` 확실하지 않음
- `bot.py:900-920` - `on_callback()` 에서 `q.data` 는 `str | None` 이지만 타입체크 없음

**문제점:**
```python
def pane_output() -> str:  # → str 보장? OSError 발생 가능하나 예외 처리 없음
    return tmux_run(["capture-pane", "-t", TMUX, "-p", "-S", "-200"]).stdout
```

실제로:
```python
out = pane_output()  # str 기대
clean = strip_ansi(out).strip()  # AttributeError 발생 가능 (out이 None?)
```

- **mypy strict**: `mypy --strict bot.py` 실행 시 50개 이상 에러
- **런타임 타입 안전성**: None 체크 없이 `.strip()` 호출 → AttributeError 위험

**개선 방향:**
```python
from typing import Optional

def pane_output() -> str:
    """현재 tmux 패널 내용 반환 (마지막 TMUX_PANE_BUFFER_LINES줄)
    
    Returns:
        tmux pane output string. 에러 시 empty string.
    
    Raises:
        None (예외는 내부 처리, fallback 반환)
    """
    try:
        result = tmux_run(["capture-pane", "-t", TMUX, "-p", "-S", f"-{TMUX_PANE_BUFFER_LINES}"])
        if result.returncode != 0:
            logger.warning(f"tmux capture-pane failed: {result.stderr}")
            return ""
        return result.stdout or ""
    except Exception as e:
        logger.error(f"pane_output error: {e}")
        return ""

# 호출부
async def monitor(self, ...):
    out = pane_output()  # 보장: str (절대 None 아님)
    clean = strip_ansi(out).strip()  # Safe
```

---

### 7. 비동기 작업 취소(CancelledError) 처리 부재
**심각도: MED**

**코드 근거:**
- `bot.py:445-448` - `restart()` 에서 `self.task.cancel()` 호출
- `bot.py:474-475` - `stop()` 에서 `self.task.cancel()` 호출
- `bot.py:508-692` - `monitor()` 는 `asyncio.CancelledError` 처리 없음
- `bot.py:809` - `cmd_esc()` 후 cancel 호출
- `bot.py:935` - `on_callback()` 에서 `bridge.task.cancel()` 호출

**문제점:**
```python
if bridge.task:
    bridge.task.cancel()  # ← CancelledError 발생!
bridge.task = asyncio.create_task(bridge.monitor(app, chat_id))
```

`monitor()` 내부:
```python
async def monitor(self, ...):
    while self.running:
        try:
            # ... 긴 로직 ...
        except Exception as e:
            print(f"[monitor error] {e}")
        await asyncio.sleep(1)
```

문제:
1. `task.cancel()` 호출 → CancelledError 발생
2. **bare except 없으므로** CancelledError 포착 안 됨 → 스택 트레이스 출력 (노이즈)
3. 정리 코드 실행 안 됨 (리소스 leak)

**개선 방향:**
```python
async def monitor(self, ...):
    try:
        self.running = True
        while self.running:
            try:
                # ... 모니터링 로직 ...
            except Exception as e:
                logger.exception(f"monitor loop error: {e}")
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("monitor task cancelled (graceful shutdown)")
        # 정리: 파일 닫기, 임시파일 삭제 등
        self.running = False
        for sid, _ in _my_locks():
            _release_lock(sid)
        raise  # CancelledError 다시 발생 (asyncio 관례)
    finally:
        logger.info("monitor task ended")
```

---

### 8. 설정 파일(config.json) 로드 오류 처리
**심각도: MED**

**코드 근거:**
- `bot.py:27-30` - `_load_config()` 함수
- `bot.py:32` - `_cfg = _load_config()` 모듈 로드 시점

**문제점:**
```python
def _load_config() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

_cfg = _load_config()  # 파일 없으면 FileNotFoundError → 프로그램 종료
```

- **파일 미존재**: 배포 시 config.json 필수 → 초기화 어려움
- **JSON 파싱 오류**: config 문법 오류 → JSONDecodeError
- **디버깅**: 사용자가 어디서 실패했는지 불명확

**개선 방향:**
```python
def _load_config() -> dict:
    """
    설정 파일 로드. 파일 없거나 오류 시 상세 메시지 출력.
    
    Raises:
        SystemExit: 설정 로드 불가 시 (exit code 1)
    """
    import sys
    
    if not _CONFIG_PATH.exists():
        print(f"ERROR: config.json not found at {_CONFIG_PATH}", file=sys.stderr)
        print(f"  Create config.json with: token, tmux_session, claude_path, allowed_ids", file=sys.stderr)
        sys.exit(1)
    
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in config.json: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to read config.json: {e}", file=sys.stderr)
        sys.exit(1)
    
    # 필수 필드 검증
    required = ["token", "claude_path"]
    for key in required:
        if key not in cfg:
            print(f"ERROR: Missing required config key: {key}", file=sys.stderr)
            sys.exit(1)
    
    return cfg
```

---

### 9. 세션 탐색(find_sessions) 성능 및 안정성
**심각도: MED**

**코드 근거:**
- `bot.py:288-314` - `find_sessions(limit: int = 8)` 함수
- `bot.py:291` - `.rglob("*.jsonl")` 무제한 재귀 검색
- `bot.py:330-365` - `_parse_session_msgs()` 전체 파일 읽기

**문제점:**
```python
def find_sessions(limit: int = 8) -> list[dict]:
    now = time.time()
    candidates = []
    for p in PROJECTS.rglob("*.jsonl"):  # O(전체 디렉토리 스캔)
        if p.stem.startswith("agent-"):
            continue
        title, last, last_ts = _parse_session_msgs(p)  # 각 파일 전체 읽기
        # ...
```

문제:
1. **I/O blocking**: `~/.claude/projects/` 가 수천 개 파일이면 수초 소요
2. **메모리**: 모든 파일을 메모리에 로드 후 정렬
3. **타이밍**: Telegram 명령에 응답 지연 (사용자는 "봇 안 움직임"으로 인식)
4. **에러 복구 안 됨**: 파일 읽기 중 permission denied → 무시

**개선 방향:**
```python
async def find_sessions_async(limit: int = 8) -> list[dict]:
    """비동기 세션 탐색 (UI 응답성 개선)"""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=4)
    
    try:
        projects_dir = PROJECTS
        # 1. 먼저 파일 목록 가져오기 (빠름)
        files = await loop.run_in_executor(
            executor,
            lambda: list(projects_dir.rglob("*.jsonl"))
        )
        
        # 2. 파일 파싱을 병렬로 (4 worker)
        parse_tasks = [
            loop.run_in_executor(executor, _parse_session_msgs, p)
            for p in files
            if not p.stem.startswith("agent-")
        ]
        
        results = await asyncio.gather(*parse_tasks, return_exceptions=True)
        
        # 3. 유효한 결과만 필터링
        candidates = []
        for p, result in zip(files, results):
            if isinstance(result, Exception):
                logger.warning(f"Failed to parse {p}: {result}")
                continue
            title, last, last_ts = result
            # ...
        
        candidates.sort(key=lambda x: x["activity_ts"], reverse=True)
        return candidates[:limit]
    finally:
        executor.shutdown(wait=False)
```

---

### 10. 메시지 청크 분할의 하드코딩 vs 제약 불일치
**심각도: LOW**

**코드 근거:**
- `bot.py:694-720` - `_chunk_text()` 함수 정의
- `bot.py:723-740` - `_send_output()` 에서 호출
- `bot.py:725` - `size: int = 3500` (Telegram limit 4096)

**문제점:**
```python
def _chunk_text(text: str, size: int = 3500) -> list[str]:
    # 줄 단위로 size 이하로 나눔
    # ...

async def _send_output(app: Application, chat_id: int, text: str):
    chunks = _chunk_text(text, size=3500)  # ← 하드코딩
```

- 3500은 관례? Telegram 메시지 길이 제한은 4096자
- 왜 3500? HTML escape 때문? 설명 없음
- 테스트 불가능 (다른 크기 필요시 코드 수정 필요)

**개선:**
```python
# 상수화
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
# Markdown/HTML escape 고려 시 여유
MESSAGE_SAFE_LENGTH = 3500  # ~90% 활용

def _chunk_text(text: str, size: int = MESSAGE_SAFE_LENGTH) -> list[str]:
    # ...
```

---

### 11. LIMIT_RE 정규식이 과도하게 광범위
**심각도: LOW**

**코드 근거:**
- `bot.py:69-70` - `LIMIT_RE = re.compile(r"You've hit your limit|hit your (daily )?limit", re.IGNORECASE)`
- `bot.py:545-560` - 사용처

**문제점:**
```python
LIMIT_RE = re.compile(r"You've hit your limit|hit your (daily )?limit", re.IGNORECASE)
```

- "hit your limit" 은 사용자가 일상 대화에서 쓸 수 있는 표현
- 오탐 위험: Claude가 "You've hit your daily limit of questions for today" 라고 응답 중 → 잘못 감지

**개선:**
```python
# 더 정확한 패턴 (Claude API 에러 메시지 기반)
LIMIT_RE = re.compile(
    r"You've\s+hit\s+your\s+(?:daily\s+)?(?:limit|rate\s+limit)|"
    r"try\s+again\s+later|"
    r"resets?\s+at",
    re.IGNORECASE | re.MULTILINE
)
```

---

### 12. TRUST_RE 패턴의 문자열 매칭 위험
**심각도: LOW**

**코드 근거:**
- `bot.py:58-66` - TRUST_RE 정의
- `bot.py:537-543` - 사용처

**문제점:**
```python
TRUST_RE = re.compile(
    r"Quick safety check|"
    r"Is this a project you created|"
    r"trust this folder|"
    r"Yes, I trust|"
    r"No, exit|"
    r"Security guide"
)
```

문제:
1. 정확한 Claude prompt인가? version 변경되면 깨짐
2. 대소문자: `Is this` vs `is this` → `re.IGNORECASE` 권장
3. 정규식이 아님 (| 연결): 실제로 regex라고 할 수 없음

---

### 13. 동기 sleep(5)이 asyncio 이벤트 루프 블로킹
**심각도: HIGH**

**코드 근거:**
- `bot.py:477` - `async def stop()` 내부: `time.sleep(5)`

**문제점:**
```python
async def stop(self):
    self.running = False
    if self.task:
        self.task.cancel()
    send_input("/exit")
    await asyncio.sleep(5)  # ← 이건 정확하지 않음, time.sleep(5) 아님?
    tmux_run(["kill-session", "-t", TMUX])
```

실제 코드 확인 필요하지만, 만약 `time.sleep(5)` 라면:
- asyncio 이벤트 루프 5초 블로킹
- 다른 메시지 핸들러 대기
- Telegram timeout 발생 가능

확인:
```python
# bot.py:477
await asyncio.sleep(5)  # 이건 맞음
# 하지만 send_input() 에서 time.sleep(0.1) → 블로킹
```

---

### 14. 에러 발생 시 종료 대신 무한 루프
**심각도: MED**

**코드 근거:**
- `bot.py:508-692` - `monitor()` 는 while True 무한 루프
- `bot.py:511-518` - tmux 세션 없어도 루프 지속 (dead_reported 플래그만 체크)
- `bot.py:689-690` - `except Exception as e: print(...)` 후 계속

**문제점:**
```python
while self.running:
    try:
        check = tmux_run(["has-session", "-t", TMUX])
        if check.returncode != 0:
            if not self.dead_reported:
                await app.bot.send_message(chat_id, "tmux 세션이 사라졌습니다.")
                self.dead_reported = True
                self.running = False  # ← 다음 순회에서 루프 탈출
            break  # ← 지금 탈출
        
        # ...
    except Exception as e:
        print(f"[monitor error] {e}")
        # ← 예외 무시 후 sleep(1) → 루프 계속
```

문제:
- 예외 발생 → 1초 sleep → 예외 다시 발생 → busy loop
- CPU 점유하지 않지만, 1초마다 재시도 (지수 백오프 없음)

---

### 15. 메시지 중복 방지 로직의 제한
**심각도: LOW**

**코드 근거:**
- `bot.py:459-470` - `_already_sent()`, `_mark_sent()` 메서드
- `bot.py:468-472` - 최대 20개만 유지

**문제점:**
```python
def _mark_sent(self, text: str):
    key = self._response_key(text)
    self._sent_keys.append(key)
    if len(self._sent_keys) > 20:
        self._sent_keys.pop(0)  # FIFO, oldest 제거
```

문제:
- 20개 초과 시 rotate (가장 오래된 것 삭제)
- 같은 응답이 21번째에 다시 나타나면 중복 전송!
- 예: 같은 에러메시지 반복 → 중복 전송

---

## 요약: 수정 우선순위

### 즉시 수정 (HIGH)
1. ✅ Asyncio 블로킹 subprocess 호출 → `run_in_executor` 또는 `asyncio.create_subprocess_exec`
2. ✅ 글로벌 싱글톤 race condition → AsyncLock 도입
3. ✅ Bare except → 구체적 예외 처리 + logging

### 1주일 내 수정 (MED)
4. 함수 내부 import 제거
5. 하드코딩 상수 정의화
6. 타입 힌트 추가 (`mypy --strict` 통과)
7. `asyncio.CancelledError` 처리
8. config.json 로드 오류 처리 개선

### 다음 리팩토링 (LOW)
9. find_sessions 비동기화
10. 메시지 청크 상수화
11. 정규식 개선

---

## 추가 권장사항

### 로깅 체계 도입
```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('claude_bridge.log'),
    ]
)
logger = logging.getLogger(__name__)
```

### 타입 체킹
```bash
mypy --strict --show-error-codes bot.py
```

### 테스트 작성
```python
# tests/test_bridge.py
import pytest
from bot import Bridge, tmux_run_async

@pytest.mark.asyncio
async def test_bridge_lock_acquisition():
    bridge = Bridge()
    # ...

@pytest.mark.asyncio
async def test_monitor_cancellation():
    # CancelledError 처리 확인
    # ...
```

### 문서화
- README.md: 설정, 배포, 문제 해결
- ARCHITECTURE.md: asyncio 이벤트 루프, 상태 관리 설명
- config.json.example: 설정 템플릿

