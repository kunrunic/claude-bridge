# 02. 스트리밍 파이프라인

Claude 응답을 Telegram 으로 전달하는 전체 데이터 흐름. monitor 루프가 1초 주기로 pane 을 읽고 상태에 따라 5가지 flush 시점 중 하나를 고른다.

## 1-tick 데이터 흐름

```
           ┌──────────────────────────────────────────────────────┐
           │  monitor tick (bridge/core.py:312-665)               │
           └──────────────────────────────────────────────────────┘
                    │
                    ▼
 ① has-session / is_alive  ── 죽었으면 1회만 알리고 exit (core.py:314-336)
                    │
                    ▼
 ② pane_output_async   → capture-pane -S -500  (tmux.py:64-69)
                    │
                    ▼
 ③ strip_ansi          → ANSI 제거한 "clean" 문자열 (parser.py:58-59)
                    │
                    ▼
 ④ 특수 상태 분기 (순서가 곧 우선순위)
    ├─ is_trust_prompt     → auto-Enter (core.py:342-352)
    ├─ is_resume_picker    → auto-Enter (core.py:355-366)
    ├─ LIMIT_RE            → 사용자 안내 + sleep 30s (core.py:369-389)
    ├─ compaction error    → /model 전환 안내, halt (core.py:393-410)
    ├─ has_context_limit   → auto /compact dispatch (core.py:414-436)
    ├─ is_approval + !awaiting → pre-approval flush + 승인 박스 전송 (core.py:438-460)
    └─ awaiting_approval   → sleep 1s (core.py:462-464)
                    │
                    ▼
 ⑤ busy edge (is_busy + !was_busy)
    └─ pre-busy flush → 상태 메시지 생성 (core.py:466-497)
                    │
                    ▼
 ⑥ busy 지속
    ├─ compaction 감지 (core.py:502-508)
    ├─ pane-hash 변화 추적 → stuck 판정 (core.py:510-516)
    ├─ timeout/stuck watchdog (core.py:518-535)
    ├─ BUSY_STREAM_SEC 주기 busy-stream flush (core.py:540-549)
    └─ 상태 메시지 edit (core.py:551-564)
                    │
                    ▼
 ⑦ busy 종료
    ├─ 상태 메시지 delete (core.py:570-576)
    ├─ post-compact 복구 경로 (core.py:579-612)
    └─ settle 대기 → response flush (core.py:614-643)
                    │
                    ▼
 ⑧ asyncio.sleep(1)  (core.py:665)
```

## flush 5가지 — `_flush_completed` 호출 시점 표

`bridge/core.py:138-195` 의 `_flush_completed` 는 다음 5개 `log_tag` 로 호출된다.

| log_tag | 호출 위치 | `include_last` | 의도 |
|---------|----------|----------------|------|
| `pre-approval` | core.py:440-443 | `True` | 승인창이 뜨기 직전까지 생성된 ⏺ 블록을 먼저 내보내기 |
| `pre-busy` | core.py:469-473 | `True` | busy 진입 직전, 이전 turn 잔여 응답 마지막 밀어내기 |
| `busy-stream` | core.py:544-549 | `False` | bypass 모드 등 장시간 busy 중 완료된 블록 주기 송출 (마지막 블록은 성장 중이라 제외) |
| `post-compact` | core.py:589-592 | `True` | compaction 을 거쳐 빠져나온 뒤 생존한 ⏺ 블록 복구 |
| `response` | core.py:624-627 | `True` | settle(2 tick 안정) 후 정상 응답 송출 |

`watchdog` 태그는 `_busy_watchdog` 에서 별도 분기로 호출 (`core.py:248-251`, `include_last=True`).

## `_flush_completed` 내부

```
_flush_completed(clean, include_last, log_tag)
    │
    ├─ 0. awaiting_approval 이면 즉시 0 리턴     (core.py:150-151)
    ├─ 1. _completed_blocks(clean, include_last)  (core.py:152)
    │      └─ parser.extract_response_blocks(clean)
    │         · _response_region 으로 end 결정
    │         · 사용자 입력 ❯ 이후부터 ⏺ 블록 수집
    │      └─ _is_block_active 로 Running/Waiting 블록 제외
    │      └─ include_last=False 면 마지막 블록도 drop
    ├─ 2. queue.detect_slip(completed)             (core.py:153-162)
    │      └─ idx_past_cc / anchor_mismatch → 로그 + dump 이벤트
    ├─ 3. queue.take_new(completed)                (core.py:163)
    │      ├─ slip 없음: completed[idx:]
    │      ├─ slip + 앵커 보유: 앵커 위치+1 부터
    │      └─ slip + 앵커 유실: []
    ├─ 4. dump.event "flush_peek"                  (core.py:164-172)
    ├─ 5. for blk in new_blocks:
    │      · AI→BOT 로그 + dump "flush_block"
    │      · sender._send_output                   (core.py:182-189)
    │      · BOT→USER 로그, sent += 1
    │      · 실패 시 break (부분 성공 보존)
    └─ 6. 전송 성공 분: queue.advance_past + mark_sent + boot_notified=True
```

