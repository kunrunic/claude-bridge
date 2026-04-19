# Claude-Bridge Telegram Bot 긍정적 리뷰

python-telegram-bot v20+ (비동기) 활용을 중심으로 한 강점 분석

---

## 강점 목록

### 1. **Async/Await 기반의 비동기 이벤트 핸들링**

bot.py의 모든 핸들러가 `async def`로 구현되어 있으며, 이를 통해 동시에 여러 사용자 요청을 논블로킹으로 처리합니다.

**강점:**
- Telegram Bot API의 long-polling 중에도 사용자 입력, 승인 처리, 상태 업데이트가 겹치지 않음
- `asyncio.sleep()`과 `asyncio.create_task()`로 I/O 대기 중에도 다른 작업 수행
- tmux 프로세스 모니터링 루프(`bridge.monitor()`)가 Telegram 이벤트와 완전히 독립적으로 실행

**코드 근거:**
- `bot.py:804-821` (`cmd_start`) - async def로 선언하여 non-blocking
- `bot.py:484-692` (`bridge.monitor()`) - 무한 루프가 `await asyncio.sleep(1)` 사이사이 기회를 양보
- `bot.py:1026-1040` (`post_init`) - 초기화 후 `asyncio.create_task()`로 모니터 태스크 생성

---

### 2. **Inline Keyboard를 활용한 대화형 UI**

Telegram의 callback query 기반 inline button으로 복잡한 흐름을 텍스트 입력 없이 제어합니다.

**강점:**
- Yes/No 승인 선택을 버튼 클릭으로 즉각 처리 → 사용자 편의성 극대화
- 세션 선택, 권한 모드 토글, 재연결/새로시작 선택이 모두 `InlineKeyboardButton`으로 구현
- 버튼의 `callback_data` 파라미터로 각 선택지를 고유하게 식별 → `on_callback()` 핸들러에서 안전하게 라우팅

**코드 근거:**
- `bot.py:762-782` (`_send_approval()`) - "Yes"/"No" 버튼으로 승인 처리
- `bot.py:785-793` (`_build_start_kb()`) - 세션 목록을 `resume:SESSION_ID` 콜백 데이터로 변환
- `bot.py:871-968` (`on_callback()`) - 다중 `if data == "..."` 분기로 각 버튼 동작 처리
- `bot.py:892-918` - 승인 Yes/No 버튼: tmux에 Enter/Down → Enter 키 자동 전송

---

### 3. **Callback Query 응답과 메시지 편집의 정교한 조합**

Telegram Bot API의 `edit_message_text` / `edit_message_reply_markup`을 활용하여 버튼 클릭 후 메시지를 즉시 업데이트합니다.

**강점:**
- 사용자가 버튼을 클릭한 메시지를 실시간으로 수정 → 상태 변화를 UX로 표현
- `callback_query.answer()`로 팝업 알림 방지 → 깔끔한 인터페이스 유지
- 중복 클릭 방지: `bridge.awaiting_approval` 플래그로 이미 처리된 요청 감지

**코드 근거:**
- `bot.py:875` - `await q.answer()` - 팝업 없이 조용히 처리
- `bot.py:878-879` - `await q.edit_message_text("기존 세션 재연결 중…")` - 메시지 실시간 업데이트
- `bot.py:901, 916` - `edit_message_text()` + `reply_markup=None` - 버튼 제거 후 최종 상태 표시 (✅/❌)
- `bot.py:892-903` - `if not bridge.awaiting_approval: return` - 중복 승인 방지

---

### 4. **메시지 반응(Reaction) 활용으로 비언어적 피드백**

Telegram의 emoji reaction을 이용하여 사용자에게 실시간 상태를 전달합니다.

**강점:**
- 텍스트 메시지 전송 없이도 즉각적인 피드백
- 📷 (이미지 수신), ✍ (텍스트 입력), ⚡ (ESC 키 전송), ✅/❌ (승인/거부) 등 의도를 아이콘으로 표현
- try/except로 reaction 실패에 대비 → 안정적

