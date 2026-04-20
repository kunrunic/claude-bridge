# 버그 리포트 — 20260418_094744

## 현상

승인 모달 상태에서 텔레그램 `/esc` 로 취소하면, 이후 사용자 메시지는 정상으로
Claude 에 전달되지만 **Claude 의 응답은 단 한 건도 텔레그램으로 오지 않는다**.
tmux pane 에는 `⏺ 네, 대기하겠습니다.`, `⏺ 하이! 무엇을 도와드릴까요?` 등
응답이 찍혀 있고, monitor 가 `[AI-BUSY]` 진입 edge 를 재차 감지(= 이전 turn 이
완료됐음을 의미) 하는데도 `[AI→BOT] response` / `[BOT→USER] delivered` 로그가
02:19:52 이후 끝까지 0 건이다.

## 타임라인 재구성

| 시각 | 소스 | 이벤트 |
|------|------|--------|
| 02:20:52 | log | `[AI-APPROVAL] Read` — `awaiting_approval=True` |
| 02:21:04 | log | `[USER→BOT] /esc` → `[USER→AI] ESC pressed` |
| 02:21:04 | tmux | 모달 닫힘. "Do you want to proceed?"/"❯ 1. Yes"/divider 는 scrollback 에 잔존 (tmux\_capture.txt 42~56 행) |
| 02:21:04~20 | tmux | Claude 가 `⏺ 네, 대기하겠습니다.` 등 응답 생성 (147~164 행) |
| 02:21:20 | log | `[USER→BOT] 대써 봇한테…` → `[BOT→AI] forwarded`. monitor 는 `awaiting_approval=True` 분기에서 sleep 중 (core.py:462-464) |
| 02:21:33 | log | `[USER-ACK] late denied (prompt expired) — context=Read` → `awaiting_approval=False` |
| 02:22:15 | log | `[USER→BOT] 여기요?` → `[BOT→AI]` → `[AI-BUSY]` (edge 재발생) |
| 02:22:37 | log | `[USER→BOT] 답이 왜 없어요?` → `[BOT→AI]` → `[AI-BUSY]` (edge 재발생) |
| 08:12:41 | log | `[USER→BOT] 하이?` → `[AI-BUSY]` (edge 재발생) |
| (전 구간) | log | `[AI→BOT] response` / `[BOT→USER] delivered (response)` **0 건** |

`[AI-BUSY]` 의 edge 재발생은 매 메시지마다 monitor 가 "busy → idle → busy" 를
관측했다는 뜻 — 즉 Claude 는 매번 응답을 끝냈다. 그런데도 flush 가 0 건이다.

## 원인 가설

### 가설 A (주, 확신 높음) — `_response_region` 의 stale 모달 오탐

`bridge/parser.py:228-238`

```python
# 1) 승인 박스/입력창 경계 감지
prompt_idx = -1
for i in range(len(lines) - 1, -1, -1):     # ← tail 제한 없이 전체 pane 역방향 스캔
    line = lines[i].strip()
    if "Do you want to proceed" in line or line == "Do you want to":
        prompt_idx = i
        break
if prompt_idx > 0:
    for i in range(prompt_idx - 1, max(-1, prompt_idx - 60), -1):
        if is_divider(lines[i]):
            end = min(end, i)               # ← 응답 영역 끝을 모달 앞 divider 로 자름
            break
```

근거 체인:

1. Claude Code TUI 는 Enter(승인) 로 닫힌 모달은 툴 실행 결과로 그 자리가
   덮여써져 scrollback 에 텍스트가 남지 않지만, **Escape 로 취소한 모달은
   텍스트("Do you want to proceed?", "❯ 1. Yes", 입력창 divider 등) 가
   scrollback 에 그대로 남는다** (tmux\_capture.txt 42~56 행 = 취소된 모달 +
   147 행 이후의 정상 응답 공존).
2. 위 루프는 **tail 제한 없이** 전체 pane 을 역방향 스캔하므로 잔존 텍스트가
   매칭된다. `is_approval()` 이 동일 키워드를 **tail 60줄로 제한** (`parser.py:106`)
   해서 False 를 돌리는 것과 비대칭 (parser.py 내부 정책 불일치).
