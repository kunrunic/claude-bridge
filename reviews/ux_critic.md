# claude-bridge UX 비평 보고서

**검토일**: 2026년 4월 15일  
**대상**: Telegram Claude Code 원격 조작 브리지 봇  
**관점**: 모바일 Telegram 사용자 경험 중심  

---

## 문제점 목록

### 1. 혼동을 유발하는 락(Lock) 오류 메시지
**심각도**: HIGH  
**코드 근거**: `bot.py:965`

```python
await ctx.bot.send_message(q.message.chat_id, 
    "⚠️ 이 세션은 다른 인스턴스에서 사용 중입니다.")
```

**문제점**:
- 사용자가 /start로 자신의 세션을 재개하려 할 때 "다른 인스턴스"라는 메시지가 뜬다
- 실제로는 자신이 같은 세션에서 이전에 시작한 인스턴스가 점유하고 있는 상황
- 모바일 사용자는 여러 인스턴스 개념을 이해하지 못할 가능성 높음
- 사용자는 "누가 내 세션을 건드리고 있나?" 혼동 → 보안 우려 발생

**개선 방향**:
- 상황별 명확한 메시지 제공:
  - 같은 chat_id라면: "이미 이 봇에서 같은 세션으로 시작했습니다. /end로 종료하거나 기존 세션에 재연결하세요."
  - 다른 chat_id라면: "다른 사용자가 이 세션을 사용 중입니다."
- "인스턴스"라는 기술 용어 대체 (사용자 관점의 용어 사용)
- 복구 방법 제시: `/unlock` 명령어 안내

---

### 2. 세션 목록 UI의 전시 한계
**심각도**: MED  
**코드 근거**: `bot.py:785-793`

```python
def _build_start_kb(sessions: list[dict]) -> InlineKeyboardMarkup:
    kb = []
    for s in sessions:
        proj = f" [{s['project']}]" if s['project'] else ""
        label = f"[{s['mtime']}]{proj} {s['title']}"
        kb.append([InlineKeyboardButton(label, callback_data="resume:" + s["id"])])
```

**문제점**:
- 세션 버튼 라벨이 고정 너비로 설정되지 않아 모바일에서 텍스트 잘림
- `{s['title']}`이 최대 40자(`bot.py:313`)로 제한되어 있으나, 앞에 `[시간][프로젝트]` 추가되면서 실제 보이는 제목이 더 짧아짐
- 세션 ID가 8자로 보이지 않으므로 사용자가 특정 세션 구분 어려움
- 세션 최대 8개(`bot.py:288`) 제한으로 그 이상은 선택 불가능 (사용자에게 숨겨짐)

**개선 방향**:
- 모바일 Telegram 표준 너비(~300px)에 맞춰 버튼 라벨 구성:
  ```
  [04/15 10:30] 프로젝트-이름
  build-fix의 첫 메시지... (truncate to ~35 chars)
  ```
- 세션이 8개를 초과하면 페이지네이션 추가 (◀ 이전 / 다음 ▶ 버튼)
- 또는 세션 검색/필터 기능 제공

---

### 3. 피드백 없는 승인 대기 구간
**심각도**: HIGH  
**코드 근거**: `bot.py:536-543` (신뢰 프롬프트), `bot.py:566-591` (승인 프롬프트)

```python
if is_trust_prompt(clean):
    if not self.last_sent.endswith("__trust_ack__"):
        send_key("Enter")
        _log("AUTO-ACK", "trust prompt")
        await app.bot.send_message(chat_id, "폴더 신뢰 프롬프트 자동 승인")
        # ... 1초 대기 후 계속
```

**문제점**:
- 신뢰 프롬프트 자동 승인은 사용자에게 고지하지만, 실제 Claude 업그레이드 승인 화면은 보이기 전에 처리됨
- 사용자가 화면을 보고 있을 때 갑자기 "승인됨" 메시지 없이 다음 단계로 진행
- 네트워크 지연이 있을 때: Claude 화면엔 승인 프롬프트가 남아있는데 봇은 이미 승인한 상태 → 동기화 오류
- 모바일에서 스크롤이 필요한 상황에서 메시지가 뒤로 묻힐 수 있음

