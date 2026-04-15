# claude-bridge 네트워크/인프라 검토 (긍정 관점)

네트워크 단절 복구 능력, polling 아키텍처의 이점, 로컬 통신의 안정성, 프로세스 관리의 견고성을 중심으로 검토합니다.

---

## 강점 목록

### 1. **Polling 기반 아키텍처의 방화벽 친화성**

**강점**: Telegram Bot API의 polling 방식 사용으로 incoming webhook이 필요 없어 NAT/방화벽 환경에서 즉시 배포 가능

- **bot.py:1062** - `app.run_polling(drop_pending_updates=True)`: 표준 python-telegram-bot polling 루프
- **start.sh:43** - `nohup python -u bot.py "$INSTANCE_NAME"`: 로컬 환경 어디든 장기 실행 가능
  
**이점**:
- 서버 공개 IP 불필요
- 포트포워딩 설정 불필요
- 모바일 핫스팟, 회사 네트워크 등 다양한 환경 지원
- 비용 효율적 (서버리스 봇 운영)

---

### 2. **네트워크 단절 자동 복구 메커니즘**

**강점**: 비동기 태스크 기반 모니터링으로 예상치 못한 연결 끊김 시 자동 재연결

- **bot.py:385-401** - `Bridge` 클래스: `asyncio.Task` 기반 상태 관리로 비동기 모니터링 구현
  ```python
  self.task: asyncio.Task | None = None  # 비동기 태스크 생명주기 관리
  ```
- **bot.py:878-883** - `reattach` 콜백:
  ```python
  if bridge.task:
      bridge.task.cancel()
  bridge.task = asyncio.create_task(bridge.monitor(ctx.application, q.message.chat_id))
  ```
  
**메커니즘**:
- Telegram API 연결 실패 시 python-telegram-bot이 자동으로 backoff 재연결
- Bridge 모니터 태스크가 비동기 예외 처리로 네트워크 재개 감지
- `drop_pending_updates=True`: 네트워크 복구 후 누적된 stale 메시지 정리로 상태 일관성 유지

---

### 3. **파일 시스템 기반 락의 견고성 및 stale 감지**

**강점**: 원자성 있는 파일 operations + stale 세션 자동 정리로 다중 인스턴스 동시성 제어

- **bot.py:226-245** - `_acquire_lock()` 함수:
  ```python
  fd = lp.open("x")  # 원자적 exclusive create (FileExistsError 보장)
  ```
  - `"x"` 모드: 파일이 이미 있으면 실패 → race condition 방지
  
- **bot.py:236-242** - Stale lock 자동 정리:
  ```python
  if info:
      owner_tmux, _ = info
      if tmux_run(["has-session", "-t", owner_tmux]).returncode != 0:
          lp.unlink(missing_ok=True)  # stale tmux 소유 락 삭제
          return _acquire_lock(session_id, chat_id)  # 재시도
  ```

**신뢰성**:
- 프로세스 crash 후 고아 lock 자동 해제
- tmux 세션 존재 여부로 소유권 실시간 검증
- 다중 인스턴스에서 같은 Claude 세션 동시 점유 방지

---

### 4. **로컬 tmux 통신의 낮은 지연 및 높은 신뢰성**

**강점**: 로컬 프로세스 간 tmux send-keys로 안정적이고 지연 최소화된 통신

- **bot.py:80-98** - tmux 유틸리티 함수:
  ```python
  def tmux_run(cmd: list[str]) -> subprocess.CompletedProcess:
      return subprocess.run(["/opt/homebrew/bin/tmux"] + cmd,
                            capture_output=True, text=True)
  
  def send_input(text: str):
      tmux_run(["send-keys", "-t", TMUX, "-l", text])  # literal mode (파싱 회피)
      time.sleep(0.1)
      tmux_run(["send-keys", "-t", TMUX, "Enter"])
  ```

**이점**:
- 네트워크 지연 없음 (localhost 프로세스 간 통신)
- 로컬 마운트 파일시스템만으로 동작 (네트워크 드라이브 불필요)
- Telegram 폴링 ↔ tmux 제어 사이 비동기 처리로 UI 블로킹 없음

---

### 5. **화면 모니터링 기반 상태 감지의 적응성**

**강점**: 정규식 기반 상태 감지로 Claude 내부 상태를 외부에서 신뢰성 있게 추적