**코드 근거:**
- `bot.py:841-843` (cmd_esc) - `await msg.set_reaction("⚡")` - ESC 키 전송 확인
- `bot.py:1003-1005` (on_message 이미지) - `await msg.set_reaction("📷")`
- `bot.py:1018-1021` (on_message 텍스트) - `await msg.set_reaction("✍")`

---

### 5. **메시지 청킹으로 안정적인 대용량 텍스트 전송**

Telegram의 메시지 길이 제한(4096자)을 초과하는 응답을 자동으로 분할합니다.

**강점:**
- 줄 단위로 청킹하여 가독성 유지 (`_chunk_text()`)
- 각 청크에 `[i/total]` 헤더로 순서 명시 → 사용자가 메시지 순서 파악 용이
- 크기 기반이 아닌 "줄 기반" 분할 → 긴 줄이 있어도 자동 재분할
- HTML `<pre>` 포맷으로 코드 블록 적절히 표현

**코드 근거:**
- `bot.py:699-721` (`_chunk_text()`)
  - 크기 초과 시 `while cur > size: chunks.append(s[:size])`로 강제 분할
  - 줄 단위 분할로 시맨틱 보존
- `bot.py:723-739` (`_send_output()`)
  - `total = len(chunks)` → `f"[{i}/{total}]\n"` 헤더 추가
  - HTML escape: `_html.escape(chunk)` → Markdown 메타 문자 충돌 방지
  - 0.2초 딜레이 (`await asyncio.sleep(0.2)`) → Telegram 레이트 리미트 회피

---

### 6. **HTML 포맷팅으로 메타 문자 충돌 제거**

