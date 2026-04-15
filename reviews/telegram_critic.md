# python-telegram-bot 라이브러리 사용 비판 검토
## claude-bridge 프로젝트

검토 날짜: 2026-04-15  
검토 대상: bot.py (1066 줄)  
라이브러리: python-telegram-bot v20+ (async)

---

## 문제점 목록

### 1. Rate Limit & Flood Control 완전 부재

**심각도: HIGH**

**코드 근거:**
- `_send_output()` (라인 723-739): 청크 사이에 0.2초 지연만 있음
- `on_message()` (라인 973-1021): 무제한 메시지 처리, rate limit 없음
- `app.run_polling()` (라인 1062): 기본 poll_interval(1초) 사용, 설정 없음

**문제점:**
```python
# 문제: 청크 전송 시 Telegram 속도 제한 무시
for i, chunk in enumerate(chunks, 1):
    # ...
    await app.bot.send_message(chat_id, body, parse_mode="HTML")
    await asyncio.sleep(0.2)  # ← 부족함 (Telegram 제한: ~30msg/sec per chat)
```

- Telegram API는 채팅당 30 메시지/초 제한
- 긴 Claude 응답(10+ 청크)을 0.2초 간격으로 전송하면 rate limit 도달 가능성 높음
- `edit_message_text()` (라인 634-638): 상태 업데이트 루프가 제한 검사 없음
- 사용자 메시지 폭증 시 (예: 봇 공격) 처리 불가능

**개선 방향:**
- 청크 전송 간격을 최소 100ms 이상으로 증가 (권장: 150-200ms)
- `RateLimiter` 또는 semaphore 구현으로 메시지 전송 속도 제어
- Telegram 에러 응답(429 Too Many Requests) 감지 후 지수 백오프 적용
- 폴링 간격 동적 조정 (busy 시 느리게, idle 시 빠르게)

**예시 개선:**
```python
# Semaphore 기반 rate limiting
self._send_semaphore = asyncio.Semaphore(10)  # 동시 전송 10개 제한

async def _send_output_ratelimited(self, app, chat_id, text):
    async with self._send_semaphore:
        chunks = _chunk_text(text, 3500)
        for chunk in chunks:
            await app.bot.send_message(...)
            await asyncio.sleep(0.15)  # 최소 150ms
```

---

### 2. Polling 방식의 동시성 문제 및 상태 레이스 조건

**심각도: HIGH**

**코드 근거:**
- `bridge.monitor()` (라인 484-692): 무한 루프로 1초마다 상태 폴링
- `on_callback()` → `bridge.task = asyncio.create_task(...)` (라인 882, 928-929, 960-961)
- `_acquired_lock()` (라인 226-245): 파일 시스템 기반 락, 동시성 미보장

**문제점:**

1. **Monitor 루프와 메시지 핸들러의 동시 실행 레이스:**
   ```python
   # 시나리오: 사용자가 메시지 보내는 순간 monitor가 상태 읽음
   # monitor() 루프 (라인 532)
   out = pane_output()  # tmux 출력 읽기
   clean = strip_ansi(out).strip()
   
   # 동시에 on_message()에서
   send_input(caption)  # tmux에 입력 전송
   
   # → 결과: monitor가 부분 입력 또는 응답 스니펫을 읽을 수 있음
   ```

2. **상태 변수 동시 접근:**
   - `self.awaiting_approval` (라인 392): monitor와 on_callback에서 동시 수정
   - `self.last_sent` (라인 391): 중복 방지 로직이 불완전
   - `self._sent_keys` (라인 396): 리스트 append는 thread-safe이나, 검사-수정 사이 간격 존재

3. **Task 취소 안전성:**
   ```python
   if bridge.task:
       bridge.task.cancel()  # ← 진행 중인 await 도중 취소되면?
   bridge.task = asyncio.create_task(...)
   ```
   - 이전 task가 정리되지 않을 수 있음
   - CancelledError 처리 없음 (라인 446)

