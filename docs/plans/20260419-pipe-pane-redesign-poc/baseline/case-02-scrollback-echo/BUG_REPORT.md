# 버그 리포트 — 20260417_164104

## 현상

14:18 `/start` 로 새 세션을 시작한 직후, 사용자가 텔레그램으로 **이전 세션의 Edit 승인 박스 스크린샷을 텍스트로 붙여넣어** 공유했다. 그 메시지 본문에 `Do you want to make this edit to parser.py?` 와 `❯ 1. Yes` 가 **원문 그대로** 포함돼 있었기 때문에, 봇은 해당 텍스트가 pane 스크롤백에 echo 된 것을 **라이브 승인창으로 오인**해서 `pre-approval` → `✅ 승인 · Read` 를 반복 발사했다. 사용자가 승인 버튼을 눌러도 pane 의 echo 가 그대로라 다음 tick 에서 다시 매치되어 루프가 계속됐다.

이후 15:14 에 "어떤 상태야?" 를 보낸 뒤에도, 그리고 15:15 에 `def is_approval(text: str) -> bool:` 코드 스니펫을 공유한 뒤에도 동일하게 `[AI-APPROVAL] 승인` 이 계속 발사됐다 — 이 때는 `summarize_approval` 가 근처에서 인식 가능한 툴명을 못 찾아 fallback "승인" 이 라벨로 들어갔다.

## 타임라인 재구성

| 시각 | 소스 | 이벤트 |
|------|------|--------|
| 14:14:22 | log | `[AI-BUSY] Compacting conversation` (이전 세션에서 compact 진행) |
| 14:17:49 ~ 14:19:07 | log | `MONITOR cancelled — cleanup` + `first boot (403 chars)` 3회 반복 (세션 재기동 과정) |
| 14:19:28 | log | `[USER→BOT] 이제 cb1 쪽 코드 변경. 먼저 parser 기존 패턴 확인. Searched for 1 patter…` **← 본문에 `Do you want to make this edit` + `❯ 1. Yes` + `Esc to cancel · Tab to amend` 포함** |
| 14:19:28 | log | `[BOT→AI] forwarded to Claude` — `send-keys -l <text>` + Enter 로 pane 에 literal 입력됨 |
| 14:20:19 | log | `[AI→BOT] pre-approval (28 chars)` ← Claude 의 실제 Edit 승인 박스가 아직 안 떴을 가능성. 사용자 echo 로 인한 첫 오탐 |
| 14:20:20 | log | `[AI-APPROVAL] Read` ← `summarize_approval` 가 echo 위쪽 어딘가에서 `Read` 툴명 매치 |
| 14:20 (tg) | CB2 | `⏺ Update(bridge/parser.py)` + `✅ 승인 · Read` |
| 14:25:54 | log | `[USER-ACK] approved` (사용자가 Yes 버튼 누름) |
| 14:25:55 | log | `[AI-APPROVAL] Read` ← **같은 echo 에 재매치, 재발사 시작** |
| 14:27:09 ~ 14:27:32 | log | `[AI-APPROVAL] Read` × 5 (approved 사이사이, 같은 원인) |
| 14:28:09 | log | `[USER→BOT] ㅋㅋ 근데 지금 예시 메시지가 승인으로 계속 날아오네` ← 사용자가 루프 자각 |
| 15:14:38 | log | `[USER→BOT] 어떤상태야?` |
| 15:14:48 | log | `[AI-APPROVAL] 승인` ← 근처에 툴명 없어 fallback "승인" |
| 15:15:13 | log | `[USER→BOT] 지금 아래 매시지가 계속 날라와 def is_approval(text: str) -> bool:` ← 사용자가 `is_approval` 코드 공유 |
| 15:17:01 | log | `[USER→BOT] image tg_1776406619.jpg` (스크린샷 추가 전달) |
| 15:18:33 ~ 15:19:04 | log | `[AI-APPROVAL] 승인` × 3 (같은 echo 재매치) |

