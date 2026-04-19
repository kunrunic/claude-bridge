# claude-bridge UX 검토: 강점 분석

> 모바일 Telegram 환경에서 Claude Code를 원격 조작하는 UX에 대한 긍정적 관점의 분석입니다.

---

## 강점 목록

### 1. 명확한 사용자 피드백 메시지

#### 1.1 단계별 상태 안내
- **접근 거부**: `"접근 거부. 이 봇은 등록된 사용자만 사용할 수 있습니다.\n본인 확인용 ID: {id}"` (bot.py:51-54)
  - 거부 이유와 해결 방법(ID 확인)을 함께 전달하여 사용자가 다음 행동을 명확히 알 수 있음
  
- **세션 상태**: 세션 없음 vs 실행 중 vs 승인 대기 등 상황별로 다른 메시지 제공 (bot.py:809-821)
  - 사용자가 현재 상황을 즉시 파악 가능
  
- **시작 단계 피드백**: `"시작 중: 세션 재개 ({session_id})\n폴더: {cwd}"` (bot.py:950-954)
  - 진행 상황과 함께 어떤 폴더에서 실행되는지 명확히 표시하여 신뢰감 제공

#### 1.2 긍정적 완료 알림
- **자동 승인 성공**: `"폴더 신뢰 프롬프트 자동 승인"` (bot.py:540)
  - 봇이 자동으로 처리한 작업을 사용자에게 알려 투명성 확보
  
- **승인/거부 결과**: `"✅ 승인 · {도구명}"`, `"❌ 거부 · {도구명}"` (bot.py:901, 916)
  - 이모지와 함께 도구 이름을 표시하여 어떤 작업을 승인했는지 한눈에 파악 가능

#### 1.3 문제 상황에 대한 상세 안내
- **프로세스 종료**: `"Claude 프로세스가 종료되었습니다.\n마지막 출력:\n\`\`\`\n{최근 1500자}\n\`\`\`"` (bot.py:522-526)
  - 단순 종료 알림을 넘어 마지막 상태를 보여주어 문제 진단 가능
  
- **사용량 한도**: `"⚠️ Claude 사용량 한도에 도달했습니다.\n{리셋 시간}에 리셋됩니다."` (bot.py:553-555)
  - 문제만 알리지 않고 언제 해결되는지까지 안내하여 사용자 답답함 감소

---

### 2. 진행 상태 표시 (⏳ busy 메시지, 경과 시간)

#### 2.1 실시간 작업 상태 표시
- **진행 상태 라벨 추출**: `busy_status()` 함수 (bot.py:141-155)
  - 화면의 상태 라인에서 "Compacting", "Thinking", "Running", "Tool use" 등을 감지하여 사용자에게 정확한 상황 전달
  - ANSI 코드 제거하고 "esc to interrupt" 같은 UI 요소만 걸러내는 정밀한 처리

#### 2.2 경과 시간 표시
- **Busy 진입 감지 및 타이머**: `self.busy_started_at` 설정 (bot.py:616)
  - 작업 시작 시간을 기록하여 경과 시간 계산 가능
  
- **실시간 업데이트**: 2초마다 경과 시간 갱신 (bot.py:629-641)
  ```python
  new_text = f"⏳ {label}… ({elapsed}s)"
  await app.bot.edit_message_text(chat_id=chat_id, message_id=self.status_msg_id, text=new_text)
  ```
  - 같은 메시지를 편집하여 메시지 창의 스팸을 방지하면서 진행 상황을 실시간으로 표시
  - 모바일에서 채팅창 스크롤이 지저분해지지 않도록 배려

#### 2.3 상태 전환의 부드러운 처리
- **Busy 상태 진입/종료 감지**: 엣지 트리거로 상태 메시지 생성/삭제 (bot.py:614-653)
  - 상태 메시지를 정확한 시점에만 생성/삭제하여 메시지 공해 최소화

---

### 3. 승인/거부 인라인 키보드 흐름

#### 3.1 직관적인 버튼 레이아웃
```python
InlineKeyboardButton("Yes (승인)", callback_data="approve_yes"),
InlineKeyboardButton("No (거부)",  callback_data="approve_no"),
```
(bot.py:763-764)
- 한영혼용으로 아무나 쉽게 이해 가능
- 모바일에서 터치하기 좋은 크기의 버튼 2개 배치

