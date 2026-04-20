# 03. 파서와 경계 감지

`bridge/parser.py` 는 **pure 함수** 계층. 입력 문자열을 받아 판정/추출 결과만 리턴한다. tmux/telegram 모듈 import 금지 (`parser.py:1-5`).

## 상태 판정 함수 — tail 정책 표

같은 pane 문자열이라도 함수마다 "어디까지 보느냐" 가 다르다. 이 비대칭이 회귀 계열 버그의 단골 원인이다.

| 함수 | 스캔 범위 | 라이브 판정 로직 | 정의 |
|------|----------|----------------|------|
| `is_trust_prompt` | **전체** (regex search) | `TRUST_RE` 매치 여부만 | parser.py:62-63 |
| `is_resume_picker` | tail **60줄** | 1) `RESUME_PICKER_RE` 매치 2) 매치 아래에 입력 divider 없음 | parser.py:66-95 |
| `is_approval` | tail **60줄** | 1) `❯ 1. Yes` 2) 위 15줄 내 `Do you want to …` 3) Yes 아래에 입력 divider 없음 | parser.py:98-120 |
| `is_busy` | 마지막 divider 이하 (없으면 tail 20) | `BUSY_RE` (`esc to interrupt`) 매치 | parser.py:123-137 |
| `has_compaction` | tail **8줄** | `COMPACT_RE` 매치 | parser.py:140-143 |
| `has_context_limit` | tail **8줄** | `CONTEXT_LIMIT_RE` 매치 | parser.py:146-149 |
| `has_compaction_error` | tail **8줄** | `COMPACT_ERROR_RE` 매치 | parser.py:152-155 |
| `LIMIT_RE.search` | **전체** | 호출 지점에서 직접 search | core.py:369 |

`is_approval` / `is_resume_picker` 는 입력 divider 부재 체크까지 포함한 **3단 검증**(`parser.py:99-120`). 텔레그램에 echo 된 scrollback 텍스트는 Claude Code 가 자기 입력 박스 divider 를 같이 렌더하므로, 이 세 번째 단이 scrollback 오탐을 걸러낸다.

## 정규식 요약

`parser.py:12-53`:

| 상수 | 패턴 요지 | 용도 |
|------|----------|------|
| `APPROVAL_RE` | `Do you want to proceed` / `Allow X to` / `Proceed?` / `(Y/n)` / `(y/N)` | 승인창 키워드 |
| `_APPROVAL_CHOICE_RE` | `^\s*❯?\s*1\.\s+Yes` | 번호 선택 ` ❯ 1. Yes` |
| `_INPUT_DIVIDER_RE` | `^\s*─{10,}\s*$` | 입력 박스 divider (폭 10+) |
| `TRUST_RE` | `Quick safety check` / `trust this folder` / … | 폴더 신뢰 프롬프트 |
| `BUSY_RE` | `esc to interrupt` | busy 단서 |
| `LIMIT_RE` | `You've hit your limit` / `hit your (daily )?limit` | 한도 초과 |
| `COMPACT_RE` | `Compacting conversation` / `Crunched for N` | 압축 이벤트 |
| `CONTEXT_LIMIT_RE` | `Context limit reached` | 컨텍스트 한도 |
| `COMPACT_ERROR_RE` | `Error during compaction` | /compact API 에러 |
| `RESUME_PICKER_RE` | `Resume from summary.*Resume full session` | resume 피커 |
| `ANSI_RE` | ANSI escape 시퀀스 | `strip_ansi` |

## 응답 영역 경계 — `_response_region`

`parser.py:214-257`. 가장 중요한 함수 중 하나. 세 가지 경계가 `end` 를 당긴다 (각각 독립적으로 적용되며, **가장 작은 값**이 채택):

### 경계 A — 승인 박스 (tail 제한 **없음**)

```python
# parser.py:227-238
prompt_idx = -1
for i in range(len(lines) - 1, -1, -1):         # 전체 역방향
    line = lines[i].strip()
    if "Do you want to proceed" in line or line == "Do you want to":
        prompt_idx = i
        break
if prompt_idx > 0:
    for i in range(prompt_idx - 1, max(-1, prompt_idx - 60), -1):
        if is_divider(lines[i]):
            end = min(end, i)                   # 승인 박스 위 가장 가까운 divider
            break
```

### 경계 B — 입력창 divider (tail **15줄**)

```python
# parser.py:241-255
divider_positions = []
for i in range(len(lines) - 1, max(-1, len(lines) - 15), -1):
    if is_divider(lines[i]):
        divider_positions.append(i)
    if "Welcome back" in lines[i] or "Claude Code v" in lines[i]:
        has_content_above = any(lines[j].strip() for j in range(0, i))
        if has_content_above:
            end = min(end, i)
if divider_positions:
    end = min(end, min(divider_positions))
```