## 원인 가설

### 가설 A (확정적) — `is_approval` 이 scrollback 에 echo 된 사용자 페이스트를 라이브 승인창으로 오인

**근거:**

- `bridge/parser.py:56-63` 현행(및 pending diff) `is_approval`:
  ```python
  def is_approval(text: str) -> bool:
      tail = "\n".join(text.splitlines()[-60:])
      if not APPROVAL_RE.search(tail):
          return False
      return bool(_APPROVAL_CHOICE_RE.search(tail))   # pending diff
  ```
  `APPROVAL_RE = "Do you want to proceed|..."`, `_APPROVAL_CHOICE_RE = r"^\s*❯?\s*1\.\s+Yes\b"`.

- 14:19:28 사용자 메시지는 다음을 **원문 그대로** 포함:
  ```
  Do you want to make this edit to parser.py?
  ❯ 1. Yes
    2. Yes, allow all edits during this session (shift+tab)
    3. No
  ```
  → `APPROVAL_RE` 와 `_APPROVAL_CHOICE_RE` 모두 매치. **pending diff 의 choice 공존 요구는 이 케이스를 걸러내지 못한다.**

- `bridge/tmux.py:51-56` 의 `send_input` 는 `send-keys -l <text>` (literal paste) + Enter. pane 스크롤백에 그대로 echo 됨.

- `bridge/core.py:415` 에서 `is_approval` 가 `is_busy` 보다 **먼저** 검사되기 때문에, Claude 가 busy 중이라도 echo 매치가 우선 발사된다.

- 승인 콜백 `bridge/receiver.py:164` 도 `is_approval(clean)` 로 확인 후 `Enter` 를 보내는데, echo 가 남아있는 한 이 확인도 **True 를 리턴**해서 happy-path 로 빠진다. 따라서 "유저 Yes → Enter 전송 → pane 변화 없음 → 다음 tick 에서 또 매치 → 재발사" 루프가 성립한다.

- `awaiting_approval` 플래그는 콜백이 clear 하지만, monitor 가 다음 tick 에서 다시 `is_approval()=True` 를 보고 set 하므로 억제 효과가 없다.

### 가설 B (보조) — `summarize_approval` 의 툴명 라벨 오탐

**근거:** `bridge/parser.py:286-291` 에서 `"Do you want to"` anchor 의 위 `APPROVAL_SCAN_LINES=30` 줄을 훑어 `^(Bash|Edit|Write|Read|MultiEdit|...)` 매치. 사용자 echo 위쪽 스크롤백에 남아있던 이전 세션의 `Read(...)` 라인이 걸려 라벨이 `Read` 로 들어갔고, 15:14 부터는 그 라인마저 밀려나 fallback `"승인"` 이 라벨됨.

본질적 원인은 아님 — 가설 A 가 해결되면 이 라벨 자체가 뜰 일이 없다. 다만 "왜 라벨이 Read 와 승인 을 오갔는가" 를 설명.

## 재현 방법

1. 빈 tmux 세션에서 봇 기동 (`/start`).
2. 텔레그램에서 다음을 한 메시지로 전송:
   ```
   parser.py 에 이런 승인창이 떴어:

    Do you want to proceed?
    ❯ 1. Yes
      2. Yes, allow all edits during this session (shift+tab)
      3. No

    Esc to cancel · Tab to amend
   ```
3. 봇이 `pre-approval` + `✅ 승인 · <툴명>` 을 발사하는지 확인.
4. Yes 를 누른 뒤 다음 monitor tick 에서 같은 승인이 다시 발사되는지 확인.

단위테스트로 재현하려면 `tests/test_bug_20260417.py` 에 아래 픽스처를 넣고 `parser.is_approval` 가 **False 를 돌려주는지** 확인 (현재는 True):