#### 3.2 승인/거부 맥락 표시
- **승인 박스 추출**: `_approval_box()` 함수 (bot.py:741-758)
  - "Do you want to proceed?" 위쪽 divider부터 승인 요청까지만 추출하여 사용자에게 명확한 맥락 제공
  - 최대 1200자 이내로 제한하여 모바일 화면에 맞게 표시 (bot.py:767-768)

#### 3.3 빠른 응답 처리
- **버튼 클릭 즉시 처리**: `on_callback()` 함수에서 즉시 `await q.answer()` 호출 (bot.py:875)
  - Telegram의 "⏳" 로딩 표시를 방지하여 사용자에게 빠른 반응성 제공
  
- **메시지 즉시 업데이트**: 버튼 클릭 후 메시지를 `edit_message_text()`로 "✅ 승인 · {도구명}"으로 변경 (bot.py:901)
  - 사용자는 자신의 선택이 반영됨을 즉시 확인 가능

#### 3.4 승인 요청 우선순위 관리
- **Pre-approval flush**: 승인 창이 나타나기 직전 이전 응답을 전송 (bot.py:567-577)
  - 사용자가 승인 대기 중에도 이전 작업 결과를 먼저 볼 수 있어 정보 손실 방지
  
- **승인 중복 방지**: `if not self.awaiting_approval:` 체크 (bot.py:894, 906)
  - 이미 처리된 승인을 다시 처리하려는 시도 방어

---

### 4. 에러 시 안내 메시지 품질

#### 4.1 문제 진단 정보 포함
- **네트워크 문제 대응**: 메시지 전송 실패 시 여러 방식으로 재시도 (bot.py:731-738)
  ```python
  try:
      await app.bot.send_message(chat_id, body, parse_mode="HTML")
  except Exception:
      try:
          await app.bot.send_message(chat_id, header + chunk)  # plain text fallback
  ```
  - 포맷팅 오류 시에도 내용은 전달하려는 배려

#### 4.2 구체적인 오류 상황별 안내
- **세션 잠금**: `"⚠️ 이 세션은 다른 인스턴스에서 사용 중입니다."` (bot.py:965)
  - 단순 실패가 아니라 왜 실패했는지(다른 인스턴스 사용 중) 설명
  
- **도구 검색 실패**: `"/unlock` 명령어로 수동 해제 가능하도록 안내 (bot.py:846-868)
  - 사용자가 문제를 직접 해결할 방법 제시

#### 4.3 타임스탬프 기반 오류 추적
- **구조화된 로깅**: `_log()` 함수로 모든 주요 이벤트 기록 (bot.py:38-45)
  - 서버 로그에서 정확한 시간별로 이벤트 추적 가능하여 버그 재현/진단 용이

---

### 5. 모바일 친화적 출력 (80col, 청킹)

#### 5.1 터미널 너비 제한 (80 columns)
```python
tmux_run([
    "new-session", "-d", "-s", TMUX,
    "-x", "80", "-y", "50",  # 80 cols, 50 rows
    ...
])
```
(bot.py:418-421)
- 표준 터미널 너비인 80 컬럼으로 설정하여 모바일/데스크탑 모두 예측 가능한 레이아웃
- 주석에도 명시적으로 "모바일 친화적"이라고 표기 (bot.py:417)

#### 5.2 스마트 텍스트 청킹
```python
def _chunk_text(text: str, size: int = 3500) -> list[str]:
    """긴 텍스트를 줄 단위로 size 이하로 나눔"""
```
(bot.py:699-721)
- **행 기준 분할**: 텍스트를 줄 단위로 나누어 가독성 보존
  - 한 줄이 size보다 크면 강제 분할하여 극단적인 경우도 처리
  
- **3500자 단위**: Telegram의 메시지 길이 제한(4096자)을 고려한 보수적인 설정
  - 안전 마진을 고려한 설계

#### 5.3 청킹된 메시지에 순서 표시
```python
header = f"[{i}/{total}]\n" if total > 1 else ""
```
(bot.py:728)
- 여러 메시지로 나뉘었을 때 "[1/3]", "[2/3]" 형태로 순서를 명시
- 사용자가 메시지 순서를 놓친 경우에도 복구 가능

#### 5.4 포맷팅 선택
```python
body = f"{header}<pre>{_html.escape(chunk)}</pre>"
```
(bot.py:730)
- **HTML `<pre>` 사용**: Markdown의 `_`, `*` 충돌을 피하고 코드 포맷 유지
  - 폰트가 고정되어 터미널 출력을 정확히 재현
  