**개선 방향**:
- Claude 승인 화면이 보인 후 사용자에게 인라인 버튼으로 Yes/No 선택지 제공 (현재는 이미 구현됨)
- 신뢰 프롬프트도 마찬가지로 자동이 아닌 사용자 확인 후 처리하도록 변경
- 또는 신뢰 프롬프트는 자동이지만, 어떤 폴더/경로를 신뢰했는지 명시하는 메시지 추가:
  ```
  폴더 신뢰: /Users/kunrunic/my-project ✅
  ```

---

### 4. 작업 중 상태의 피드백 부족
**심각도**: MED  
**코드 근거**: `bot.py:614-644` (busy 상태 메시지)

```python
if busy_now and not self.was_busy:
    self.was_busy = True
    self.busy_started_at = time.time()
    label = busy_status(clean)
    self.last_status_label = label
    _log("AI-BUSY", label)
    try:
        msg = await app.bot.send_message(chat_id, f"⏳ {label}… (0s)")
        self.status_msg_id = msg.message_id
```

**문제점**:
- 상태 메시지 업데이트는 최대 2초 간격(`bot.py:643`)
- 모바일에서는 푸시 알림 지연 때문에 실제로는 3~5초 뒤에 보임
- 30초 이상 작업할 때 사용자는 "정말 작업 중인가?" 의문 발생 가능
- Compacting/Thinking 등의 상태만 표시되고, 무엇을 하고 있는지 맥락 부족

**개선 방향**:
- 상태 메시지 업데이트 간격을 1초로 단축
- 상태 라벨에 추가 컨텍스트 제공:
  ```
  ⏳ Bash: ls -la (3s)
  ⏳ Tool use (Bash) - 5초 경과
  ```
- 30초 이상 대기하면 "여전히 작업 중입니다. /esc로 취소할 수 있습니다." 안내 메시지 추가
- 한도 초과 시처럼 주기적 상태 업데이트 (기존 메시지 편집이 아닌 새 메시지)

---

### 5. /unlock 커맨드의 낮은 발견성
**심각도**: MED  
**코드 근거**: `bot.py:846-868` (구현), `bot.py:1027-1033` (커맨드 목록)

```python
BotCommand("unlock", "세션 락 강제 해제"),
```

**문제점**:
- /unlock 커맨드는 /start 메뉴에 보이지 않음 (메뉴는 5개 커맨드만 표시)
- 사용자가 락 오류를 만났을 때 "어떻게 해결하지?" 상태에서 /unlock 존재를 모를 가능성 높음
- 오류 메시지(`bot.py:965`)에서 /unlock 안내가 없음
- 기술 초심자는 "락"이 뭔지 모르므로 커맨드 이름부터 혼동

**개선 방향**:
- 락 오류 메시지에 직접 해결책 포함:
  ```
  ⚠️ 이 세션은 다른 곳에서 사용 중입니다.
  👉 해결: /unlock 입력
  ```
- 또는 버튼으로 제공:
  ```
  [락 해제] 버튼 → callback_data="force_unlock"
  ```
- 커맨드 설명 개선: "세션 락 강제 해제" → "세션 재개 문제 해결"

---

### 6. /esc 커맨드의 불명확한 동작
**심각도**: MED  
**코드 근거**: `bot.py:831-843`

```python
async def cmd_esc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ESC 키 전송 (승인창 취소 / 작업 중단)"""
    if not is_allowed(update):
        await deny(update); return
    if tmux_run(["has-session", "-t", TMUX]).returncode != 0:
        await update.message.reply_text("세션이 없습니다.")
        return
    send_key("Escape")
    _log("USER→AI", "ESC pressed")
    try:
        await update.message.set_reaction("⚡")
    except Exception:
        pass
```