```python
ECHOED_PANE = """⏺ 이전 질문에 대한 답:
  (어쩌구저쩌구)
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
────────────────────────────────────────────────────────────────────────────────
  ❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)              ◉ xhigh · /effort
"""
```

대조군(라이브 승인 — True 여야 함)은 `tests/test_bug_20260415.py:63` 의 `APPROVAL_PANE` 를 그대로 사용 가능.

## 제안 수정안

실제 승인 박스는 **입력 박스 자리를 차지한다**. 그래서 `❯ 1. Yes` 아래에 "입력 박스 divider (전폭 `─` 라인)" 가 **나타나지 않는다**. 반면 scrollback 에 echo 된 경우에는 Claude Code 가 여전히 자기 입력 박스를 렌더하므로 Yes 줄 아래에 `────` 구분선이 반드시 존재한다. 이 구조적 차이를 가드로 추가한다.

### bridge/parser.py 변경

```diff
 _APPROVAL_CHOICE_RE = re.compile(r"^\s*❯?\s*1\.\s+Yes\b", re.MULTILINE)
+# 입력 박스 divider — Claude Code 가 항상 ❯ 입력 프롬프트를 감싸는 전폭 ─ 라인.
+_INPUT_DIVIDER_RE = re.compile(r"^\s*─{20,}\s*$")


 def is_approval(text: str) -> bool:
-    # tail 영역 + 번호 선택지 공존 요구 — 대화 본문에 승인 관련 문구가 섞여도
-    # 실제 승인 박스가 아니면 False. (bridge 가 pane 에 쓰는 자기 출력까지
-    # 긁어서 오탐하는 것을 막음)
-    tail = "\n".join(text.splitlines()[-60:])
-    if not APPROVAL_RE.search(tail):
-        return False
-    return bool(_APPROVAL_CHOICE_RE.search(tail))
+    # 라이브 승인 박스의 구조적 특징:
+    #   1) tail 에 "Do you want to …" 프롬프트
+    #   2) 그 아래 ~6줄 내에 "❯ 1. Yes" 선택지
+    #   3) "❯ 1. Yes" **아래에 입력 박스 divider(전폭 ─) 가 없다** —
+    #      승인 박스가 입력 박스 자리를 대체하기 때문.
+    # 사용자가 텔레그램에서 예시 approval 박스 텍스트를 붙여넣으면 (1),(2) 는 모두
+    # 매치되지만, Claude Code 는 자기 입력 박스를 계속 렌더하므로 Yes 아래에
+    # `────────────────────────────────────────` 라인이 반드시 다시 등장한다 → (3) 으로 걸러진다.
+    tail_lines = text.splitlines()[-60:]
+    yes_idx: int | None = None
+    for i in range(len(tail_lines) - 1, -1, -1):
+        if _APPROVAL_CHOICE_RE.search(tail_lines[i]):
+            yes_idx = i
+            break
+    if yes_idx is None:
+        return False
+    # Yes 줄 위 ~8줄 내에 "Do you want to …" 가 있어야
+    window_top = max(0, yes_idx - 8)
+    if not APPROVAL_RE.search("\n".join(tail_lines[window_top : yes_idx + 1])):
+        return False
+    # Yes 줄 아래에 입력 박스 divider 가 있으면 라이브 승인이 아님 (scrollback echo)
+    for ln in tail_lines[yes_idx + 1 :]:
+        if _INPUT_DIVIDER_RE.match(ln):
+            return False
+    return True
```

### 추가 방어(권장, 선택) — receiver 콜백에서 Enter 오발사 방지

`bridge/receiver.py:164` 의 happy-path 가 여전히 `is_approval` True 를 믿고 `Enter` 를 보낸다. 가설 A 의 주 수정만으로 대부분 막히지만, **pane hash 스냅샷 가드**를 함께 얹으면 더 안전하다:

```diff
     if data == "approve_yes":
+        # 승인을 트리거했을 때의 pane snapshot 과 비교해 내용이 바뀌지 않았으면
+        # (= 승인 박스가 실존하지 않았거나 echo 오탐이었을 가능성) 재요청 경로로 폴백.
         pane = tmux.pane_output()
         clean = parser.strip_ansi(pane).strip()
         context = bridge.last_approval_context or "승인"

         if parser.is_approval(clean):
             # 정상 경로: 프롬프트가 여전히 있음
             bridge.awaiting_approval = False
             _log("USER-ACK", "approved")
             tmux.send_key("Enter")
```

(당장 필수는 아니지만, `is_approval` 강화만으로도 본 건은 해결된다.)

### 리스크 / 사이드이펙트

- **true-positive 손실 위험:** 정말 드물지만 Claude Code 가 승인 박스 **아래에도** 전폭 `─` 라인을 넣는 케이스가 있으면 is_approval 가 False 를 낼 수 있다. 현행 `tests/test_bug_20260415.py` 의 `APPROVAL_PANE` 픽스처와 `tests/test_approval_reconnect.py:401` 의 `"Bash(ls -la)\n─────────\nDo you want to proceed?\n❯ 1. Yes  2. No"` 에서는 Yes 줄 **위쪽**에만 divider 가 있으므로 수정 후에도 True 가 유지된다(검증 완료).
- `_INPUT_DIVIDER_RE` 가 `─{20,}` 로 충분히 보수적이라 ⏺ 블록 내부의 짧은 대시 장식(예: `──` 3~4자)에 오탐하지 않는다.

### 테스트 추가/변경 제안

- [ ] `tests/test_bug_20260417.py` 새 파일 추가:
    - `test_is_approval_rejects_scrollback_echo()` — 위 "재현 방법" 의 `ECHOED_PANE` 로 `parser.is_approval(...) is False` 확인.
    - `test_is_approval_accepts_live_box()` — `APPROVAL_PANE` (test_bug_20260415 의 픽스처) 로 True 확인 (regression guard).
    - `test_is_approval_accepts_compact_box_inline()` — `test_approval_reconnect.py:401` 유사 픽스처로 True 확인.
- [ ] `tests/test_approval_reconnect.py` 의 기존 mock `pane_output="Do you want to proceed?"` 같은 **빈 선택지 케이스**는 새 조건에서 False 가 된다 → 이미 해당 테스트들은 `parser.is_approval` 자체를 `patch.object(... return_value=True)` 로 강제 우회하므로 (121, 147줄) 영향 없음. 다만 강제 우회가 빠진 테스트가 있다면 픽스처에 `\n❯ 1. Yes\n  2. No` 를 추가해야 함.
- [ ] `tests/test_scenarios.py` 등에서 `is_approval` 을 직접 부르는 곳이 있으면 같은 방식으로 픽스처 보강.

## 추가 수집이 필요한 정보

- [ ] `dump/` 디렉토리 (본 증거 번들에는 없음). pane_tick / events.jsonl 이 있으면 "is_approval=True 가 몇 tick 연속으로 나왔는가, 그 사이 pane hash 가 정말 고정돼 있었는가" 를 확정할 수 있다.
- [ ] 14:17~14:19 `first boot (403 chars)` 가 3회 반복된 배경 — 본 버그와는 별개의 `edit_message_text` BadRequest 재시도 이슈 (logs/2026-04-17.log 206~246줄). 별건 리포트로 분리할지 판단 필요.

---

**요약:** `bridge/parser.py:is_approval` 가 "tail 60줄 안에 `Do you want to …` + `❯ 1. Yes` 공존" 만으로 판정해서, 사용자가 텔레그램에서 **approval 박스 스크린샷을 텍스트로 붙여넣으면** pane scrollback echo 자체를 라이브 승인창으로 오인한다. 라이브 승인은 `❯ 1. Yes` 아래에 입력 박스 divider 가 없다는 구조적 특징이 있으니 해당 가드를 추가한다.