3. 그 결과 `end` 가 모달 앞 divider 로 고정되어, 이후 Claude 가 찍는 모든 신규
   `⏺` 블록이 `extract_response_blocks` 의 스캔 범위(`scan_start..end`) 밖으로
   밀려난다.
4. `_flush_completed` 는 "완료 블록 0" → "새 블록 0" 으로 판정하고 전송하지
   않는다. 이 때문에 `_log("AI→BOT", ...)` 조차 호출되지 않아 로그에도 흔적이
   남지 않는다.
5. scrollback 이 `TMUX_SCROLL_LINES=500` 에 의해 밀려날 때까지 이 상태는
   지속된다. 실제로 02:21 ~ 08:12 (6 시간, 사용자 메시지 4 건) 동안 모달
   텍스트가 scrollback 에서 빠지지 않아 증상이 계속됐다.

이 패턴은 같은 파일에서 이미 두 번 반복된 회귀의 연장선이다:
`bc6a015` (*is\_approval 이 scrollback echo 를 라이브 승인창으로 오인*),
`4c20696` (*fresh pane 의 초기 부팅 배너가 `_response_region` end 를 0 으로
만들던 버그*) — 모두 "pane scrollback 에 남은 잔존 마커" 계열.

### 가설 B (부, 보조) — `cmd_esc` 의 상태 미복원

`bridge/receiver.py:77-90` 의 `cmd_esc` 는 `tmux.send_key("Escape")` 만 보내고
`bridge.awaiting_approval` 은 그대로 둔다.

* monitor (`core.py:462-464`) 는 `awaiting_approval=True` 인 동안 모든 flush /
  busy 처리를 건너뛰고 sleep 한다. ESC 직후 Claude 가 생성한 "네, 대기하겠습니다."
  는 monitor 눈에 들어오지도 못한다.
* 텔레그램의 "거부" 버튼을 누를 때(late\_denied 경로) 까지 이 상태가 유지된다.
* 02:21:33 이후 `awaiting_approval=False` 가 되지만 그 때부터는 **가설 A** 가
  고장을 이어받아 증상이 영구화된다.

