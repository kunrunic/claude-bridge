# claude-bridge 설계 비판 리뷰

**검토 대상**: Telegram ↔ Claude Code (tmux) 브리지 데몬  
**검토일**: 2026-04-15  
**평가 관점**: 설계 결함, 확장성 병목, 책임 분리, 글로벌 상태 관리

---

## 문제점 목록

### 1. CRITICAL: 멀티 인스턴스 인자 전달 설계 결함

**심각도**: HIGH

**코드 근거**:
- `start.sh:42-43`: INSTANCE_NAME을 argv로 전달
  ```bash
  INSTANCE_NAME=$(basename "$PWD")
  nohup python -u bot.py "$INSTANCE_NAME" >> "$LOG_FILE" 2>&1 &
  ```
- `bot.py:1043-1066`: `main()` 함수가 sys.argv를 수신하지 않음
  ```python
  def main():
      app = (Application.builder()...
      )
      app.add_handler(...)
      _log("BOOT", "claude-bridge started (Ctrl+C to quit)")
      app.run_polling(drop_pending_updates=True)
  
  if __name__ == "__main__":
      main()  # argv 전달 안 함
  ```

**문제점**:
- start.sh가 INSTANCE_NAME을 전달하지만 bot.py 코드에 수신 로직이 없음
- 멀티 인스턴스(cb1, cb2)가 서로 다른 tmux_session으로 분리되어야 하는데, 이 인자가 config.json의 `tmux_session` 값에 반영되지 않음
- 결과: 여러 폴더에서 bot.py를 실행해도 모두 동일한 tmux_session명("claude_bridge")을 사용할 가능성 → **인스턴스 간 세션 충돌 위험**

**개선 방향**:
```python
def main():
    import sys
    instance_name = sys.argv[1] if len(sys.argv) > 1 else None
    # config 로드 시 instance_name으로 tmux_session 오버라이드
    if instance_name:
        global TMUX
        TMUX = _cfg.get("tmux_session", "claude_bridge").replace("claude_bridge", instance_name)
        # 또는 더 명확하게: TMUX = f"cb_{instance_name}" if instance_name != "claude-bridge" else TMUX
```

---

### 2. CRITICAL: 글로벌 싱글톤 `bridge` 객체의 멀티 유저/멀티 세션 미지원

**심각도**: HIGH

**코드 근거**:
- `bot.py:695`: 전역 싱글톤
  ```python
  bridge = Bridge()
  ```
- `bot.py:435-441`: 모든 사용자가 같은 bridge 객체를 공유
  ```python
  def start(self, session_id: str | None = None, chat_id: int = 0) -> bool:
      if session_id and not _acquire_lock(session_id, chat_id):
          return False
      _release_lock(self.current_session_id)  # 이전 락 무조건 해제
      self.current_session_id = session_id
      ...
  ```

**문제점**:
- `self.chat_id`, `self.running`, `self.current_session_id` 등이 하나의 bridge만 관리 가능
- 여러 Telegram 사용자(각기 다른 chat_id)가 동일 인스턴스에 접속할 경우:
  - 사용자 A가 세션 X를 시작 → `bridge.current_session_id = X`
  - 사용자 B가 세션 Y를 시작 → `bridge.current_session_id = Y`
  - 사용자 A의 모니터 태스크가 여전히 Y를 모니터링 (또는 충돌)
- `/end` 명령 실행 시 전역 tmux 세션 종료 → 다른 사용자의 작업도 종료됨
- `self.awaiting_approval` 플래그가 전역 → 한 사용자의 승인 대기가 다른 사용자의 입력 처리를 블록

**설계 한계**:
- 현재 README에서 "여러 인스턴스 동시 운영" = 폴더 복제
- 같은 인스턴스에 여러 사용자 접근 시나리오는 **고려되지 않음**
- tmux가 1:1 (인스턴스:세션) 관계를 강제하기 때문

**개선 방향**:
```python
# 선택지 A: chat_id 별 bridge 인스턴스 풀
_bridge_pool: dict[int, Bridge] = {}

def get_bridge(chat_id: int) -> Bridge:
    if chat_id not in _bridge_pool:
        _bridge_pool[chat_id] = Bridge()
    return _bridge_pool[chat_id]

# 선택지 B: tmux 분리 (각 chat_id마다 별도 pane/window)
# 선택지 C: 설계 단순화 - "1 인스턴스 = 1 chat_id" 명시 & 타 사용자 거부
```

---

### 3. HIGH: 세션 락 시스템의 스테일 데이터 정리 부재

