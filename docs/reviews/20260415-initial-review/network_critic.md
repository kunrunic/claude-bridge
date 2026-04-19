# Claude-Bridge 네트워크/인프라 비판적 검토 보고서

검토 대상: Telegram polling 기반 봇 + tmux 로컬 프로세스 제어 + 파일 시스템 락  
검토자: 네트워크/인프라 전문가  
검토 일시: 2026-04-15

---

## 🔴 문제점 목록 (심각도별 정렬)

### 1. Telegram API 폴링 재연결 실패 시나리오 미처리

**심각도: HIGH**

**코드 근거:**
- `bot.py:1062` - `app.run_polling(drop_pending_updates=True)`
- `bot.py:1043-1062` - main() 함수에서 polling loop 실패 시 복구 로직 부재

**문제점:**
- `run_polling()`이 네트워크 오류로 실패하거나 연결이 끊어진 후 자동 복구 메커니즘이 없음
- Telegram API 타임아웃, 연결 재설정, 502/503 에러 등에 대한 재시도 로직 부재
- polling 루프가 죽으면 프로세스 자체는 계속 실행되지만 메시지를 수신하지 않음 (좀비 상태)
- 장시간 운영 시 네트워크 글리치로 인한 silent failure 가능성 높음

**구체적 위험:**
- 사용자가 명령을 보내도 봇이 응답하지 않는 상황 지속
- 모니터링이 없을 경우 이 상태가 몇 시간 유지될 수 있음
- 프로세스가 실행 중이므로 자동 재시작 스크립트(systemd, supervisord)도 감지 불가

**개선 방향:**
```python
# 권장: exponential backoff와 함께 재시도 루프 추가
async def run_with_retry():
    retry_count = 0
    max_retries = 10
    base_delay = 5
    
    while retry_count < max_retries:
        try:
            app.run_polling(timeout=30, drop_pending_updates=True)
            break
        except Exception as e:
            retry_count += 1
            delay = base_delay * (2 ** min(retry_count, 3))
            _log("POLLING-ERROR", f"재시도 {retry_count}/{max_retries} in {delay}s: {e}")
            await asyncio.sleep(delay)
    
    if retry_count >= max_retries:
        _log("FATAL", "Polling failed after max retries")
        sys.exit(1)
```

**참고:** python-telegram-bot 라이브러리 자체는 connection pool을 관리하지만, 애플리케이션 레벨의 polling loop 복구는 개발자 책임.

---

### 2. 타임아웃 설정 부재 (subprocess & 네트워크 블로킹)

**심각도: HIGH**

**코드 근거:**
- `bot.py:80-82` - tmux_run() 무제한 대기:
  ```python
  def tmux_run(cmd: list[str]) -> subprocess.CompletedProcess:
      return subprocess.run(["/opt/homebrew/bin/tmux"] + cmd,
                            capture_output=True, text=True)  # timeout=None
  ```
- `stop.sh:25-29` - sleep 5 후 강제 종료:
  ```bash
  kill $PID
  sleep 5
  if kill -0 $PID 2>/dev/null; then
      kill -9 $PID
  ```

**문제점:**
- tmux 명령어 실행이 hang되면 봇 전체가 블로킹됨 (asyncio 메인 루프 freeze)
- tmux 세션이 느린 디스크 I/O, NFS 마운트 이슈, 또는 데드락 상태에 빠진 경우 timeout 없이 무한 대기
- `send_input()`, `send_key()`, `pane_output()` 호출 중 block → 승인/응답 처리 지연
- 대기 중인 Telegram 메시지 응답 불가 → 사용자 UX 저하

**구체적 시나리오:**
1. 느린 NFS 마운트에서 `pane_output()` → `tmux capture-pane` hang
2. 봇의 monitor() 코루틴 블로킹 (await asyncio.sleep(1)에 도달하지 못함)
3. Telegram 메시지 큐 쌓임, /esc, 승인 응답 미수신

**개선 방향:**
```python
import subprocess

def tmux_run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    """timeout을 포함한 tmux 실행"""
    try:
        return subprocess.run(
            ["/opt/homebrew/bin/tmux"] + cmd,
            capture_output=True,
            text=True,
            timeout=timeout  # 기본 5초
        )
    except subprocess.TimeoutExpired as e:
        _log("TMUX-TIMEOUT", f"Command timed out after {timeout}s: {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr="Timeout")

def pane_output() -> str:
    """timeout을 포함한 pane 출력"""
    r = tmux_run(["capture-pane", "-t", TMUX, "-p", "-S", "-200"], timeout=3.0)
    if r.returncode != 0:
        return ""  # timeout이면 빈 문자열 반환
    return r.stdout
```