**문제점**:
- 커맨드명 /esc는 키 입력이라는 의도 불명확
- 사용자는 "무엇을 취소하는 건지" 확실하지 않음
- Telegram 모바일에서 /esc 타이핑이 불편 (슬래시 커맨드는 추천 목록 안 보일 수 있음)
- 작업 중단과 승인 거부를 구분하지 않음 (실제로는 상황에 따라 다름)

**개선 방향**:
- 커맨드명 변경: /cancel 또는 /stop (더 직관적)
- 인라인 버튼 추가 (커맨드 입력 대체):
  ```
  [⏹ 작업 중단] 버튼 → Escape 전송
  ```
- 피드백 메시지 구체화:
  ```
  ❌ 작업을 중단했습니다.
  ```
  (현재는 ⚡ 반응만 표시)

---

### 7. 세션 시작 흐름의 혼동
**심각도**: MED  
**코드 근거**: `bot.py:804-821` (cmd_start), `bot.py:878-890` (reattach vs force_new)

```python
if tmux_run(["has-session", "-t", TMUX]).returncode == 0:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("기존 세션 유지 (재연결)", callback_data="reattach")],
        [InlineKeyboardButton("새로 시작 (기존 종료)", callback_data="force_new")],
    ])
```

**문제점**:
- "기존 세션 유지 (재연결)" vs "새로 시작 (기존 종료)"는 사용자에게 혼동
- "기존 세션"이 무엇을 의미하는지 불명확:
  - tmux 세션인가? Claude 세션인가?
  - 지금 진행 중인 작업인가? 저장된 이전 세션인가?
- 실제 동작:
  - reattach: 현재 tmux의 Claude 프로세스에 재연결 (진행 중인 작업 유지)
  - force_new: tmux 종료 → 새 Claude 실행 또는 저장된 세션 선택
- 모바일 사용자는 버튼 텍스트만으로는 구분 어려움

**개선 방향**:
- 메시지 명확화:
  ```
  이미 실행 중인 Claude 세션이 있습니다. 원하는 작업을 선택하세요:
  
  [계속 작업] - 지금 진행 중인 작업으로 돌아가기
  [새로 시작] - 진행 중인 작업 중단하고 다른 세션 열기
  ```
- 또는 현재 세션의 첫 번째 메시지 미리보기:
  ```
  진행 중: "GitHub PR review..."
  [계속] [새로 시작]
  ```

---

### 8. 승인/거부 후 상태 불명확
**심각도**: LOW  
**코드 근거**: `bot.py:892-918` (approve_yes/approve_no)

```python
elif data == "approve_no":
    if not bridge.awaiting_approval:
        await q.answer("이미 처리됨", show_alert=False)
        return
    bridge.awaiting_approval = False
    _log("USER-ACK", "denied")
    send_key("Down")
    await asyncio.sleep(0.15)
    send_key("Enter")
    summary = bridge.last_approval_summary or "거부"
    try:
        await q.edit_message_text(f"❌ 거부 · {summary}", reply_markup=None)
```

**문제점**:
- 거부(approve_no) 시 Down 키 입력 후 Enter는 의도적이지만, 사용자는 모름
- Claude 승인 화면의 "No" 옵션을 자동 선택하려는 의도인데, 설명이 없음
- 거부 후 Claude가 어떻게 반응할지 예측 불가 (도구 취소? 전체 작업 중단?)
- 네트워크 지연이 있으면 Down 선택 실패할 가능성

**개선 방향**:
- 거부 확인 전 사용자에게 경고:
  ```
  정말 거부하시겠습니까? Claude의 작업이 중단될 수 있습니다.
  [거부] [취소]
  ```
- 거부 후 예상 결과 안내:
  ```
  ❌ 거부됨 · Bash
  Claude가 다른 방법을 시도합니다.
  ```
- 또는 거부 대신 중단 옵션 분리:
  - "거부" → Claude가 선택적으로 대안 시도
  - "중단" → 전체 작업 중단 (/esc와 같음)

---

