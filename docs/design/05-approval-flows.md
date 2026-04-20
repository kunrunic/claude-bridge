# 05. 승인/모드 플로우

Claude Code 는 기본적으로 도구 호출마다 사용자의 승인을 요구한다. 봇은 이 승인 UI 를 파싱해 Telegram 으로 중계하거나, `--dangerously-skip-permissions` 로 우회한다. 두 모드 모두 **같은 파서**를 쓰지만 **제어 흐름은 완전히 다르다**.

## 모드 선택

| 모드 | 설정 | Claude Code 인자 |
|------|------|-----------------|
| 지속 승인 (기본) | `bridge.skip_permissions = False` | 없음 |
| Bypass | `bridge.skip_permissions = True` | `--dangerously-skip-permissions` (`core.py:67-68`) |

토글: `/start` 메뉴의 `[권한 …]` 버튼 → `receiver.py:368-388` `toggle_perm`. `bridge.running` 이면 `bridge.restart()` 로 세션 재기동.

## 지속 승인 모드

### 승인 박스 발생 → Telegram 전달

`core.py:438-460`:

```
is_approval(clean) & !awaiting_approval
 ├─ pre-approval flush (include_last=True, "pre-approval")
 │   → 승인창 위에 쌓여있던 ⏺ 응답을 먼저 내보냄
 ├─ last_approval_summary = summarize_approval(clean)     # 도구명 (예: "Bash")
 ├─ last_approval_full = clean[-800:]
 ├─ status_msg_id 삭제 (busy status → 승인으로 전환)
 ├─ awaiting_approval = True
 └─ sender._send_approval(app, chat_id, clean)
     └─ _approval_box 로 잘라낸 스니펫 + [Yes/No] 버튼
```

이후 `awaiting_approval=True` 인 동안 monitor 는 모든 flush/busy 처리를 건너뛴다 (`core.py:462-464`).

### `approve_yes` 분기 (`receiver.py:163-219`)

```
pane = capture-pane → is_approval(clean)?
 ├─ YES: 정상 경로
 │   awaiting_approval = False
 │   tmux.send_key("Enter")
 │   edit_message_text "✅ 승인 · {summary}"
 │
 ├─ NO + is_alive: 프롬프트 만료 (late approved)
 │   awaiting_approval = False
 │   queue.reset()                 # 새 turn 간주
 │   tmux.send_input(f"사용자가 '{context}' 작업을 승인했습니다. 이어서 진행해주세요.")
 │
 ├─ NO + !is_alive + current_session_id: 세션 죽음
 │   awaiting_approval = False
 │   bridge.start(session_id)      # tmux 새로 띄움
 │   bridge.monitor 재-spawn
 │   sleep(3) → queue.reset() → send_input("사용자가 '...' 승인했습니다 …")
 │
 └─ else: "이미 처리됨" alert
```

### `approve_no` 분기 (`receiver.py:221-269`)

```
pane = capture-pane → is_approval(clean)?
 ├─ YES: 정상 경로
 │   send_key("Down") → sleep 0.15 → send_key("Enter")    # Yes→No 이동
 │
 ├─ NO + is_alive: late denied
 │   queue.reset()
 │   send_input("사용자가 '{context}' 작업을 거부했습니다. 해당 작업을 취소하고 대기해주세요.")
 │
 ├─ NO + !is_alive + session_id: "이 세션 재시작?" 2-버튼
 │   └─ resume_after_no:<id> → bridge.start + monitor 재-spawn
 │   └─ resume_after_no:cancel → 취소 텍스트
 │
 └─ else: "이미 처리됨" alert
```

### `/esc` 분기 (`receiver.py:77-91`)

```
cmd_esc
 ├─ has-session? 없으면 "세션이 없습니다."
 ├─ tmux.send_key("Escape")
 ├─ log "USER→AI ESC pressed"
 └─ message.set_reaction("⚡")
```

**⚠️ `awaiting_approval` 을 clear 하지 않는다.** → monitor 는 `awaiting_approval=True` 분기에서 계속 sleep → Claude 가 ESC 후 생성한 응답이 전달되지 않음. 이번 작업 Fix 2 의 타깃.

또한 **텔레그램 승인 메시지(Yes/No 버튼)** 는 그대로 남아있다. 사용자가 나중에 버튼을 누르면 `approve_yes/no` 의 late-approved/denied 경로로 빠져 의도치 않은 입력이 Claude 로 주입될 수 있다.

## Bypass 모드 (`skip_permissions=True`)