**추가 고려:**
- Telegram API 타임아웃도 명시적으로 설정: `Application.builder().request(Request(connect_timeout=10, read_timeout=15)).build()`

---

### 3. 파일 시스템 기반 락의 NFS/공유 스토리지 안전성 문제

**심각도: HIGH**

**코드 근거:**
- `bot.py:214-245` - _lock_path() 및 _acquire_lock():
  ```python
  def _lock_path(session_id: str) -> Path:
      return _LOCK_DIR / f".cb_lock_{session_id}"
  
  def _acquire_lock(session_id: str, chat_id: int) -> bool:
      lp = _lock_path(session_id)
      try:
          fd = lp.open("x")  # "exclusive create" 모드
          fd.write(f"{TMUX}\n{chat_id}")
          fd.close()
          return True
  ```
- `stop.sh:38-50` - 락 파일 정리:
  ```bash
  for lf in ~/.claude/.cb_lock_*; do
      [ -f "$lf" ] || continue
      OWNER=$(head -1 "$lf" 2>/dev/null || true)
      if [ "$OWNER" = "$TMUX_NAME" ]; then
          rm -f "$lf"
  ```

**문제점 (POSIX 파일 시스템 한계):**

1. **NFS의 원자성 부재:**
   - `open("x")` (O_EXCL)는 NFS v3에서 race condition 발생 가능 ([RFC 3530 - NFS v4에서 fixed](https://tools.ietf.org/html/rfc3530))
   - 두 개 인스턴스가 동시에 `lp.open("x")`를 호출하면 둘 다 성공할 수 있음
   - 결과: 같은 세션을 두 곳에서 동시에 제어 (⏺ 응답 충돌, 승인 중복)

2. **stale lock 감지의 오류:**
   ```python
   if tmux_run(["has-session", "-t", owner_tmux]).returncode != 0:
       lp.unlink(missing_ok=True)
   ```
   - tmux 세션이 실제로 dead일 때만 정리 (process crash, 세션 hang 상황 미감지)
   - 네트워크 지연으로 `has-session` 명령이 timeout → stale lock이 영구적으로 남음

3. **락 파일 쓰기 완료 전 충돌:**
   ```python
   fd = lp.open("x")
   fd.write(f"{TMUX}\n{chat_id}")
   fd.close()  # ← 이 사이에 다른 프로세스가 읽으면 불완전한 데이터
   ```

**구체적 공격 벡터 (멀티 인스턴스 환경):**
```
T0: Instance A - open("x") → 성공
T1: Instance B - open("x") → NFS race로 성공 (둘 다 파일 생성)
T2: A가 ".claude/.cb_lock_session1" 쓰기 완료
T3: B가 동일 파일 덮어쓰기
→ 같은 세션_id에 대해 A와 B 모두 "획득 성공"으로 판단
```

**파일 시스템 종류별 영향:**
| 파일시스템 | O_EXCL 원자성 | 위험 |
|-----------|------------|------|
| ext4/btrfs/APFS (로컬) | ✓ 안전 | LOW |
| NFS v3 | ✗ 취약 | **HIGH** |
| NFS v4.0 | ⚠️ 부분 | MED |
| CIFS/SMB | ✗ 취약 | **HIGH** |

**개선 방향 (우선순위순):**

1. **fcntl.flock() 사용 (권장):**
   ```python
   import fcntl
   
   def _acquire_lock(session_id: str, chat_id: int) -> bool:
       lp = _lock_path(session_id)
       try:
           # 먼저 파일이 없으면 생성하되, 동시 접근은 제어
           lp.parent.mkdir(parents=True, exist_ok=True)
           with open(str(lp), "a") as f:
               # non-blocking exclusive lock 시도
               fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
               # lock 획득 성공 → 기존 데이터 지우고 새로 쓰기
               f.seek(0)
               f.truncate()
               f.write(f"{TMUX}\n{chat_id}\n")
               f.flush()
               return True
       except (IOError, OSError):
           return False  # lock 획득 실패 (다른 인스턴스가 소유)
   ```
   - **장점:** NFS, CIFS에서도 안전 (POSIX advisory lock)
   - **주의:** NFS v3 + BSD/Linux는 동작 방식 차이 있음

2. **Redis/etcd 등 중앙 저장소 사용:**
   ```python
   # redis를 사용한 분산 락
   import redis
   redis_client = redis.Redis(host='localhost', port=6379)
   
   def _acquire_lock(session_id: str, chat_id: int) -> bool:
       lock_key = f"bridge:lock:{session_id}"
       try:
           # SET NX EX → atomic한 "없으면 생성, 만료시간 포함"
           return redis_client.set(lock_key, f"{TMUX}:{chat_id}", 
                                   nx=True, ex=3600)
       except redis.ConnectionError:
           return True  # Redis 미연결 시 허용 (실패 안전성)
   ```
   - **장점:** 네트워크 파일시스템과 무관, 분산성 우수
   - **단점:** 외부 의존성 추가

3. **최소한의 임시방편:**
   ```python
   # 락 파일에 TTL (수정 시간 기반) 추가
   def _is_lock_stale(session_id: str, ttl_seconds: int = 600) -> bool:
       lp = _lock_path(session_id)
       if not lp.exists():
           return True
       mtime = lp.stat().st_mtime
       return time.time() - mtime > ttl_seconds
   
   def _acquire_lock(session_id: str, chat_id: int) -> bool:
       # stale lock 먼저 정리
       if _is_lock_stale(session_id):
           _lock_path(session_id).unlink(missing_ok=True)
       
       # 이후 기존 로직
       ...
   ```
   - 이 방식은 **stale lock만 정리하고 race condition은 미해결**

---

### 4. 모니터 루프의 예외 처리 부족 (silent failure)

**심각도: MEDIUM**

**코드 근거:**
- `bot.py:689-692` - monitor() 함수의 최상위 예외 처리:
  ```python
  except Exception as e:
      print(f"[monitor error] {e}")
  
  await asyncio.sleep(1)  # 무조건 계속 실행
  ```

**문제점:**
- 예외를 로그만 하고 계속 실행 → 특정 오류 상황에서 무한 실패 루프 가능
- 예: `pane_output()` 호출 시 tmux session이 없으면 subprocess 실패 → Exception
- 같은 예외가 매초 반복되어 로그를 도배하면서도 봇은 "살아있다"고 착각
- 심각한 오류(permission denied, memory exhaustion)를 일반 오류와 구분하지 않음

**예시:**
```
[09:00:15] [monitor error] Traceback (...): Permission denied
[09:00:16] [monitor error] Traceback (...): Permission denied
[09:00:17] [monitor error] Traceback (...): Permission denied
...  ← 무한 반복, 사용자는 봇이 멀쩡하다고 생각
```

**개선 방향:**
```python
async def monitor(self, app: Application, chat_id: int):
    # ...
    error_count = 0
    last_error = None
    
    while self.running:
        try:
            # ... 모니터 로직
            error_count = 0  # 성공하면 리셋
            
        except asyncio.CancelledError:
            raise  # 명시적 취소는 전파
        
        except (KeyError, ValueError, AttributeError) as e:
            # 프로그래밍 오류 - 재시작 필요
            _log("MONITOR-BUG", f"Programming error: {e}")
            await app.bot.send_message(chat_id, f"봇 내부 오류: {e}")
            self.running = False
            break
        
        except Exception as e:
            error_count += 1
            error_str = str(e)
            
            if error_str != last_error:
                _log("MONITOR-ERROR", f"({error_count}회) {e}")
                last_error = error_str
            
            if error_count > 10:
                # 10회 연속 실패 → 심각한 오류
                await app.bot.send_message(chat_id, 
                    f"모니터링 오류 발생. /start로 재시작해주세요.")
                self.running = False
                break
        
        await asyncio.sleep(1)
```

---

### 5. Telegram API 재연결 후 메시지 손실

**심각도: MEDIUM**

**코드 근거:**
- `bot.py:1062` - `app.run_polling(drop_pending_updates=True)`
- `bot.py:535-596` - monitor()에서 특정 상태 체크 없음

**문제점:**
- `drop_pending_updates=True`는 봇 시작 시점의 대기 중인 메시지를 버림
- 네트워크 끊김 → 재연결 → 그 사이 사용자가 보낸 메시지 손실 가능
- 승인 응답(/esc, 타이밍)이 중요한 경우 문제 심각

**시나리오:**
```
T0: 사용자가 "Yes (승인)" 버튼 클릭 → Telegram 서버 큐
T1: 봇 네트워크 끊김
T2: 봇 재연결 (run_polling 재시작)
T3: drop_pending_updates=True로 T0 메시지 버림
T4: Claude는 계속 사용자 응답 대기 → timeout/hang
```

**개선 방향:**
```python
def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )
    # ...
    
    # 첫 시작 시에만 drop_pending_updates=True
    # 이후 재연결은 drop 안함
    drop_first_only = True
    
    while True:
        try:
            app.run_polling(
                drop_pending_updates=drop_first_only,
                allowed_updates=["message", "callback_query"]  # 필요한 것만
            )
        except KeyboardInterrupt:
            break
        except Exception as e:
            _log("POLLING-ERROR", f"{e}, reconnecting in 5s...")
            drop_first_only = False  # 재연결 후에는 drop 안함
            time.sleep(5)
```

---

### 6. tmux subprocess 호출의 sync/async 불일치

**심각도: MEDIUM**

**코드 근거:**
- `bot.py:80-98` - 모든 tmux 조작이 동기 함수:
  ```python
  def tmux_run(cmd: list[str]) -> subprocess.CompletedProcess:
      return subprocess.run(...)  # 블로킹
  
  def send_input(text: str):
      import time
      tmux_run([...])  # 블로킹
      time.sleep(0.1)  # 동기 sleep
  ```
- `bot.py:484-692` - 이들을 async 컨텍스트에서 호출:
  ```python
  async def monitor(self, app: Application, chat_id: int):
      while self.running:
          # ...
          send_input(caption)  # ← 동기 호출이 await 없음 → 이벤트 루프 블로킹
          await asyncio.sleep(1)
  ```

**문제점:**
- `send_input()`, `pane_output()` 등이 subprocess.run()을 직접 호출
- asyncio 이벤트 루프를 블로킹 → 다른 메시지/콜백 처리 지연
- 동시에 여러 명령이 들어오면 큐잉 없이 순차 블로킹

**구체적 시나리오:**
```
T0: /start 명령 받음 (cmd_start 핸들러 실행)
T0.5: send_input() 호출 → subprocess.run() 시작 (2초 소요)
T0.6: 사용자가 메시지 전송 (on_message 핸들러 대기)
T2.5: subprocess 완료, send_input() 반환
T2.5: on_message 핸들러 처리 시작 (2초 지연)
→ 사용자 UX 저하
```

**개선 방향:**
```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=4)

def tmux_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["/opt/homebrew/bin/tmux"] + cmd,
                          capture_output=True, text=True, timeout=5.0)

async def tmux_run_async(cmd: list[str]) -> subprocess.CompletedProcess:
    """비동기 버전 - 스레드 풀에서 실행"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, tmux_run, cmd)

async def send_input_async(text: str):
    """비동기 입력"""
    await tmux_run_async(["send-keys", "-t", TMUX, "-l", text])
    await asyncio.sleep(0.1)
    await tmux_run_async(["send-keys", "-t", TMUX, "Enter"])

# monitor() 내에서 사용:
async def monitor(self, app: Application, chat_id: int):
    # ...
    await send_input_async(caption)  # ← 이제 non-blocking
```

---

### 7. 승인 프롬프트 응답 타임아웃 없음

**심각도: MEDIUM**

**코드 근거:**
- `bot.py:566-591` - 승인 대기 루프:
  ```python
  self.awaiting_approval = True
  await _send_approval(app, chat_id, clean)
  await asyncio.sleep(1)
  continue  # ← 무한 루프, 최대 대기 시간 없음
  ```

**문제점:**
- 사용자가 승인 버튼을 누르지 않으면 봇은 영원히 대기
- Claude 프로세스도 진행하지 못함 (사용자 입력 차단)
- 네트워크 오류로 승인 메시지가 도달하지 않으면 교착 상태

**개선 방향:**
```python
async def monitor(self, app: Application, chat_id: int):
    # ...
    approval_timeout_seconds = 300  # 5분
    approval_start_time = None
    
    while self.running:
        # ...
        
        if is_approval(clean) and not self.awaiting_approval:
            # ...
            self.awaiting_approval = True
            approval_start_time = time.time()
            await _send_approval(app, chat_id, clean)
            await asyncio.sleep(1)
            continue
        
        # 승인 대기 중
        if self.awaiting_approval:
            elapsed = time.time() - approval_start_time
            if elapsed > approval_timeout_seconds:
                _log("APPROVAL-TIMEOUT", f"{elapsed}s 초과")
                await app.bot.send_message(chat_id, 
                    "⏰ 승인 응답 타임아웃. ESC를 눌러 취소하세요.")
                self.awaiting_approval = False
                send_key("Escape")
            else:
                await asyncio.sleep(1)
            continue
```

---

### 8. 로그인 토큰 설정 파일의 보안 (낮은 우선순위)

**심각도: LOW**

**코드 근거:**
- `bot.py:24` - config.json에서 TOKEN 로드:
  ```python
  TOKEN = _cfg["token"]
  ```

**문제점:**
- config.json이 일반 파일이므로 권한이 644라면 누구나 읽을 수 있음
- Telegram bot token 노출 → 봇 계정 탈취

**개선 방향:**
```bash
# start.sh에 추가
if [ -f "config.json" ]; then
    if [ "$(stat -f '%A' config.json 2>/dev/null || stat -c '%a' config.json)" != "600" ]; then
        echo "⚠️ config.json 권한 수정: 644 → 600"
        chmod 600 config.json
    fi
fi
```

---

## 📊 리스크 기반 우선순위

| 순위 | 문제점 | 심각도 | 영향 범위 | 개선 난이도 | 추천 |
|-----|------|--------|---------|----------|------|
| 1 | Polling 재연결 미처리 | HIGH | 장시간 운영 | MED | 🔴 즉시 |
| 2 | 타임아웃 부재 | HIGH | 모든 tmux 호출 | MED | 🔴 즉시 |
| 3 | NFS 락 race condition | HIGH | 멀티 인스턴스 | HIGH | 🟡 1주일 내 |
| 4 | 예외 처리 부족 | MED | 운영 안정성 | LOW | 🟡 우선 |
| 5 | 메시지 손실 (drop_pending) | MED | 신뢰성 | LOW | 🟡 권장 |
| 6 | sync/async 불일치 | MED | 응답 지연 | MED | 🟡 권장 |
| 7 | 승인 타임아웃 | MED | edge case | LOW | 🟡 우선 |
| 8 | 토큰 파일 권한 | LOW | 보안 | LOW | 🟢 선택 |

---

## ✅ 현재 코드의 장점 (for balance)

다음 항목들은 잘 구현되어 있습니다:

1. **다중 인스턴스 락 시스템:** TMUX 세션명 기반 소유권 추적 (NFS 문제 제외)
2. **stale 락 자동 정리:** tmux 세션 존재 여부 확인 후 정리 (제한적이지만 방어)
3. **ANSI 색상 제거:** 터미널 출력을 깔끔하게 파싱
4. **중복 응답 방지:** `_already_sent()` 로직으로 재전송 차단
5. **신뢰 프롬프트 자동 승인:** 새 폴더 진입 시 자동 처리
6. **해시 기반 상태 변화 감지:** `last_hash` 비교로 불필요한 전송 방지

---

## 🎯 단기 개선 로드맵

### Phase 1 (1주일)
- [ ] tmux_run() timeout 추가 (3~5초)
- [ ] monitor() 예외 처리 개선 (error count 기반 중단)
- [ ] app.run_polling() retry loop 추가

### Phase 2 (2주일)
- [ ] fcntl.flock() 기반 락 시스템 도입 또는 TTL 기반 개선
- [ ] async send_input() 구현 (ThreadPoolExecutor)
- [ ] 승인 응답 타임아웃 추가

### Phase 3 (1개월)
- [ ] Redis 기반 분산 락 평가
- [ ] monitoring/alerting 시스템 추가 (e.g., sentry, datadog)
- [ ] systemd unit 작성 (자동 재시작)

---

## 📝 결론

**현재 구조의 주요 취약점:**
1. **폴링 네트워크 복구 메커니즘 부재** → 장시간 운영 불가
2. **타임아웃 부재로 인한 이벤트 루프 블로킹** → UX 저하
3. **파일 시스템 락의 NFS 취약성** → 멀티 인스턴스 충돌 가능

**권장 사항:**
- 네트워크 안정성 개선이 최우선 (Polling + timeout)
- 락 시스템은 환경에 따라 fcntl 또는 Redis로 업그레이드
- 예외 처리 강화로 운영 안정성 확보

---

**작성자:** Network/Infrastructure Review Team  
**버전:** 1.0  
**최종 수정:** 2026-04-15