### 9. 이미지 첨부 후 피드백
**심각도**: LOW  
**코드 근거**: `bot.py:986-1009`

```python
if msg.photo:
    # ... 이미지 다운로드 ...
    _log("USER→BOT", f"image {fname} ({photo.file_size or 0} bytes)")
    
    # Claude에 경로 + 캡션 전달
    payload = f"[텔레그램 이미지 첨부: {fpath}]"
    if caption:
        payload += f"\n{caption}"
    send_input(payload)
    # ...
    try:
        await msg.set_reaction("📷")
    except Exception:
        pass
```

**문제점**:
- 이미지 전송 후 📷 반응만 표시되고, 텍스트 피드백 없음
- 사용자는 이미지가 정확히 어디에 저장됐는지 모름
- 오류 발생 시 ("이미지 수신 실패") 명시되지만, 성공 시 다음 단계 불명확
- Claude가 이미지를 처리했는지 확인하려면 다음 응답을 기다려야 함

**개선 방향**:
- 성공 피드백 메시지 추가:
  ```
  ✅ 이미지 첨부됨 (1.2MB)
  Claude가 분석하는 중...
  ```
- 또는 텍스트 + 반응 조합:
  ```
  [이미지 수신] + 📷 반응
  ```
- 이미지 경로는 로그에만 표시 (사용자 메시지에는 노출 X)

---

### 10. 세션 활동 시간 표시의 모호성
**심각도**: LOW  
**코드 근거**: `bot.py:312-314`

```python
candidates.append({
    # ...
    "mtime": datetime.fromtimestamp(activity_ts).strftime("%m/%d %H:%M"),
    "title": title[:40],
    # ...
})
```

**문제점**:
- "[04/15 10:30]"이 무엇을 나타내는지 불명확:
  - 세션 생성 시간? 마지막 활동? 마지막 메시지?
- 사용자는 "어느 세션이 최신인지" 판단하기 위해 UI 로직을 추측해야 함
- 글로벌 시간대 정보 없음 (UTC? 로컬 시간?)

**개선 방향**:
- 라벨 명시:
  ```
  [마지막 활동: 04/15 10:30]
  ```
- 또는 상대 시간 사용:
  ```
  [5분 전] 
  [어제 3시]
  ```
- 세션 정렬 기준 설명 추가: "최신 활동 순으로 정렬됨"

---

## 요약: 우선순위별 개선 항목

| 우선순위 | 문제 | 영향도 |
|---------|------|--------|
| P0 | 혼동 오류 메시지 (다른 인스턴스) | 보안 우려, 사용자 혼동 |
| P0 | 승인 화면 피드백 부족 | 동기화 오류, UX 단절 |
| P1 | 세션 목록 UI 한계 | 다수 세션 사용자 경험 악화 |
| P1 | /unlock 발견성 | 문제 해결 어려움 |
| P2 | /esc 커맨드 명확화 | 학습곡선 상승 |
| P2 | 시작 흐름 혼동 | 잘못된 선택 유도 |
| P3 | 이미지 피드백 | 완성도 |

---

## 추가 권장사항

### 모바일 UX 최적화
1. **버튼 크기**: 모바일 Telegram의 터치 타겟은 최소 48x48px
   - 현재 인라인 버튼이 작을 수 있으니 텍스트 길이 확인 필요
   
2. **메시지 길이**: 모바일에서는 200자 이상의 메시지가 스크롤 필요
   - 진행 상태 업데이트 메시지는 짧게 유지

3. **피드백 시간**: 모바일 푸시 알림은 2~5초 지연 발생
   - 상태 업데이트 간격을 2초 이상으로 설정 (현재 2초는 적절)

### 메시지 톤
- 기술 용어 최소화 (인스턴스 → "다른 곳", 락 → "사용 중")
- 행동 지시 명확화 ("이미 실행 중입니다" → "다른 세션을 열려면 [새로 시작] 선택")
- 이모지 활용 적극화 (✅, ❌, ⏳ 등)

---

**문서 작성 완료**: 2026-04-15