**심각도**: HIGH

**코드 근거**:
- `bot.py:237-242`: 락 파일 stale 감지는 acquire 시에만
  ```python
  try:
      fd = lp.open("x")
      ...
  except FileExistsError:
      info = _parse_lock(session_id)
      if info:
          owner_tmux, _ = info
          if tmux_run(["has-session", "-t", owner_tmux]).returncode != 0:
              lp.unlink(missing_ok=True)
              return _acquire_lock(session_id, chat_id)  # 재귀
      return False
  ```
- `bot.py:259-271`: `_is_locked()` 에서도 stale 정리하지만 일관성 없음
- `stop.sh:38-50`: 종료 시에만 이 인스턴스 소유 락 정리

**문제점**:
- **타 인스턴스가 비정상 종료되면** `~/.claude/.cb_lock_*` 파일이 남아있음
- 만약 부팅 직후 tmux 세션이 아직 살아있다면 (프로세스만 죽음) stale 정리가 실패
- `find_sessions()` 호출 시 `_is_locked()` 체크만 함 (락 파일 자동 정리 없음)
- **누적**: 좀비 락 파일이 점점 쌓여 특정 세션을 영구 잠금 가능

**개선 방향**:
```python
def cleanup_stale_locks():
    """주기적으로 실행: 모든 락 파일의 owner tmux 세션 존재 여부 확인"""
    for lf in _LOCK_DIR.glob(".cb_lock_*"):
        info = _parse_lock(lf.stem[len(".cb_lock_"):])
        if info:
            owner_tmux, _ = info
            if tmux_run(["has-session", "-t", owner_tmux]).returncode != 0:
                lf.unlink(missing_ok=True)
                _log("STALE-CLEANUP", f"removed {lf.name}")

# post_init() 또는 monitor() 시작 시 호출
```

---

### 4. HIGH: 책임 분리 위반 - bot.py가 tmux/프로세스 관리까지 담당

**심각도**: HIGH

**코드 근거**:
- `bot.py:80-98`: tmux 유틸 함수들이 bot.py에 인라인
  ```python
  def tmux_run(cmd: list[str]) -> subprocess.CompletedProcess:
      return subprocess.run(["/opt/homebrew/bin/tmux"] + cmd, ...)
  
  def pane_output() -> str: ...
  def send_input(text: str): ...
  def send_key(key: str): ...
  ```
- `bot.py:407-448`: Bridge 클래스가 tmux spawn/kill/restart 담당
  ```python
  def _spawn(self, session_id: str | None) -> bool: ...
  def start(self, session_id: str | None = None, ...) -> bool: ...
  def restart(self) -> bool: ...
  def is_alive(self) -> bool: ...
  ```
- `bot.py:484-692`: monitor 루프에서 화면 파싱, 정규식 매칭, 상태 추적

**문제점**:
- **단일 책임 원칙 위반**: bot.py가
  1. Telegram 봇 로직
  2. tmux 프로세스 관리
  3. tmux 화면 파싱 & 상태 머신
  4. 세션 락 관리
  를 동시에 담당
- 테스트 불가능 (tmux가 필수, subprocess 모킹 어려움)
- 버그 범위 파악 어려움 (어느 모듈이 실패했는지 추적 곤란)
- 유지보수성 저하 (1066줄이 한 파일에 뭉쳐 있음)

**개선 방향**:
```
bot.py
├── telegrambot.py      (Telegram API 통합, 핸들러)
├── tmux_bridge.py      (tmux 프로세스 관리)
├── session_monitor.py   (화면 파싱, 상태 머신)
├── lock_manager.py      (락 획득/해제/정리)
└── utils.py             (정규식, 로깅)
```

---

### 5. HIGH: 이미 처리된 응답을 재탐지할 가능성 (해시 기반 중복 방지의 한계)

**심각도**: HIGH

**코드 근거**:
- `bot.py:455-458`: 정규화 후 중복 판정
  ```python
  def _response_key(self, text: str) -> str:
      lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
      return "\n".join(lines)
  ```
- `bot.py:656-687`: 화면 변화 감지 & 2초 안정 후 전송
  ```python
  h = hashlib.md5(clean.encode()).hexdigest()
  if h != self.last_hash:
      self.last_hash = h
      pending = clean
      settle = 0
  else:
      settle += 1
  
  if pending and settle >= 2 and pending != self.last_sent:
      response = extract_last_response(pending)
      ...
  ```