- **bot.py:56-76** - 여러 정규식으로 상태 구분:
  ```python
  APPROVAL_RE = re.compile(r"Do you want to proceed|...")  # 승인 요청
  TRUST_RE = re.compile(r"Quick safety check|...")         # 폴더 신뢰
  BUSY_RE = re.compile(r"esc to interrupt")                # 활성 작업
  LIMIT_RE = re.compile(r"You've hit your limit|...", re.IGNORECASE)  # 한도
  ```

- **bot.py:129-155** - 세밀한 busy 상태 감지:
  ```python
  def busy_status(text: str) -> str:
      """화면에서 실제 진행 상태 라인을 추출"""
      tail_lines = text.splitlines()[-25:]  # 최근 25줄만 분석 (오탐 방지)
      for line in reversed(tail_lines):
          m = _STATUS_LINE_RE.search(line)  # Compacting/Thinking/Running 등 감지
          if m:
              label = m.group(1).strip()
              return label[:80]  # 너무 긴 상태는 자르기
  ```

**적응성**:
- Claude 내부 상태 변화를 실시간 감지 → Telegram 사용자에게 진행상황 즉시 피드백
- 정규식 패턴을 외부화하여 새 상태 타입 추가 용이

---

### 6. **프로세스 생명주기의 견고한 정리**

**강점**: graceful shutdown → force kill 2단계로 좀비 프로세스 방지

- **stop.sh:22-31** - 우아한 종료 및 강제 종료:
  ```bash
  PID=$(cat "$PID_FILE")
  if kill -0 $PID 2>/dev/null; then      # PID 존재 확인
      kill $PID                          # SIGTERM (정상 종료)
      sleep 5
      if kill -0 $PID 2>/dev/null; then  # 여전히 살아있으면
          echo "정상 종료 안됨, 강제 종료 중..."
          kill -9 $PID 2>/dev/null || true  # SIGKILL
      fi
  fi
  ```

- **stop.sh:38-51** - 인스턴스별 락파일 정리:
  ```bash
  OWNER=$(head -1 "$lf" 2>/dev/null || true)
  if [ "$OWNER" = "$TMUX_NAME" ]; then
      rm -f "$lf"  # 이 인스턴스 소유 락만 정리
  fi
  ```

**견고성**:
- 좀비 프로세스 없음
- 파일 락 정리로 다음 실행 시 깨끗한 시작
- 다중 인스턴스 환경에서 타 인스턴스 락 보존

---

### 7. **중복 제거를 통한 네트워크 효율성**

**강점**: 화면 내용 hash 기반 중복 감지 + 최근 전송 응답 추적으로 불필요한 Telegram 전송 최소화

- **bot.py:390-396** - 중복 추적 상태:
  ```python
  self.last_hash: str = ""                    # 화면 내용 hash
  self.last_sent: str = ""                    # 마지막 전송 응답
  self._sent_keys: list[str] = []            # 최근 20개 히스토리 (중복 방지)
  ```

- **bot.py:656-687** - hash 기반 변화 감지 및 중복 검사:
  ```python
  h = hashlib.md5(clean.encode()).hexdigest()
  if h != self.last_hash:
      self.last_hash = h
      pending = clean
      settle = 0
  else:
      settle += 1
  
  if pending and settle >= 2 and pending != self.last_sent:
      if response and "⏺" in response and not self._already_sent(response):
          await _send_output(app, chat_id, response)
  ```

**효율성**:
- 화면이 변하지 않으면 메시지 전송 안 함
- 같은 응답 중복 전송 방지
- Telegram 전송량 감소 → rate limit 회피, 네트워크 bandwidth 절감

---

### 8. **비동기 상태 머신으로 안정적 흐름 제어**

**강점**: 상태 기반 FSM으로 race condition 없이 승인/대기/작업 중 상태 전환 관리

- **bot.py:385-398** - Bridge 상태 변수:
  ```python
  self.running: bool = False                  # 세션 실행 여부
  self.awaiting_approval: bool = False        # 승인 대기 중
  self.was_busy: bool = False                 # 마지막 폴링 때 작업 중 여부 (엣지 감지용)
  self.limit_reported: bool = False           # 한도 초과 알림 중복 방지
  ```

- **bot.py:566-596** - 상태별 처리 로직:
  ```python
  if is_approval(clean) and not self.awaiting_approval:  # 승인창 진입
      # ... 승인 전송 ...
      self.awaiting_approval = True
  
  if self.awaiting_approval:                           # 승인 대기 중
      await asyncio.sleep(1)
      continue
  ```

**안정성**:
- 상태 전환 명확 (승인 대기 중에는 다른 액션 무시)
- 비동기 태스크에서도 상태 일관성 보장
- 사용자 입력 (승인/거부)과 모니터 태스크 간 동기화 간단