Markdown이 아닌 HTML을 파스 모드로 사용하여 `*`, `_`, `` ` `` 등 특수문자의 오동작을 방지합니다.

**강점:**
- `<pre>` 태그 내 모든 텍스트가 literal로 취급 → 코드/로그 출력에 안전
- `&`, `<`, `>` 세 문자만 이스케이프 → 다른 기호는 그대로 표시
- fallback 로직으로 HTML 파싱 실패 시 plain text 재시도 → 안정성

**코드 근거:**
- `bot.py:724-739` (`_send_output()`)
  - `body = f"<pre>{_html.escape(chunk)}</pre>"`
  - `await app.bot.send_message(chat_id, body, parse_mode="HTML")`
  - 실패 시 plain text로 재전송: `except Exception as e2: print(...)`

---

### 7. **정교한 상태 머신: Busy 상태 추적과 Status 메시지 관리**

Claude의 작업 상태(Compacting, Thinking 등)를 감지하여 실시간 진행률 표시합니다.

**강점:**
- `was_busy` 플래그로 busy 진입/종료 엣지 감지 → 중복 메시지 방지
- 상태 메시지 ID 추적으로 매번 새 메시지 발송하지 않고 기존 메시지 편집
- 경과 시간 표시 (`elapsed = int(time.time() - self.busy_started_at)`) → 사용자가 진행 상황 파악
- 최근 25줄만 검색 → regex 성능 최적화

**코드 근거:**
- `bot.py:614-624` - busy 진입 시 메시지 생성:
  ```python
  if busy_now and not self.was_busy:
      label = busy_status(clean)
      msg = await app.bot.send_message(chat_id, f"⏳ {label}… (0s)")
      self.status_msg_id = msg.message_id
  ```
- `bot.py:627-641` - busy 지속 시 메시지 편집:
  ```python
  if busy_now:
      elapsed = int(time.time() - self.busy_started_at)
      new_text = f"⏳ {label}… ({elapsed}s)"
      await app.bot.edit_message_text(...)
  ```
- `bot.py:133-154` - `_STATUS_LINE_RE`와 `busy_status()` - 상태 라인 추출

---

### 8. **승인 프롬프트의 지능형 감지와 요약**

Claude Code의 도구 승인 요청을 정규식으로 감지하고, 도구명만 추출하여 간결하게 표시합니다.

**강점:**
- `APPROVAL_RE`로 "Do you want to proceed", "Allow X to", "(Y/n)" 등 다양한 형식 감지
- `summarize_approval()`으로 승인 박스 위에서 도구명만 추출 (Bash, Edit, Read 등)
- 최근 20줄 히스토리 기반 중복 전송 방지 → `_sent_keys` 리스트로 추적
- trust prompt와 approval은 별도 regex로 분리 → 오탐 감소

**코드 근거:**
- `bot.py:57-60` - `APPROVAL_RE` 정규식
- `bot.py:108-126` (`summarize_approval()`) - 역순 탐색로 "Do you want to" 찾은 후, 그 위에서 도구명 추출
- `bot.py:396` - `self._sent_keys: list[str] = []` - 최근 20개 히스토리
- `bot.py:460-470` (`_mark_sent()`) - 히스토리 추가 & 20개 초과 시 제거

---

### 9. **멀티 인스턴스 안전성: 세션 락 메커니즘**

같은 세션을 여러 Telegram 사용자가 동시에 resume하는 것을 락 파일로 방지합니다.

**강점:**
- `_acquire_lock()` - 파일 존재 여부로 atomic 락 획득 (race condition 안전)
- Stale 락 감지: tmux 세션이 죽었으면 자동 정리 후 재획득
- 락 해제는 소유 인스턴스만 수행 → 권한 관리
- `/unlock` 명령으로 사용자가 강제 해제 가능 (본인 chat_id 소유만)

**코드 근거:**
- `bot.py:226-245` (`_acquire_lock()`)
  - `fd = lp.open("x")` - exclusive create로 atomic
  - stale 감지: `if tmux_run(["has-session", "-t", owner_tmux]).returncode != 0`
  - 재귀 호출로 재획득
- `bot.py:247-257` (`_release_lock()`) - `if info and info[0] == TMUX` - 소유 확인
- `bot.py:846-868` (`cmd_unlock()`) - 본인 chat_id만 해제 가능

---

### 10. **세션 발견과 활동 시간 기반 정렬**

사용자가 `.claude/projects` 에서 세션을 자동 발견하고, 최근 활동순으로 정렬하여 빠른 접근을 제공합니다.

**강점:**
- `.jsonl` 파일을 재귀 탐색 (`rglob()`)
- 타임스탬프 파싱으로 정확한 활동 시간 추출 (fallback: mtime)
- 최근 60초 내 활동 세션은 제외 → 현재 활성 세션 충돌 방지
- 제목/마지막 메시지/프로젝트 슬러그 추출로 사용자가 세션 빠르게 식별

**코드 근거:**
- `bot.py:288-318` (`find_sessions()`)
  - `PROJECTS.rglob("*.jsonl")`
  - `_parse_session_msgs()` - 첫/마지막 메시지 + 타임스탐프 추출
  - `if now - activity_ts < 60: continue` - 60초 활성 세션 제외
  - `if _is_locked(p.stem): continue` - 락된 세션 제외
  - `candidates.sort(key=..., reverse=True)` - 최근순 정렬

---

### 11. **응답 추출의 다층 필터링**

tmux 화면에서 Claude의 실제 응답만 정교하게 추출하여 승인 박스, 입력 프롬프트, 이전 응답을 제거합니다.

**강점:**
- 마지막 ⏺ (응답 시작) 이후부터 시작
- 새로운 ❯ (사용자 입력) 이전까지 추출
- divider 라인 감지로 입력창 경계 식별
- "Do you want to proceed" 및 "Welcome back" 등 특수 요소를 경계로 사용

**코드 근거:**
- `bot.py:156-208` (`extract_last_response()`)
  - `if lines[i].lstrip().startswith("⏺"): start = i` - 응답 시작
  - `if lines[i].lstrip().startswith("❯ "): end = i` - 다음 입력 경계
  - divider 감지: `def is_divider(line)` - 긴 "─" 문자열
  - "Do you want to proceed" & "Welcome back" 감지로 특수 영역 제외

---

### 12. **이미지 다운로드와 경로 기반 처리**

Telegram에서 받은 이미지를 로컬 파일로 저장 후, 경로를 Claude에 전달합니다.

**강점:**
- `bot.get_file()` → `file.download_to_drive()`로 안전한 이미지 다운로드
- 타임스탬프 기반 파일명으로 중복 방지
- 캡션이 있으면 함께 전달 → 이미지 + 텍스트 맥락 유지
- 이미지 수신 실패 시 사용자에게 에러 메시지 반환

**코드 근거:**
- `bot.py:970-971` - `IMAGE_DIR.mkdir(exist_ok=True)` - 이미지 디렉토리 자동 생성
- `bot.py:988-1009` (`on_message` 이미지 처리)
  - `photo = msg.photo[-1]` - 최고 해상도 선택
  - `f = await ctx.bot.get_file(photo.file_id)`
  - `await f.download_to_drive(custom_path=str(fpath))`
  - `payload = f"[텔레그램 이미지 첨부: {fpath}]\n{caption}"`

---

### 13. **명령어와 필터의 명확한 분리**

Command handler와 Message handler를 구분하여 `/start`, `/end` 같은 명령과 일반 메시지를 안전하게 분리합니다.

**강점:**
- `CommandHandler` - `/start`, `/end`, `/esc`, `/unlock`, `/whoami` 전용
- `MessageHandler(filters.TEXT & ~filters.COMMAND)` - 일반 텍스트/이미지만 처리
- `filters.PHOTO` 추가로 이미지 첨부 메시지 명시적 처리
- 필터 조합으로 명령이 실수로 일반 메시지로 처리되지 않도록 보호

**코드 근거:**
- `bot.py:1050-1059` (`main()`)
  ```python
  app.add_handler(CommandHandler("start", cmd_start))
  app.add_handler(CommandHandler("end", cmd_end))
  app.add_handler(MessageHandler(
      (filters.TEXT & ~filters.COMMAND) | filters.PHOTO,
      on_message,
  ))
  ```

---

### 14. **구조화된 로깅으로 디버깅 용이성**

`_log()` 함수로 타임스탬프와 태그를 포함한 일관된 로그 형식을 유지합니다.

**강점:**
- `[HH:MM:SS] [TAG] message` 형식으로 시간대 파악 용이
- 태그별로 (예: `AI→BOT`, `USER→AI`, `AUTO-ACK`) 데이터 흐름 추적
- 승인 요약, busy 상태, 락 상태 등을 명확하게 기록

**코드 근거:**
- `bot.py:37-44` (`_log()`)
  ```python
  def _log(tag: str, msg: str = ""):
      ts = time.strftime("%H:%M:%S")
      print(f"[{ts}] [{tag}] {msg}")
  ```
- 사용 예시: `_log("AUTO-ACK", "trust prompt")`, `_log("AI-APPROVAL", summary)`, `_log("USER→AI", "ESC pressed")`

---

### 15. **권한 관리와 접근 제어**

`allowed_ids` 설정으로 특정 사용자만 봇을 사용하도록 제한합니다.

**강점:**
- 모든 명령/메시지 핸들러에서 `is_allowed()` 체크
- 접근 거부 시 사용자에게 chat_id를 알려주어 등록 과정 간소화
- `/whoami` 명령으로 자신의 chat_id 확인 가능

**코드 근거:**
- `bot.py:46-53` (`is_allowed()` & `deny()`)
  ```python
  def is_allowed(update) -> bool:
      return update.effective_chat.id in ALLOWED_IDS

  async def deny(update: Update):
      await update.effective_message.reply_text(
          f"접근 거부. ... 본인 확인용 ID: {update.effective_chat.id}"
      )
  ```
- 모든 핸들러에서: `if not is_allowed(update): await deny(update); return`

---

## 결론

이 프로젝트는 python-telegram-bot v20+의 비동기 API를 매우 효과적으로 활용합니다.

**핵심 강점:**
1. **비동기성** - Telegram 이벤트와 tmux 모니터링의 완전한 독립적 실행
2. **UX 지향** - Inline keyboard, reaction, status 메시지 편집으로 사용자 경험 극대화
3. **안정성** - 멀티 인스턴스 락, 중복 전송 방지, 에러 폴백 로직
4. **정교함** - 정규식 기반의 상태 감지, 메시지 청킹, 응답 추출 필터링
5. **관찰성** - 구조화된 로깅으로 시스템 동작을 명확하게 추적

이러한 설계는 Telegram의 제약 (메시지 크기, 레이트 리미트, 동시성)을 잘 이해하고, 각각에 대해 검증된 해결책을 적용한 결과입니다.