- **HTML 이스케이프**: `< > &` 만 이스케이프하여 원본 형식 최대 보존

---

### 6. 중복 전송 방지 메커니즘

#### 6.1 응답 정규화를 통한 중복 감지
```python
def _response_key(self, text: str) -> str:
    """중복 판정용 정규화 키 — 빈 줄/공획 무시한 정규화 문자열"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)

def _already_sent(self, text: str) -> bool:
    """최근 전송 내역(최대 20개)과 비교해 중복인지 확인"""
    key = self._response_key(text)
    return key in self._sent_keys
```
(bot.py:455-463)
- 공백 차이를 무시하고 실질적으로 같은 응답인지 판정
- 재전송 시도나 네트워크 지연으로 인한 중복 방지

#### 6.2 최근 20개 히스토리 유지
```python
self._sent_keys.append(key)
if len(self._sent_keys) > 20:
    self._sent_keys.pop(0)
```
(bot.py:467-470)
- 메모리 효율성과 충돌 방지의 균형 맞춤
- 짧은 시간 안에 반복되는 응답만 방지하되, 오래된 응답과의 중복은 허용

#### 6.3 해시 기반 상태 추적
```python
h = hashlib.md5(clean.encode()).hexdigest()
if h != self.last_hash:
    self.last_hash = h
    pending = clean
    settle = 0
else:
    settle += 1
```
(bot.py:656-662)
- MD5 해시로 화면 변화를 빠르게 감지
- settle 카운터로 안정화될 때까지 대기하여 부분적 업데이트 무시

---

### 7. 자동 처리의 투명성

#### 7.1 트러스트 프롬프트 자동 승인
```python
if is_trust_prompt(clean):
    if not self.last_sent.endswith("__trust_ack__"):
        send_key("Enter")
        _log("AUTO-ACK", "trust prompt")
        await app.bot.send_message(chat_id, "폴더 신뢰 프롬프트 자동 승인")
```
(bot.py:536-540)
- 자동으로 처리하되 사용자에게 반드시 알림
- 마지막 상태에 `__trust_ack__` 태그를 붙여 중복 자동 승인 방지

#### 7.2 한도 초과 감지의 상세함
```python
reset_match = re.search(r"resets\s+(\d+(?::\d+)?(?:am|pm)?)\s*\(([^)]+)\)", clean, re.IGNORECASE)
if reset_match:
    reset_info = f"{reset_match.group(1)} ({reset_match.group(2)})"
```
(bot.py:548-550)
- 리셋 시간을 자동으로 파싱하여 사용자에게 정확한 정보 제공
- 정규식으로 여러 시간 포맷 지원 (예: "2:30pm (PDT)")

---

### 8. 세션 안정성과 복구 메커니즘

#### 8.1 멀티 인스턴스 세션 잠금
```python
def _acquire_lock(session_id: str, chat_id: int) -> bool:
    """락 획득. 이미 다른 인스턴스가 점유 중이면 False"""
    lp = _lock_path(session_id)
    try:
        fd = lp.open("x")  # exclusive create
```
(bot.py:226-234)
- 파일 락을 통해 여러 봇 인스턴스가 같은 세션을 동시에 사용하지 않도록 보호
- 사용자가 여러 봇을 동시에 실행해도 안전

#### 8.2 Stale 락 자동 정리
```python
if info:
    owner_tmux, _ = info
    if tmux_run(["has-session", "-t", owner_tmux]).returncode != 0:
        lp.unlink(missing_ok=True)
        return _acquire_lock(session_id, chat_id)
```
(bot.py:237-242)
- 실제로 프로세스가 살아있지 않은 stale 락을 자동으로 감지하고 정리
- 봇 비정상 종료 후에도 세션 복구 가능

#### 8.3 부팅 시 응답 중복 방지
```python
seed_clean = strip_ansi(pane_output()).strip()
seed_resp  = extract_last_response(seed_clean)
if seed_resp:
    self.last_sent = seed_resp
    self._mark_sent(seed_resp)
    _log("BOOT-SEED", f"마지막 ⏺ 블록 {len(seed_resp)}자 무시 처리")
```
(bot.py:499-503)
- 봇 재시작 시 이전 응답을 시드로 설정
- 사용자가 이미 본 응답을 다시 전송하지 않음

---

### 9. 사용자 제어의 명확한 아이콘/이모지 피드백