B 는 A 의 필요조건은 아니며 A 단독으로도 증상 재현 가능. 다만 B 가 있으면
"ESC 직후 몇 초 동안" 의 응답도 놓치고 불필요한 late-denied 입력("사용자가
'Read' 작업을 거부했습니다…") 이 Claude 에 주입되어 대화 흐름이 어지러워진다.

## 재현 방법

1. 텔레그램으로 Claude 에 승인이 필요한 툴 (Bash / Read / Edit 등) 호출 유도.
2. 모달이 뜨면 **승인/거부 버튼을 누르지 않고** `/esc` 전송.
3. Claude 에게 아무 메시지 ("하이?") 전송.
4. 기대: 응답이 텔레그램으로 전달됨.
   실제: `[AI-BUSY]` edge 만 반복, `[AI→BOT] response` 0 건. tmux 에는 응답 존재.

재현 판별: `grep -c 'AI→BOT response' logs/<date>.log` 가 `USER→BOT` 대비
극단적으로 작고, pane 에 ESC 로 닫힌 "Do you want to proceed?" 가 남아있으면
동일 원인.

## 제안 수정안

실제 파일은 건드리지 않는다. 아래 diff 는 repairer.sh 가 적용할 제안이다.

### Fix 1 — `bridge/parser.py` `_response_region` 가드 (주)

승인 경계 truncation 을 **라이브 모달** 일 때만 수행하도록 `is_approval(text)`
로 게이트. 추가로, 매칭된 "Do you want to proceed?" 가 **tail 60줄 안에** 있어야
유효하다는 조건도 AND 로 두어, `is_approval` 이 혹시 false-negative 나도
"pane 중간 깊숙이 있는 오래된 prompt" 는 걸러낸다 (senior dev 지적 반영).

```diff
--- a/bridge/parser.py
+++ b/bridge/parser.py
@@ def _response_region(text: str) -> tuple[list[str], int]:
     end = len(lines)

-    # 1) 승인 박스/입력창 경계 감지
-    prompt_idx = -1
-    for i in range(len(lines) - 1, -1, -1):
-        line = lines[i].strip()
-        if "Do you want to proceed" in line or line == "Do you want to":
-            prompt_idx = i
-            break
-    if prompt_idx > 0:
-        for i in range(prompt_idx - 1, max(-1, prompt_idx - 60), -1):
-            if is_divider(lines[i]):
-                end = min(end, i)
-                break
+    # 1) 승인 박스 경계 감지 — **라이브 모달** 일 때만.
+    #    ESC 로 닫힌 모달 텍스트가 scrollback 에 남아있어도 응답 영역을
+    #    잘라먹지 않도록 is_approval(text) 로 게이트한다. 추가로 매칭 위치가
+    #    tail 60줄 안에 있어야 유효한 경계로 본다 (is_approval 가 혹시
+    #    false-negative 일 때의 방어선).
+    #    (20260418_094744: /esc 후 응답이 끝까지 forwarding 되지 않던 회귀.
+    #     bc6a015 / 4c20696 에 이은 pane-scrollback 오탐 계열.)
+    if is_approval(text):
+        prompt_idx = -1
+        tail_start = max(0, len(lines) - 60)
+        for i in range(len(lines) - 1, tail_start - 1, -1):
+            line = lines[i].strip()
+            if "Do you want to proceed" in line or line == "Do you want to":
+                prompt_idx = i
+                break
+        if prompt_idx > 0:
+            for i in range(prompt_idx - 1, max(-1, prompt_idx - 60), -1):
+                if is_divider(lines[i]):
+                    end = min(end, i)
+                    break
```

(`is_approval` 이 `_response_region` 보다 앞에 정의되어 있어 전방 참조 문제
없음 — 현재 parser.py 기준 98 행 vs 214 행.)

### Fix 2 — `bridge/receiver.py` `cmd_esc` 의 상태 복원 (부)

사용자가 명시적으로 취소한 것이므로 봇 쪽 대기 상태도 즉시 내린다. `queue.reset()`
은 넣지 **않는다** — 다음 `on_message` 가 새 turn 진입 시 자동으로 reset 하고,
ESC 직후 idle 상태에서의 중복/race 위험이 없다 (senior dev 지적 반영).

```diff
--- a/bridge/receiver.py
+++ b/bridge/receiver.py
@@ async def cmd_esc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
     tmux.send_key("Escape")
     _log("USER→AI", "ESC pressed")
+    # /esc 는 사용자가 직접 승인 모달을 닫은 것 — 봇 쪽 대기 상태도 즉시 해제.
+    # 그대로 두면 monitor 가 awaiting_approval=True 분기에서 계속 sleep 하며
+    # Claude 의 post-ESC 응답을 보지 못한다. (20260418_094744)
+    if bridge.awaiting_approval:
+        bridge.awaiting_approval = False
+        _log("USER-ACK", "esc (local approval state cleared)")
     try:
         await update.message.set_reaction("⚡")
```

### Fix 3 (권장) — 관측성 보강

Fix 1/2 적용 후에도 동종 계열 버그가 재발했을 때 **1 분 내 원인 파악** 이
가능하도록, flush 판정 시의 블록 카운트를 구조화 로그에 남긴다. 이미 있는
`dump.event("core", "flush_peek", ...)` 에 더해 포그라운드 로그에도 0 건 지속을
단독 라인으로 찍는다.

```diff
--- a/bridge/core.py
+++ b/bridge/core.py
@@ async def _flush_completed(...):
         completed = self._completed_blocks(clean, include_last=include_last)
         slip = self.queue.detect_slip(completed)
         ...
         new_blocks = self.queue.take_new(completed)
+        if not new_blocks and log_tag == "response":
+            # 응답 구간인데 보낼 블록 0 — stale boundary 오탐 or queue idx
+            # 어긋남 의심. 연속 발생 시 alert 필요.
+            _log("FLUSH-EMPTY", f"{log_tag} completed={len(completed)} idx={self.queue.idx}")
         dump.event(
             "core", "flush_peek",
             ...
         )
```

### 리스크 / 사이드이펙트

* **Fix 1**: `is_approval` 이 라이브 모달을 놓치는 순간(매우 좁은 터미널,
  TUI 애니메이션 렌더 중간) 에도 경계 truncation 이 건너뛰어질 수 있다. 그러나
  그 상태에서 `_flush_completed` 는 상위 `monitor` 루프의 `if parser.is_approval(clean):`
  분기에서도 같이 False 가 되어 `pre-approval` flush 가 스킵된다 — 기존 대비
  퇴보 없음. 오히려 안정된 pane 이 되었을 때 자연스럽게 flush 된다.
* **Fix 2**: `cmd_esc` 이후 텔레그램 승인 메시지의 승인/거부 버튼은 여전히
  존재. 사용자가 지연해서 누르더라도 receiver 의 `approve_yes/no` 가 `is_approval(clean)`
  False / `is_alive` True 경로로 가서 "late approved/denied" 로 추가 Claude
  입력을 보내게 된다. 이는 실제 사용자가 보낸 원래 메시지와 시간적으로 겹쳐
  약간 혼란스러울 수 있음. 별도 후속으로 "ESC 후에는 해당 승인 메시지의 버튼을
  비활성화하고 `❌ 취소됨` 으로 edit\_message" 를 권장 (본 PR 범위 밖).
* **Fix 3**: 로그 라인 1 개 추가 외 영향 없음.

### 테스트 추가/변경 제안

- [ ] `tests/test_parser.py` — `_response_region` / `extract_response_blocks`
      회귀 테스트:
  ```python
  STALE_APPROVAL_PANE = """\
  ⏺ Bash(ls)

  ────────────────────────────────────────────────────────────────
   Bash command
     ls
   Do you want to proceed?
   ❯ 1. Yes
     2. No
   Esc to cancel · Tab to amend · ctrl+e to explain
  ────────────────────────────────────────────────────────────────
  ❯ 여기요?

  ⏺ 네, 여기 있습니다.

  ────────────────────────────────────────────────────────────────
  ❯
  ────────────────────────────────────────────────────────────────
  """
  def test_response_region_ignores_stale_approval_after_esc():
      blocks = parser.extract_response_blocks(STALE_APPROVAL_PANE)
      assert any("네, 여기 있습니다." in b for b in blocks)
  ```
- [ ] `tests/test_receiver.py` — `cmd_esc` 가 `bridge.awaiting_approval` 을
      False 로 복원하는지 검증. `bridge.queue.idx` 는 **건드리지 않음** (Fix 2
      정책 확인).
- [ ] integration: mock pane 을 "ESC 직후 + 신규 ⏺" 로 세팅 후 monitor 1 tick
      돌려 `_send_output` 이 호출되는지 확인.

### 후속 (본 PR 범위 밖, Architect 권고)

1. **상태 진입/복원 경로 통합** — `bridge.awaiting_approval` 의 set/clear 가
   `on_callback` (approve\_yes/no), `on_message`, `cmd_esc`, `core.monitor`
   네 곳에 분산되어 있다. `Bridge.set_awaiting(summary, ...) / clear_awaiting(reason)`
   헬퍼로 일원화하면 "ESC 는 approval 상태 해제" 같은 새 규칙 추가 시 누락이
   사라진다.
2. **특수 박스 경계 감지 통합** — 현재 `is_approval` / `is_trust_prompt` /
   `is_resume_picker` 는 각자 라이브 판정 로직을 갖는데, `_response_region` 은
   승인만 개별 처리. `boundary_from_live_box(text) -> int | None` 같은
   단일 함수로 묶으면 동종 오탐이 재발해도 한 곳만 고치면 된다.
3. **ESC 후 텔레그램 승인 메시지 edit** — Fix 2 의 리스크에서 언급한 후속.

## 추가 수집이 필요한 정보

- [ ] `dump/` 가 이번 세션에는 없다. 재현 시 `bin/dump_on.sh` 등으로 켜두면
      `source=core, kind=flush_peek` 의 `completed_count=0, new_count=0` 이
      연속으로 찍히는 것으로 가설 A 를 정량 확증할 수 있다 (FLUSH-EMPTY 포그라운드
      로그는 Fix 3 적용 후 제공).
- [ ] Claude Code 버전별로 ESC 시 scrollback 잔존 여부가 다를 수 있음. 현재
      세션은 `v2.1.112 / Opus 4.7`. 재현 테스트는 동일 버전 기준.
- [ ] `--dangerously-skip-permissions` 모드 운용 시에는 승인 자체가 없어 본
      버그의 조건이 성립하지 않음 (QA 회귀 범위 표 참조). 권한 확인 모드에서만
      우선 확인.