## 블록 추출 — `parser.extract_response_blocks`

`parser.py:283-321`. 응답 "영역" 안에서 ⏺ 블록만 뽑는다.

1. `_response_region(text)` 으로 `lines` + `end`(exclusive) 결정. end 는 다음 중 **가장 먼저 오는 것**으로 당겨진다:
   - "Do you want to proceed" 위쪽 가장 가까운 divider (tail 제한 없음, `parser.py:228-238`)
   - 마지막 15줄 안의 입력창 divider 시작점 (`parser.py:241-255`)
   - `Welcome back` / `Claude Code v` 배너 (위에 내용이 있을 때만; fresh 부팅 배너 오탐 방지, `parser.py:250-253`)
2. `end` 까지의 범위에서 **가장 마지막에 "제출된" `❯` 사용자 입력** 을 찾아 그 다음 줄부터 스캔 (`parser.py:296-306`). "제출됨"은 `❯` 뒤에 텍스트가 있는 줄.
3. 스캔 범위에서 `⏺` 로 시작하는 줄들의 인덱스를 모아, 각 인덱스 사이를 하나의 블록으로 묶음.

## 위치 기반 큐 — `StreamQueue`

`bridge/stream_queue.py`. 실제 데이터는 담지 않음. `idx`(다음 송출 시작 인덱스) + `last_fp`(마지막 송출 블록 앵커) 두 개만 보유.

- `take_new(completed)` — slip 없으면 `completed[idx:]`, 있으면 앵커 재탐색, 앵커도 없으면 빈 리스트 (`stream_queue.py:54-70`).
- `detect_slip` — (a) idx 가 completed 개수를 넘어가거나 (b) `completed[idx-1]` 의 앵커가 `last_fp` 와 다르면 eviction 발생 판정 (`stream_queue.py:39-52`).
- `advance_past(completed, last_sent)` — eviction 이후에도 좌표 재정렬 (`stream_queue.py:77-86`).
- `seed(N)` — 부팅 시 pane 에 이미 있던 N 개 블록을 "소비됨" 으로 표시 (`stream_queue.py:95-99`, `core.py:299-306`).
- `reset()` — 새 user turn(텍스트/이미지 수신) 시 호출 (`receiver.py:462`, `receiver.py:479`).

## 송출 — `sender._send_output`

`bridge/sender.py:44-62`. `_chunk_text` 로 3500자 이하로 분할, `<pre>` 래핑 HTML 전송. 실패 시 평문으로 재시도.

## 사용자 입력 방향 (Inbound)

`bridge/receiver.py:431-485`:
1. `on_message` 에서 권한 체크 → `session_alive` 체크
2. 이미지는 `tg_images/` 에 저장 후 `[텔레그램 이미지 첨부: <path>]\n<caption>` 로 `send_input`
3. 텍스트는 `send_input(caption)` 그대로
4. **직전에 `bridge.queue.reset()`** 호출 — 새 turn 시작 표식 (`receiver.py:462`, `receiver.py:479`)
5. reaction(✍/📷) 설정

## Known Fragility

- `_response_region` 의 "Do you want to proceed" 탐색은 **tail 제한 없음** (`parser.py:228-233`) 이라 ESC 로 닫힌 모달이 scrollback 에 남으면 경계 오탐 → flush 0건 지속 (`docs/plans/20260418-esc-flush-recovery/`).
- `take_new` 는 앵커 유실 시 보수적으로 빈 리스트를 반환 — 이 경로로 빠지면 **로그도 남기지 않음** (`core.py:164-172` 의 dump 는 찍히지만 포그라운드 로그는 `for blk in new_blocks:` 안쪽이라 스킵).
- `queue.reset()` 호출은 `on_message` 와 `on_callback` 의 late-approved/denied 경로에 분산되어 있다 (`receiver.py:183, 204, 243, 462, 479`). `cmd_esc` 에서는 호출하지 않음.

## 참고

- 경계 감지 함수 상세: [03-parser-and-boundaries.md](03-parser-and-boundaries.md)
- 상태 플래그: [04-state-machine.md](04-state-machine.md)
- flush 관측: [06-observability.md](06-observability.md)