**문제점**:
- MD5 해시는 화면 레이아웃 변화에만 반응 (내용 변화 감지 전)
- ANSI 색상 코드 제거 후 검사하지만, 실제 텍스트 내용이 같은데 **스크롤 위치만 변함** 경우 누락 가능
- `_already_sent()` 는 최근 20개만 기억 → 21번째 호출 때 중복 가능
- 예시:
  ```
  화면 1: [보충 설명]
  화면 2: [보충 설명] + [⏺ Claude 응답]  <- 처음 전송
  화면 3: [보충 설명] 스크롤됨 + [⏺ Claude 응답]  <- hash 다름, 재전송!
  ```

**개선 방향**:
```python
def _response_key(self, text: str) -> str:
    response = extract_last_response(text)
    if not response:
        return ""
    # 응답 블록 자체의 해시만 사용
    return hashlib.sha256(response.encode()).hexdigest()

def _already_sent(self, text: str) -> bool:
    key = self._response_key(text)
    # 최근 N개 대신 "현재 세션에서 본 모든 응답" 기억
    return key in self._sent_keys
```

---

### 6. MED: 레이스 컨디션 - bridge.running vs monitor task lifecycle

**심각도**: MED

**코드 근거**:
- `bot.py:871-883`: reattach 핸들러
  ```python
  if data == "reattach":
      await q.edit_message_text("기존 세션 재연결 중…")
      if bridge.task:
          bridge.task.cancel()
      bridge.task = asyncio.create_task(
          bridge.monitor(ctx.application, q.message.chat_id)
      )
  ```
- `bot.py:484-692`: monitor 루프
  ```python
  while self.running:
      try:
          ...
          if check.returncode != 0:
              ... self.running = False
              break
      except Exception as e:
          print(f"[monitor error] {e}")
      await asyncio.sleep(1)
  ```

**문제점**:
- 동시에 여러 핸들러가 `bridge.task.cancel()` 호출 가능 (race condition)
- `self.running = False` 후에도 task 취소 전 tmux kill 호출 시 경합
- `bridge.task = None` 초기화가 없음 → 이전 cancelled task 참조 유지

**개선 방향**:
```python
async def cancel_monitor_safely():
    if bridge.task and not bridge.task.done():
        bridge.task.cancel()
        try:
            await bridge.task
        except asyncio.CancelledError:
            pass
    bridge.task = None

# 모든 task 할당 전에 호출
if data == "reattach":
    await cancel_monitor_safely()
    bridge.task = asyncio.create_task(...)
```

---

### 7. MED: 정규식 매칭의 거짓 양성 (False Positives)

**심각도**: MED

**코드 근거**:
- `bot.py:58-75`: 각 정규식 독립적으로 검사
  ```python
  APPROVAL_RE = re.compile(
      r"Do you want to proceed|"
      r"Allow\s+\w+\s+to|Proceed\?|\(Y/n\)|\(y/N\)"
  )
  TRUST_RE    = re.compile(...)
  LIMIT_RE    = re.compile(r"You've hit your limit|...", re.IGNORECASE)
  BUSY_RE     = re.compile(r"esc to interrupt")
  ```
- `bot.py:545-560`: LIMIT_RE는 전체 화면에서 매칭
  ```python
  if LIMIT_RE.search(clean):
      if not self.limit_reported:
          reset_match = re.search(
              r"resets\s+(\d+(?::\d+)?(?:am|pm)?)\s*\(([^)]+)\)",
              clean, re.IGNORECASE
          )
  ```

**문제점**:
- `LIMIT_RE` = "You've hit your limit" → 로그, 히스토리, 다른 대화에서도 매칭 가능
- `\(Y/n\)` 패턴은 "Press (Y/n) to..." 같은 일반 메시지도 승인창으로 오인
- reset 정보 파싱이 실패하면 None인데, f-string에서 그냥 버림 → 사용자에게 "한도 초과" 만 보임
- ANSI 스트립 후에도 색상 코드 흔적이 일부 남을 수 있음 → 패턴 미스매치

**개선 방향**:
```python
def is_limit_hit(text: str) -> bool:
    """마지막 30줄에서만 매칭 (로그 필터링)"""
    tail = "\n".join(text.splitlines()[-30:])
    return bool(LIMIT_RE.search(tail))

def extract_reset_info(text: str) -> str | None:
    """reset 정보 파싱 & 검증"""
    m = re.search(
        r"resets\s+([0-9]{1,2}:[0-9]{2}(?:am|pm)?)\s+\(([^)]+)\)",
        text, re.IGNORECASE
    )
    return f"{m.group(1)} ({m.group(2)})" if m else None
```