4. **Polling 기반 설계의 본질적 문제:**
   - 1초 간격 폴링은 응답 지연 → 모니터링 부정확
   - busy 상태 감지 딜레이 (라인 598-616): "esc to interrupt" 신호가 0-1초 내 사라질 수 있음
   - 신뢰 프롬프트 자동 응답 실패 가능성 (라인 536-543): 프롬프트가 0.5초 미만 표시되면 놓침

**개선 방향:**
- asyncio Lock/Event 도입으로 상태 변수 보호
- `asyncio.Event` 또는 `asyncio.Condition` 사용으로 signaling 구현
- Webhook 전환 검토 (polling에서 push 모델로)
- Task 취소 시 `asyncio.CancelledError` 명시 처리

---

### 3. 메시지 유실 및 중복 전송 가능성

**심각도: MED**

**코드 근거:**
- `_already_sent()` (라인 460-463): 최근 20개 히스토리 기반 중복 판정
- `_response_key()` (라인 455-458): 공백/줄 정규화만 함
- monitor 루프의 해시 기반 안정화 (라인 656-686): settle >= 2만으로 확인

**문제점:**

1. **20개 히스토리는 부족한 버퍼:**
   ```python
   if len(self._sent_keys) > 20:
       self._sent_keys.pop(0)  # ← FIFO 제거
   ```
   - 청크 다중 전송 시 이전 응답이 빠르게 제거됨
   - 사용자가 메시지를 늦게 본 후 '다시 달라'고 요청하면? → 중복 전송 가능

2. **Hash 기반 안정화의 문제:**
   ```python
   h = hashlib.md5(clean.encode()).hexdigest()
   if h != self.last_hash:
       self.last_hash = h
       pending = clean
       settle = 0
   else:
       settle += 1
   ```
   - ANSI 색상 코드 제거 후 비교하나, 타이밍 깔짝한 변화 감지 못할 수 있음
   - 예: Claude가 점진적으로 응답 추가 시 (streaming 흉내) 중간 스냅샷 유실

3. **Telegram 전송 실패 처리 미흡:**
   ```python
   try:
       await app.bot.send_message(...)
   except Exception as e:
       print(...)  # ← 로그만, 재시도 없음
   ```
   - 네트워크 오류 시 메시지가 버려짐
   - `last_sent` 업데이트 전 실패하면 중복, 후 실패하면 유실

4. **Approval 응답 중 네트워크 오류:**
   ```python
   bridge.awaiting_approval = False  # 라인 896
   send_key("Enter")  # 라인 898
   # 이후 edit_message_text 실패하면?
   # → 사용자는 응답 확인 불가, 하지만 Claude는 이미 승인 받음
   ```

**개선 방향:**
- 히스토리 크기를 동적으로 조정 (최소 50개 또는 시간 기반 1시간)
- SHA256 기반 내용 체크섬으로 더 안정적인 비교
- 전송 실패 시 재시도 로직 (exponential backoff)
- 메시지 전송 성공 확인 후에만 `last_sent` 업데이트

---

### 4. Callback Query Timeout 및 응답 확인 부재

**심각도: MED**

**코드 근거:**
- `on_callback()` (라인 871-968): callback_data 크기 제한 무시
- `q.answer()` (라인 875): 사용자 피드백 2초 제한
- 승인 응답 처리 (라인 892-918): 실제 클릭 후 처리 완료까지 시간 미측정

**문제점:**

1. **Callback Query 타임아웃:**
   ```python
   await q.answer()  # ← Telegram 제한: 30초 내 호출 필수
   # 라인 875에서만 호출, 이후 긴 작업(예: 세션 시작) 중 실패 가능
   
   # 예: 라인 955의 bridge.start() 완료까지 최대 5초+
   ok = bridge.start(session_id, chat_id=q.message.chat_id)
   ```
   - callback_query.answer()를 초기에만 호출하고, 작업 중 재호출 없음
   - 사용자는 "호이스트된" 상태로 남음