#### 9.1 작업 상태를 시각적으로 표현
```python
await msg.set_reaction("📷")  # 이미지 수신
await msg.set_reaction("✍")   # 텍스트 포워드
await msg.set_reaction("⚡")   # ESC 키 전송
```
(bot.py:1003, 1019, 841)
- 텍스트만이 아닌 이모지 반응으로 사용자의 액션이 수신되었음을 즉시 확인
- 모바일에서 한눈에 인식 가능한 시각 피드백

#### 9.2 진행 상태 이모지
- `"⏳ {status}… ({elapsed}s)"` - 진행 중 (bot.py:621)
- `"✅ 승인 · {도구}"` - 성공 (bot.py:901)
- `"❌ 거부 · {도구}"` - 거부 (bot.py:916)
- `"⚠️ 사용량 한도"` - 경고 (bot.py:553)

---

### 10. 명령어 발견성 (Commands 메뉴)

```python
await app.bot.set_my_commands([
    BotCommand("start",  "세션 목록 / 새 세션 시작"),
    BotCommand("end",    "현재 세션 종료"),
    BotCommand("esc",    "ESC 키 전송 (취소/중단)"),
    BotCommand("unlock", "세션 락 강제 해제"),
    BotCommand("whoami", "내 chat_id 확인 (관리자 등록용)"),
])
```
(bot.py:1027-1033)
- Telegram의 자동완성 명령어 메뉴에 모든 명령어와 설명 등록
- 사용자가 "/" 입력 시 명령어를 쉽게 발견 가능
- 각 명령어에 목적을 한줄로 설명하여 사용자 학습곡선 완화

---

### 11. 세션 컨텍스트 보존

#### 11.1 작업 디렉토리 복구
```python
cwd = get_session_cwd(session_id) if session_id else default_cwd
if not cwd:
    cwd = default_cwd
wrapped = f"cd {cwd!r} && {claude_cmd}"
```
(bot.py:412-415)
- 이전 세션의 작업 디렉토리를 추출하여 복구
- 사용자가 같은 프로젝트에서 계속 작업 가능

#### 11.2 세션 메타데이터 표시
```python
label = f"[{s['mtime']}]{proj} {s['title']}"
```
(bot.py:789)
- 마지막 활동 시간 `[mm/dd HH:MM]`
- 프로젝트 이름 `[project-slug]`
- 첫 명령어/마지막 명령어 (40자 제한)
- 사용자가 세션을 선택할 때 충분한 정보 제공

---

### 12. 이미지 업로드 지원

```python
if msg.photo:
    photo = msg.photo[-1]  # 가장 큰 해상도
    f = await ctx.bot.get_file(photo.file_id)
    await f.download_to_drive(custom_path=str(fpath))
    payload = f"[텔레그램 이미지 첨부: {fpath}]\n{caption}"
    send_input(payload)
```
(bot.py:986-1000)
- 모바일 Telegram의 주요 강점인 이미지 송수신을 활용
- 사용자가 스크린샷/설계도/에러 메시지 이미지를 직접 Claude에 전달 가능
- 파일 경로와 함께 캡션 지원으로 맥락 제공

---

### 13. 권한 모드 토글

```python
bridge.perm_label()  # "[권한 확인 ON]  탭하면 스킵" 또는 "[권한 스킵 ON]  탭하면 OFF"
```
(bot.py:451-453)
- 사용자가 권한 확인 모드를 쉽게 전환 가능
- 라벨이 명확하게 현재 상태와 다음 상태를 표시 (UX 하이라이트)
- 세션 실행 중이면 자동 재시작하여 즉시 적용

---

## 종합 평가

이 프로젝트는 **모바일 Telegram 환경을 철저히 고려한 설계**를 보여줍니다:

1. **명확한 피드백**: 모든 주요 작업에 상태 메시지/이모지 반응 제공
2. **진행 상황 시각화**: ⏳ 이모지와 경과 시간으로 사용자가 기다릴 수 있게 배려
3. **안정성**: 멀티 인스턴스 잠금, stale 락 정리, 중복 방지 등으로 견고함
4. **접근성**: 80컬럼 레이아웃, 3500자 청킹, HTML 포맷으로 모바일 가독성 최적화
5. **투명성**: 자동 처리 작업을 반드시 사용자에게 알려 신뢰 구축
6. **복구력**: 부팅 시드, 세션 복구, 디렉토리 보존으로 연속성 보장

모바일 앱 수준의 사려 깊은 UX 설계가 돋보입니다.