### 경계 C — Welcome/Claude Code v 배너

B 루프 내에서 함께 처리. **위에 내용이 있을 때만** 경계로 사용 — fresh 부팅 시 맨 위 배너를 경계로 삼아 `end=0` 이 되는 회귀(`4c20696`) 방지.

### 조합 규칙

세 경계 중 가장 이르게 등장하는 것이 실질 `end`. 어느 경계도 맞지 않으면 `end = len(lines)`.

## ⏺ 블록 추출 — `extract_response_blocks`

`parser.py:283-321`.

1. `_response_region` 으로 `lines`, `end` 취득.
2. `end` 이전 범위에서 **가장 마지막 "제출된" `❯` 줄** 을 찾아 `user_prompt_idx` 로 잡는다. "제출됨" = `❯` 뒤에 공백이 아닌 텍스트가 남는 경우 (`parser.py:296-305`). 빈 `❯` (대기 중인 입력 박스) 는 스킵.
3. `user_prompt_idx + 1 .. end` 범위에서 `⏺` 로 시작하는 줄의 인덱스를 순서대로 수집.
4. 인접 인덱스 사이를 하나의 블록으로 `\n` join.

### `_is_block_active`

`parser.py:329-332`. 블록 **꼬리 3줄**에 `Running…` / `Waiting…` / `ctrl+b...background` 가 있으면 아직 실행 중인 도구(=타이머가 계속 갱신되는 블록)으로 보고 완료 블록에서 제외 (`core.py:136`).

## 승인 박스 추출

- `summarize_approval(text)` — `APPROVAL_SCAN_LINES=30` 위쪽 범위에서 `Bash|Edit|Write|Read|MultiEdit|WebFetch|Grep|Glob|Task` 토큰 매치 → 도구명 리턴 (`parser.py:337-356`).
- `_approval_box(text)` — `Do you want to` 위쪽 가장 가까운 divider ~ +6 줄까지만 잘라 Telegram `_send_approval` 에 전달 (`parser.py:359-376`).

## `/model` 피커

- `_parse_model_options(lines)` — ` ❯?\s*N\.\s+(name)` 패턴으로 `{"num", "name", "current"}` 추출 (`parser.py:382-402`).
- `_find_picker_cursor(lines)` — 현재 `❯` 가리키는 번호 (`parser.py:405-411`).

## 위치 기반 큐 — `StreamQueue`

`bridge/stream_queue.py`. 파서가 블록 리스트를 내면 여기에서 dedup 판정.

- **상태**: `idx`(int, 다음 송출 시작), `last_fp`(str, 마지막 송출 블록의 앵커).
- **앵커** = 블록 첫 줄 앞 64자(`_fp` — `stream_queue.py:21-31`). 완료된 `⏺ Bash(...)` 꼴이라 충돌 실질 0.
- **slip 판정**: `idx > len(completed)` (eviction 으로 블록들이 밀려남) 또는 `completed[idx-1]` 앵커가 `last_fp` 와 불일치.
- **복원**: slip 발생 시 `last_fp` 가 있으면 `completed` 를 훑어 같은 앵커를 찾고, 그 다음부터 반환.
- **reset/seed/advance_past/mark_sent** — 각각 새 turn 진입 / 부팅 seed / 전송 후 좌표 갱신 / 앵커 업데이트.

## Known Fragility

1. **tail 정책 불일치**. `is_approval` 은 tail 60 제한, `_response_region` 의 경계 A 는 전체 역방향 → ESC 로 닫힌 모달이 scrollback 에 남은 경우 `is_approval=False` 인데 `_response_region` 만 경계로 당김 → flush 봉쇄. 회귀 `bc6a015`, `4c20696`, 이번 건.
2. **Welcome 배너 vs 부팅 배너**. B 경계가 "위에 내용 있을 때만" 조건이지만, 섞인 scrollback 에서는 여전히 오탐 여지.
3. **`user_prompt_idx` 탐색**은 `❯` 뒤 "공백 제거 후 비어있지 않음" 만 본다. 화면에 사용자 입력의 일부만 드러난 중간 상태에서는 일시적 오탐 가능.
4. **앵커 유실 경로 침묵**. `take_new` 가 `[]` 리턴하면 `_flush_completed` 의 로그는 아예 찍히지 않음 → 관측 불능 구간이 생김.

## 참고

- 파이프라인 내 사용 순서: [02-streaming-pipeline.md](02-streaming-pipeline.md)
- 상태 조합: [04-state-machine.md](04-state-machine.md)