2. **Callback 데이터 크기:**
   ```python
   callback_data="resume:" + s["id"]  # ← ID가 UUID면 ~36자
   ```
   - Telegram 제한: callback_data ≤ 64바이트
   - 현재는 세션 ID만이므로 안전하나, 미래 확장 시 위험

3. **Long-running 콜백 작업:**
   ```python
   # 라인 954-961: 진행 중 상태 메시지 없음
   await q.edit_message_text(f"시작 중: {label}", parse_mode="Markdown")
   ok = bridge.start(...)  # 3-5초 소요
   # 사용자는 로딩 상태 모름
   ```

4. **에러 발생 시 콜백 응답 불완전:**
   ```python
   # 라인 967: 실패 시에만 send_message 호출
   else:
       await ctx.bot.send_message(...)  # ← edit 대신 새 메시지
   ```
   - 기존 메시지가 수정되지 않아 혼란 야기

**개선 방향:**
- callback_query.answer() 호출 후 notification 추가 전송
- Long-running 작업 시 진행 상태 메시지 주기적 업데이트
- Callback 타임아웃 대비: 별도 작업 큐 + async task로 관리
- Callback 데이터 최대 길이 검증

---

### 5. Error Handling 및 Recovery 부재

**심각도: MED**

**코드 근거:**
- `tmux_run()` (라인 80-82): returncode만 확인, stderr 무시
- Exception 처리가 거의 없거나 `pass` (라인 506, 640-641, 717, 776)
- 크래시 후 자동 복구 메커니즘 없음

**문제점:**

1. **Subprocess 에러 무시:**
   ```python
   def tmux_run(cmd: list[str]) -> subprocess.CompletedProcess:
       return subprocess.run(["/opt/homebrew/bin/tmux"] + cmd,
                             capture_output=True, text=True)
   # ← stderr에 있는 에러 메시지 접근 불가
   ```
   - tmux가 없거나 경로 잘못되면 조용히 실패
   - 디버깅 어려움

2. **Monitor 루프의 예외 처리:**
   ```python
   except Exception as e:
       print(f"[monitor error] {e}")  # ← 루프 계속
   await asyncio.sleep(1)
   ```
   - 모든 Exception을 catch하여 무시
   - 진짜 문제 (파일 권한, 디스크 부족 등) 식별 불가
   - 로그 파일에 쌓이지 않음 (포그라운드 출력만)

3. **Lock 파일 손상:**
   ```python
   def _parse_lock(session_id: str) -> tuple[str, int] | None:
       try:
           lines = lp.read_text().splitlines()
           return lines[0].strip(), int(lines[1].strip())
       except Exception:
           return None  # ← 손상된 파일 무시
   ```
   - 손상된 락 파일이 영구 블로킹 가능

4. **Telegram API 에러 미분류:**
   ```python
   except Exception as e:
       print(f"[send HTML failed: {e}]")
       try:
           await app.bot.send_message(header + chunk)
       except Exception as e2:
           print(f"[send plain failed: {e2}]")
   ```
   - 429 (rate limit), 400 (invalid param), 403 (blocked) 등 구분 없음
   - 400 vs 429는 전혀 다른 대응이 필요

**개선 방향:**
- 구체적 Exception 타입 처리 (telegram.error.TelegramError 등)
- 로그 파일 쓰기 (포그라운드 + 파일)
- Telegram 에러 코드별 처리 전략
- Crash dump / heartbeat 메커니즘

---

### 6. 동시 다중 사용자 지원 미설계

**심각도: MED**

**코드 근거:**
- 글로벌 `bridge` 인스턴스 (라인 695): 단일 인스턴스만 유지
- `ALLOWED_IDS` (라인 36): 복수 사용자 지원하나 순차 처리만 가능
- `on_message()` (라인 973): 현재 활성 세션의 chat_id만 처리

**문제점:**