---

### 8. MED: 승인 요약 추출 시 도구명 누락 가능성

**심각도**: MED

**코드 근거**:
- `bot.py:109-127`: summarize_approval
  ```python
  def summarize_approval(text: str) -> str:
      raw_lines = text.splitlines()
      proceed_idx = -1
      for i in range(len(raw_lines) - 1, -1, -1):
          if "Do you want to" in raw_lines[i]:
              proceed_idx = i
              break
      if proceed_idx < 0:
          return "승인"
      
      start = max(0, proceed_idx - 30)
      for ln in raw_lines[start:proceed_idx]:
          s = ln.strip()
          m = re.match(r"^(Bash|Edit|Write|...|Task)\b", s)
          if m:
              return m.group(1)
      return "승인"
  ```

**문제점**:
- 도구 헤더가 정확히 줄 시작에 있어야만 매칭
- 들여쓰기나 ANSI 코드 남음 → 정규식 미스매치 가능
- 도구명이 없으면 무조건 "승인" 반환 → 사용자가 어떤 도구 승인 중인지 모름
- 예: "  ▏ Bash(...)" 형식이면 `^` 앵커로 미스매치

**개선 방향**:
```python
def summarize_approval(text: str) -> str:
    # 도구명 추출
    tools = re.findall(r"\b(Bash|Edit|Write|Read|Grep|Glob)\b", text)
    if tools:
        return tools[-1]  # 가장 최근 도구
    
    # 캡션 추출
    m = re.search(r"Allow\s+(\w+)\s+to", text)
    if m:
        return m.group(1)
    
    return "승인"
```

---

### 9. MED: 이미지 저장 경로가 절대 경로인데 상대 경로로 전달 가능성

**심각도**: MED

**코드 근거**:
- `bot.py:970-971`: IMAGE_DIR 생성
  ```python
  IMAGE_DIR = Path(__file__).parent / "tg_images"
  IMAGE_DIR.mkdir(exist_ok=True)
  ```
- `bot.py:986-1001`: 이미지 처리
  ```python
  fname = f"tg_{int(time.time())}.jpg"
  fpath = IMAGE_DIR / fname
  ...
  payload = f"[텔레그램 이미지 첨부: {fpath}]"
  ...
  send_input(payload)
  ```

**문제점**:
- `fpath` = `Path("/Users/kunrunic/claude-bridge/tg_images/tg_1713179400.jpg")`
- Claude Code가 `/Users/kunrunic/claude-bridge/` (봇 폴더) 에서 실행되지 않을 수 있음
- Claude가 다른 프로젝트 디렉토리에서 `--resume` 할 경우 상대 경로는 무의미
- 절대 경로를 전달했으므로 Claude가 올바르게 Read 도구 호출 가능 (현재는 OK)
- 하지만 `IMAGE_DIR` 이 bot 폴더에 하드코딩되어 있음 → 다른 경로에서 bot 실행 시 이미지 경로가 꼬임

**개선 방향**:
```python
# config.json에 image_storage 경로 추가
IMAGE_DIR = Path(_cfg.get("image_storage", str(Path(__file__).parent / "tg_images")))

# 또는 ~/.claude 아래 글로벌 경로 사용
IMAGE_DIR = Path.home() / ".claude" / "bridge_images"
```

---

### 10. MED: config.json 오류 시 예외 처리 미흡

**심각도**: MED

**코드 근거**:
- `bot.py:26-30`: 부팅 시 config 로드
  ```python
  def _load_config() -> dict:
      with open(_CONFIG_PATH, encoding="utf-8") as f:
          return json.load(f)
  
  _cfg = _load_config()
  ```
- 이후 즉시 접근
  ```python
  TOKEN       = _cfg["token"]
  CLAUDE      = _cfg["claude_path"]
  ```

**문제점**:
- `config.json` 누락 → FileNotFoundError (비정상 종료)
- JSON 문법 오류 → JSONDecodeError (비정상 종료)
- 필드 누락 (`token`, `claude_path`) → KeyError (비정상 종료)
- systemd/launchd 서비스로 등록할 경우 보이지 않는 곳에서 크래시

**개선 방향**:
```python
def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        print(f"ERROR: {_CONFIG_PATH} 파일이 없습니다. setup.sh를 먼저 실행하세요.")
        sys.exit(1)
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: {_CONFIG_PATH} JSON 형식 오류: {e}")
        sys.exit(1)
    
    required = ["token", "claude_path"]
    for key in required:
        if key not in cfg:
            print(f"ERROR: config.json 에 '{key}' 필드가 없습니다.")
            sys.exit(1)
    return cfg
```