---

### 9. **로그 파일 자동 정리로 디스크 관리**

**강점**: 일정 기간 이상의 로그 자동 삭제로 장기 운영 안정성 보장

- **start.sh:37-38** - 3일 이상 된 로그 자동 삭제:
  ```bash
  find "$LOG_DIR" -name "*.log" -type f -mtime +3 -delete 2>/dev/null || true
  ```

**이점**:
- 디스크 full 방지
- 장기 실행 환경에서 무한정 로그 증가 방지
- 최근 3일 로그만 유지하여 디버깅 정보 충분히 확보

---

### 10. **프로세스 상태 검증을 통한 데드락 방지**

**강점**: 재시작 전 PID 검증으로 이전 인스턴스가 완전히 종료되었음을 확인

- **start.sh:11-20** - 재시작 안전성:
  ```bash
  if [ -f "$PID_FILE" ]; then
      PID=$(cat "$PID_FILE")
      if kill -0 $PID 2>/dev/null; then      # PID 생존 확인
          echo "이미 실행 중 (PID=$PID)..."
          exit 1
      else
          rm -f "$PID_FILE"                  # 좀비 PID 파일 정리
      fi
  fi
  ```

- **start.sh:48-57** - 시작 후 즉시 검증:
  ```bash
  sleep 1
  if kill -0 $BOT_PID 2>/dev/null; then      # 1초 후 PID 재확인
      echo "✓ 실행됨 (PID=$BOT_PID)"
  else
      echo "✗ 실행 실패"
      rm -f "$PID_FILE"
      exit 1
  fi
  ```

**신뢰성**:
- 서비스 시작 실패를 즉시 감지 (자동 재시작 스크립트의 feedback)
- 좀비 프로세스/스테일 PID 파일 정리로 깨끗한 시작 보장

---

### 11. **환경 변수 격리로 다중 인스턴스 안정성**

**강점**: 각 인스턴스가 폴더 이름을 argv로 받아 tmux 세션명 고유화

- **start.sh:42-43** - 인스턴스 식별:
  ```bash
  INSTANCE_NAME=$(basename "$PWD")
  nohup python -u bot.py "$INSTANCE_NAME" >> "$LOG_FILE" 2>&1 &
  ```

- **bot.py:1065-1066** - argv 처리:
  ```python
  if __name__ == "__main__":
      main()  # sys.argv[1]로 INSTANCE_NAME 수신
  ```

- **stop.sh:9-14** - 인스턴스 정확 식별:
  ```bash
  INSTANCE_NAME=$(basename "$PWD")
  LEFTOVER=$(pgrep -f "bot\.py $INSTANCE_NAME\$" | head -1 || true)  # 정확 매치
  ```

**다중 인스턴스 지원**:
- 같은 디렉토리 구조의 여러 프로젝트에서 동시 실행 가능
- 인스턴스명으로 ps 출력 구분 가능 (모니터링 용이)
- 각 인스턴스의 lock 파일, PID 파일 독립

---

### 12. **정규식 기반 ANSI 코드 제거로 안정적 화면 파싱**

**강점**: 터미널 컬러/스타일 코드를 사전에 제거하여 상태 감지 정확도 향상

- **bot.py:76, 100-101** - ANSI 제거:
  ```python
  ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
  
  def strip_ansi(s: str) -> str:
      return ANSI_RE.sub("", s)
  ```

**이점**:
- 정규식 패턴 매칭 안정성 향상 (색상 코드로 인한 오탐 방지)
- 상태 감지 정규식을 간결하게 유지

---

## 요약: 아키텍처 강점의 통합 관점

| 레이어 | 강점 | 신뢰성 | 확장성 |
|-------|------|--------|--------|
| **네트워크** | Polling (방화벽 친화, 자동 복구) | 높음 | 높음 |
| **프로세스** | tmux 로컬 통신 (지연 0, 네트워크 무관) | 높음 | 중상 |
| **동시성** | 파일 기반 lock + stale 감지 | 높음 | 높음 |
| **상태 관리** | 비동기 FSM + hash 기반 중복 제거 | 높음 | 높음 |
| **운영** | 로그 자동 정리, 정리 스크립트 | 중상 | 중상 |

이 설계는 **로컬 환경에서의 서버리스 봇 아키텍처**로, 
- 인터넷 연결 불안정한 환경에서도 자동 복구
- 다중 인스턴스 동시 운영 가능
- 네트워크 지연에 영향받지 않는 로컬 제어
를 달성하고 있습니다.