1. **단일 tmux 세션:**
   ```python
   TMUX = _cfg.get("tmux_session", "claude_bridge")  # 라인 33
   # 모든 사용자가 같은 tmux 세션 공유
   ```
   - 사용자 A와 B가 동시에 세션 사용하면 심각한 충돌
   - locked 되지 않으면 둘 다 입력 전송 가능 (경쟁 상태)

2. **Chat ID별 tmux 세션 분리 전략 부재:**
   - 라인 33의 tmux_session이 argv로 전달되지만 (recent commit 참고), 현재 코드는 단일 이름만 사용
   - `_build_cmd()` (라인 399-405): chat_id를 반영하지 않음

3. **Lock 메커니즘의 한계:**
   ```python
   def _acquire_lock(session_id: str, chat_id: int) -> bool:
       # chat_id는 저장되나, tmux 세션 이름은 단일 TMUX 상수 사용
   ```
   - 같은 세션을 여러 chat_id가 동시에 resume하려 하면 첫 번째만 성공
   - 그 후 다른 사용자는 거부되나, 메시지 전송은? → `bridge.running`과 tmux 상태만 확인

4. **Monitor task의 chat_id 고정:**
   ```python
   async def monitor(self, app: Application, chat_id: int):
       self.chat_id = chat_id  # ← 단일 chat_id만 저장
   ```
   - 승인 메시지, 응답 모두 하나의 chat_id로만 전송

**개선 방향:**
- Chat ID별로 별도 tmux 세션 생성 (예: TMUX=f"cb_{chat_id}")
- Chat ID → tmux session 맵핑 (dict 또는 DB)
- 각 chat_id별 독립적 Bridge 인스턴스
- Lock 메커니즘 강화 (session_id + chat_id 복합 키)

---

### 7. ANSI 코드 제거 미흡 및 정규식 복잡성

**심각도: LOW**

**코드 근거:**
- `ANSI_RE` (라인 76): `\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])`
- `strip_ansi()` (라인 100-101): 모든 제거 시도
- 응답 추출 (라인 157-208): ANSI 제거 후 divider 감지

**문제점:**

1. **불완전한 ANSI 정규식:**
   ```python
   ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
   ```
   - ANSI 8 색상, 256 색상, true color 지원
   - 일부 특수 시퀀스 (SGR Parameters) 누락 가능
   - 예: `\x1B[38;5;196m` (256 색) 또는 `\x1B[38;2;255;0;0m` (true color)는 부분 제거

2. **응답 추출 로직의 취약성:**
   ```python
   # 라인 195-196
   if lines[i].lstrip().startswith("⏺"):
       start = i
       break
   ```
   - ⏺ 문자가 여러 번 나타나면? → 마지막만 선택 (정확할 수 있으나 불명확)
   - ANSI 코드 제거 전후 라인 수 불일치 가능

3. **Divider 검사의 정규식 비효율:**
   ```python
   def is_divider(line: str) -> bool:
       s = line.strip()
       return bool(s) and len(s) > 20 and all(c in "─" for c in s)
   ```
   - O(n) 순회 (각 문자 검사)
   - 정규식 사용이 나음: `re.match(r"^─{20,}$", s.strip())`

**개선 방향:**
- ANSI 정규식을 표준 라이브러리 사용 (예: `colorama.AnsiToWin32`)
- 또는 더 정확한 정규식: `\x1B\[[0-9;]*[mGKHflSTABCDE]`
- 응답 추출 시 ANSI 제거 이전에 구조 파싱
- Regex pre-compile 최적화

---

### 8. 설정 파일 오류 처리 및 검증 부재

**심각도: LOW**

**코드 근거:**
- `_load_config()` (라인 26-28): JSON 파싱 에러 처리 없음
- `TOKEN`, `CLAUDE` 등 필수 필드 검증 없음
- 파일 없으면 FileNotFoundError로 즉시 크래시

**문제점:**

