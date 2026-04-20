# 수정 상세 — Fix 1 / 2 / 3

## Fix 1 — `bridge/parser.py` `_response_region` 가드

### 의도

승인 박스 경계 truncation 을 **라이브 모달일 때만** 수행. ESC 로 닫힌 모달의 scrollback 잔존 텍스트로 경계가 오탐되는 것을 차단.

### 변경

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
+    # 1) 승인 박스 경계 감지 — **라이브 모달일 때만**.
+    #    ESC 로 닫힌 모달 텍스트가 scrollback 에 남아있어도 응답 영역을
+    #    잘라먹지 않도록 is_approval(text) 로 게이트한다. 추가로 매칭
+    #    위치가 tail 60줄 안에 있어야 유효한 경계로 본다 (is_approval 이
+    #    혹시 false-negative 일 때의 방어선).
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

### 전방 참조 주의

`is_approval` 은 `parser.py:98-120`, `_response_region` 은 `parser.py:214-257`. 호출 순서가 **위→아래** 로 자연 만족(전방 참조 문제 없음).

### 유지되는 경계

- 경계 B (입력창 divider, tail 15): 건드리지 않음
- 경계 C (Welcome/Claude Code v 배너): 건드리지 않음

### 리뷰 반영 — 중첩 모달 케이스

tail 60 안에 "Do you want to proceed" 가 **2개 이상** 있을 수 있다 (ESC 로 닫힌 구 모달 + 새로 뜬 라이브 모달). 현재 구현은 `range(len-1, tail_start-1, -1)` 로 **역방향 첫 매칭**을 잡으므로 실제 라이브(가장 최근) 모달이 선택됨 → 안전. 이 경로는 `test-plan.md` 의 stale+active 테스트로 커버.

## Fix 2 — `bridge/receiver.py` `cmd_esc` 의 상태 복원

### 의도

사용자가 명시적으로 `/esc` 로 승인을 취소했으면 봇 쪽 `awaiting_approval` 도 즉시 해제. monitor 가 sleep 분기에서 깨어나 ESC 이후 응답을 볼 수 있게 함.

### 변경

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
     except Exception:
         pass
```

### `queue.reset()` 을 **하지 않는** 이유

- ESC 는 "현재 turn 을 취소" 가 아니라 "승인창을 닫음". 같은 turn 내에서 Claude 가 이어서 출력할 수 있으므로 queue 좌표는 유지되어야 한다.
- 사용자가 ESC 후 새 메시지를 보내면 `on_message` 가 `bridge.queue.reset()` 을 수행 (`receiver.py:462, 479`). 이중 reset 불필요.
- 시니어 리뷰의 의문 제기: "`on_message` 직전까지 queue 상태 불일치 없는가?" — monitor 는 `awaiting_approval=False` 로 돌아간 뒤 정상 take_new 를 수행, 기존 `idx/last_fp` 는 ESC 이전 상태 그대로 유효.

### 리뷰 반영 — 늦은 승인 버튼과의 race

시나리오: `/esc` → `awaiting_approval=False` 직후 사용자가 과거 승인 메시지의 Yes 버튼을 누름 → `approve_yes` 분기가 `is_approval(clean)=False` 로 판정 → late-approved 경로로 Claude 에 "사용자가 … 승인했습니다" 텍스트 주입.

- 의도치 않은 텍스트가 주입될 수 있으나, **이는 기존 정책** (ESC 없이도 프롬프트 만료 시 발생). 이번 Fix 로 새로 생기는 리스크 아님.
- 근본 해결은 ESC 시 해당 승인 메시지를 `edit_message_text` 로 `❌ 취소됨` 처리 — **본 작업 범위 밖**. `docs/reviews/20260418-esc-flush-recovery/` 후속 권고로 기록.

## Fix 3 — `bridge/core.py` 관측성 보강

### 의도

응답 구간에서 `_flush_completed` 가 "slip 발생 + 앵커 미발견" 으로 빈 리스트를 받아 침묵하는 경로를 포그라운드 로그에 단독 태그로 노출. 다음 회귀 때 "왜 응답이 안 나가지?" 의 1분 내 판정을 가능하게 함.

### 변경 (조건 강화)

```diff
--- a/bridge/core.py
+++ b/bridge/core.py
@@ async def _flush_completed(...):
         completed = self._completed_blocks(clean, include_last=include_last)
         slip = self.queue.detect_slip(completed)
         ...
         new_blocks = self.queue.take_new(completed)
+        if (
+            not new_blocks
+            and log_tag == "response"
+            and slip is not None
+        ):
+            # 응답 구간 + slip 발생인데 앵커로도 복원 실패 → stale boundary
+            # 오탐 or scrollback eviction 조합. 연속 발생 시 alert 필요.
+            _log(
+                "FLUSH-EMPTY",
+                f"{log_tag} completed={len(completed)} idx={self.queue.idx} slip={slip}",
+            )
         dump.event(
             "core", "flush_peek",
             ...
         )
```

### 원 리포트 대비 변경

- 원안: `not new_blocks and log_tag == "response"` — 정상 take_new=0 경로(이미 모든 블록 송출 완료, busy 초기 등)에서도 찍혀 스팸.
- 리뷰 반영: `slip is not None` AND 조건 추가. 진짜 의심 상황 (slip + 앵커 복원 실패) 에만 포그라운드 로그.

### dump 이벤트 보강은 **없음**

`dump.event("core", "flush_peek", ...)` 는 이미 `completed_count/new_count/slip/queue_idx` 를 다 기록 중 (`core.py:164-172`). CB_DUMP 가 켜진 세션에서는 포그라운드 로그 추가 없이도 동일 정보가 있음. Fix 3 은 CB_DUMP 가 꺼진 세션에서의 사각지대를 메우는 목적.

## 적용 순서

Fix 1 → Fix 2 → Fix 3. 각 Fix 적용 직후 `python -m pytest tests/ -q` 로 기존 회귀 확인 (테스트 추가는 [test-plan.md](test-plan.md)).

## 참고

- 사이드이펙트 표 + 대응: [side-effects.md](side-effects.md)
- 테스트 케이스: [test-plan.md](test-plan.md)
- 원 리포트: [`bugreport/20260418_094744/BUG_REPORT.md`](../../../bugreport/20260418_094744/BUG_REPORT.md)