---

### 11. LOW: 로깅 구조화 부족 - 시간 이외 추적 불가

**심각도**: LOW

**코드 근거**:
- `bot.py:38-45`: `_log()` 함수
  ```python
  def _log(tag: str, msg: str = ""):
      import time
      ts = time.strftime("%H:%M:%S")
      if msg:
          print(f"[{ts}] [{tag}] {msg}")
  ```
- 로그 출력: `[10:35:45] [AI-BUSY] Thinking… (2s)`

**문제점**:
- JSON/구조화 로그 미지원 → grep/awk로 분석 어려움
- session_id, chat_id 같은 컨텍스트 정보 로깅 없음
- 여러 세션/사용자 로그 섞임 → 추적 곤란
- 원본 로그 파일만 남고 분석 도구 없음

**개선 방향**:
```python
import logging
import json
from logging.handlers import RotatingFileHandler

def setup_logging():
    logger = logging.getLogger("claude_bridge")
    handler = RotatingFileHandler(
        f"logs/{date.today()}.jsonl",
        maxBytes=10*1024*1024, backupCount=3
    )
    handler.setFormatter(
        logging.Formatter(
            '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "tag": "%(name)s", "msg": "%(message)s"}'
        )
    )
    logger.addHandler(handler)
    return logger

logger.info("task", extra={
    "session_id": session_id[:12],
    "chat_id": chat_id,
    "duration_ms": elapsed_ms,
})
```

---

### 12. LOW: restart.sh 의 오류 처리 부실

**심각도**: LOW

**코드 근거**:
- `restart.sh:8`
  ```bash
  ./stop.sh || true
  ```

**문제점**:
- `|| true` 로 모든 오류 무시 → stop 실패를 감지 불가
- stop 실패 후 start 실행 시 tmux 세션 충돌 가능성
- 사용자는 "재시작됨" 메시지를 보지만 실제로는 기존 세션 여전히 살아있을 수 있음

**개선 방향**:
```bash
set -e

cd "$(dirname "$0")"

echo "=== 재시작 시작 ==="
if ./stop.sh; then
    echo "✓ 기존 세션 종료됨"
else
    echo "⚠️ 기존 세션 종료 실패, 계속 진행..."
fi

sleep 2  # 정리 시간

echo "--- 새 세션 시작 ---"
./start.sh
```

---

## 종합 평가

### 강점
- Telegram ↔ tmux 중계 로직 자체는 견고함 (ANSI 파싱, 승인 흐름)
- 세션 락 개념 도입으로 멀티 인스턴스 충돌 방지 시도
- 스크린샷 없이 구조화 로그만으로 버그 재현 가능한 repair.sh 개념

### 약점
1. **글로벌 싱글톤 bridge** → 멀티 유저 미지원, 확장 불가능
2. **멀티 인스턴스 인자 전달 미완성** → start.sh와 bot.py 사이 계약 미충족
3. **책임 분리 부재** → 1066줄 모놀리스, 테스트 불가능
4. **정규식 거짓 양성** → 상황별 정확도 저하
5. **레이스 컨디션** → 동시 핸들러 호출 시 불안정

### 권장 우선순위
1. **#1 (CRITICAL)**: INSTANCE_NAME argv 수신 구현 → 멀티 인스턴스 격리
2. **#2 (CRITICAL)**: Bridge 싱글톤 → 딕셔너리 풀로 변경 (또는 chat_id당 tmux window 분리)
3. **#3 (HIGH)**: 정규식 정확도 개선 (tail 기반 매칭, 컨텍스트 확인)
4. **#4 (HIGH)**: 모듈 분리 → tmux_bridge.py, session_monitor.py 추출
5. **#7 (MED)**: 레이스 컨디션 → task 라이프사이클 관리 개선

---

## 결론

claude-bridge는 **개념적으로 우수한 설계**이지만, **구현 단계에서 타협**이 많습니다. 특히 멀티 인스턴스/멀티 유저 시나리오가 불완전하고, 모놀리식 구조로 인해 **확장성과 유지보수성이 낮습니다**. 현재 상태로는 "단일 사용자, 단일 폴더 인스턴스" 용도로만 안정적입니다.

**리팩토링 없이 프로덕션화하려면** 사용 제약(1 사용자/1 인스턴스)을 명시 문서화하고, 위의 CRITICAL 2개 항목만이라도 수정하길 권장합니다.