1. **JSON 파싱 실패:**
   ```python
   def _load_config() -> dict:
       with open(_CONFIG_PATH, encoding="utf-8") as f:
           return json.load(f)
   # ← JSON 형식 오류 시 json.JSONDecodeError 발생 → 프로그램 종료
   ```

2. **필수 필드 누락:**
   ```python
   TOKEN       = _cfg["token"]  # KeyError 가능
   CLAUDE      = _cfg["claude_path"]
   ```

3. **필드 타입 검증 없음:**
   ```python
   ALLOWED_IDS: set[int] = set(_cfg.get("allowed_ids", []))
   # allowed_ids가 [123, "456", None] 같으면?
   ```

**개선 방향:**
- pydantic 또는 dataclass 기반 설정 검증
- 필수/선택 필드 명확히
- 기본값 제공

---

### 9. Webhook 전환 미고려 (Polling의 비효율)

**심각도: LOW (설계 권고)**

**코드 근거:**
- `app.run_polling()` (라인 1062): drop_pending_updates=True (설정 좋음)

**문제점:**

1. **Polling의 본질적 비효율:**
   - 1초마다 Telegram API 폴링 → 네트워크 자원 낭비
   - 응답 지연: 1초 ~ 2초 (평균 1.5초)
   - 모바일 네트워크에서 배터리 소비

2. **High-traffic 환경에서 부적절:**
   - 100개 allowed_ids면 동시에 여러 사용자 → 처리 지연
   - 비 동기 처리 안 함 (asyncio 사용하나 poll 루프는 blocking)

**개선 방향:**
- Webhook 전환 (production) 또는 최소 polling 간격 증대
- 현재 구조가 아니라면 polling 유지 가능 (개인 프로젝트)

---

## 종합 평가

| 카테고리 | 평가 | 우선순위 |
|---------|------|--------|
| Rate Limit | ⚠️ 심각 | 1순위 |
| 동시성 안전성 | ⚠️ 심각 | 1순위 |
| 메시지 유실 | ⚠️ 중간 | 2순위 |
| Callback 안정성 | ⚠️ 중간 | 2순위 |
| Error Handling | ⚠️ 중간 | 2순위 |
| 다중 사용자 | ⚠️ 중간 | 2순위 |
| ANSI 처리 | ✓ 낮음 | 3순위 |
| 설정 검증 | ✓ 낮음 | 3순위 |
| Webhook | ℹ️ 설계 | 선택 |

---

## 핵심 개선 로드맵

### Phase 1: 긴급 (Rate Limit + 동시성)
1. asyncio.Semaphore 기반 메시지 전송 rate limiting
2. asyncio.Lock으로 상태 변수 보호
3. Task 취소 시 CancelledError 처리

### Phase 2: 안정성 (메시지 유실 방지)
1. 전송 실패 재시도 로직
2. 히스토리 크기 증대 및 시간 기반 관리
3. Callback query timeout 명시 처리

### Phase 3: 호환성 (다중 사용자)
1. Chat ID별 tmux 세션 분리
2. Bridge 인스턴스 풀 관리
3. 사용자별 독립적 monitor task

### Phase 4: 관찰성 (모니터링)
1. 구조화된 로깅 (파일 기반)
2. Telegram 에러 코드별 메트릭
3. 심각한 상태 (crashed, deadlock) 감지 alert

---

## 결론

python-telegram-bot v20+ async 라이브러리 자체는 잘 설계되었으나, **claude-bridge에서는 Telegram API의 제한(rate limit, callback timeout)과 async/await의 동시성 모델을 충분히 고려하지 않았습니다.**

특히:
- **Polling 방식이 1초마다 상태를 폴링**하면서 **동시성 보호가 없어** tmux와의 상태 레이스가 발생 가능
- **Telegram rate limit 대응 없이** 메시지를 연속 전송하면 429 에러로 메시지 유실
- **Error handling이 전부 silent catch**로 프로덕션 환경에 부적절

권장: **Phase 1 + 2를 우선 수행**하여 안정성 향상, 이후 **다중 사용자 지원(Phase 3) 검토**.