### 무엇이 빠지나

- Claude Code 가 승인 프롬프트를 아예 띄우지 않음 → `is_approval(clean)` 항상 False
- `core.py:438-460` 의 승인 분기가 발동하지 않음
- `awaiting_approval` 이 True 가 되는 경로가 없음 → monitor 의 sleep 1s 분기(`core.py:462-464`) 도 실질 발동 X

### 무엇이 달라지나

한 번의 사용자 입력이 긴 툴 체인을 트리거해 **pane 이 계속 busy 유지**. 응답이 모여 한꺼번에 오는 게 아니라 중간중간 `⏺` 블록이 찍힌다. 이를 실시간으로 전달하는 분기가 `busy-stream`:

```
busy 지속 중:
  if now - busy_last_stream_at >= BUSY_STREAM_SEC (10s):
      busy_last_stream_at = now
      _flush_completed(pending, include_last=False, "busy-stream")
          # 마지막 블록은 성장 중이라 제외
          # queue.idx 로 이미 보낸 블록은 자동 제외
```

`BUSY_STREAM_SEC=10` 이 사용자 체감 "진행 상황 알림" 주기를 결정한다.

### ESC 는?

Bypass 모드에서도 `/esc` 는 동일하게 `tmux.send_key("Escape")` — Claude Code 의 현재 작업 중단으로 동작. `awaiting_approval` 은 원래 False 라 clear 누락 이슈가 **발생하지 않는다**. 이 이슈는 지속 승인 모드 전용.

## 공통 자동 승인 — Trust / Resume picker

두 프롬프트는 대화형 선택을 요구하지만 봇이 Telegram 으로 중계하지 않는다. 기본값을 자동 Enter:

| 프롬프트 | 감지 | 자동 응답 | 알림 |
|---------|------|---------|------|
| Quick safety check (폴더 신뢰) | `is_trust_prompt` | `send_key("Enter")` | "폴더 신뢰 프롬프트 자동 승인" |
| Resume from summary/full | `is_resume_picker` | `send_key("Enter")` (기본값 summary) | "Resume 피커 자동 승인 (summary)" |

플래그(`trust_ack_pending`, `resume_picker_ack_pending`)로 중복 Enter 방지 (`core.py:342-366`).

## 컨텍스트 한도 자동 대응

`core.py:414-436`. Claude Code 가 `Context limit reached` 를 띄우면 사용자 입력을 거부하므로, 봇이 `/compact` 를 대신 dispatch:

```
has_context_limit(clean) & !compact_error_halted
 ├─ auto_compacting = False → True
 ├─ send_message "⚠️ Context limit 도달 — 자동 압축 실행 중 …"
 └─ tmux.send_input("/compact")
```

실패 시 (`has_compaction_error`) → `compact_error_halted=True` + `/model` 전환 안내 (`core.py:393-403`).

복구 신호: limit 과 error 모두 사라지면 halted 해제 (`core.py:406-410`).

## 모델 전환 (`/model`)

`receiver.py:93-110` 와 `sender.py:102-150`. pane 에 피커가 이미 열려있으면 그대로 포워딩, 아니면 `tmux.send_input("/model")` → 0.8s sleep → 피커 캡쳐.

버튼 선택 시 (`receiver.py:313-352`) 현재 `❯` 위치를 읽어 Up/Down 키로 이동 후 Enter.

## Known Fragility

1. **`cmd_esc` 의 상태 미복원** — 지속 승인 모드 전용. `awaiting_approval` 이 True 로 남아 monitor 가 응답을 못 봄. (Fix 2 대상)
2. **ESC 후 승인 메시지 잔존** — 텔레그램의 Yes/No 버튼이 계속 활성. 사용자가 뒤늦게 누르면 의도와 다른 텍스트가 Claude 로 주입됨.
3. **Resume picker 기본값 강제** — `summary` 선택을 자동화. 사용자가 full 을 원하면 분기가 없음.
4. **Trust 프롬프트 자동 Enter** — 신뢰하지 않는 폴더를 무조건 허용. `bridge.current_session_id` 를 신뢰하는 전제.
5. **`auto_compacting` / `compact_error_halted` 조합** — 세 플래그가 맞물리며 7가지 상태를 만든다. 전이 검증은 현재 `tests/test_compaction_watchdog.py` 에 일부만 있음.

## 참고

- 상태 플래그 전수: [04-state-machine.md](04-state-machine.md)
- 파이프라인 내 flush 종류: [02-streaming-pipeline.md](02-streaming-pipeline.md)
