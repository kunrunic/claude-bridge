# claude-bridge 코드 리뷰 (긍정 관점)

Python 백엔드 개발자 시점에서 검토한 결과, 이 프로젝트는 **async/await 기반의 실시간 시스템 통합에서 상당히 우수한 설계 패턴과 방어적 프로그래밍**을 보여줍니다.

---

## 강점 목록

### 1. 원자성 보장한 락 구현 (Exclusive Create Pattern)

**근거:** `bot.py:226-245` (`_acquire_lock`)

```python
fd = lp.open("x")  # exclusive create — 이미 존재하면 FileExistsError
fd.write(f"{TMUX}\n{chat_id}")
fd.close()
return True
```

**강점:**
- `open("x")` 모드로 **원자적 파일 생성**을 보장합니다
- 멀티 인스턴스 환경에서 race condition을 OS 수준에서 방지
- Stale lock 감지 로직(`tmux has-session` 확인)으로 자동 복구
- Exception 처리로 락 디렉토리 권한 부족 시 graceful fallback

**이유:** 분산 락에서 가장 중요한 것은 원자성인데, `open("x")`는 POSIX의 O_EXCL 플래그를 활용하여 커널 수준 보장을 제공합니다. 이는 간단하면서도 견고합니다.

---

### 2. 정교한 상태 머신 설계 (monitor 함수)

**근거:** `bot.py:484-692` (Bridge.monitor 메서드)

주요 상태 전환:
- `awaiting_approval` → 승인창 감지
- `was_busy` → 진행 중 상태 감지  
- `settle` 카운터 → 화면 안정화 대기

```python
# busy 진입 엣지에서 직전 ⏺ 응답 flush (Fix 1)
if busy_now and not self.was_busy:
    response = extract_last_response(clean)
    if (response and "⏺" in response
            and response != self.last_sent
            and not self._already_sent(response)):
```

**강점:**
- 상태 변화의 **엣지(edge) 감지**로 "진입", "지속", "종료" 3단계를 명확히 구분
- 중복 전송 방지를 위해 `_already_sent()` 함수로 최근 20개 히스토리 추적
- MD5 해시로 화면 변화를 감지하고 `settle` 카운터로 **2초 안정화** 기다림
- Pre-busy flush, pre-approval flush로 **연쇄 도구 호출 중 중간 설명 손실** 방지

**이유:** 실시간 모니터링에서 상태 천이의 정확성이 UX를 결정합니다. 이 구현은 "과거에 본 응답 재전송 방지"와 "새로운 응답 누락 방지"를 동시에 해결합니다.

---

### 3. 방어적 프로그래밍: 다층 예외 처리

**근거:** `bot.py:333-365` (_parse_session_msgs), `bot.py:689-692` (monitor 루프)

```python
with open(path, errors="ignore") as f:
    for line in f:
        try:
            d = json.loads(line)
            # ... timestamp 파싱
            ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass  # 개별 라인 파싱 실패해도 루프 계속
```

**강점:**
- `errors="ignore"` → 파일 인코딩 문제로 전체 세션 목록이 깨지는 것 방지
- JSON 라인 파싱, timestamp 변환 각 단계에서 독립적 예외 처리
- **Silent fail이 아닌 Continue**: 한 줄이 깨져도 다음 데이터 처리
- Monitor 루프 최상위에서 `except Exception` (라인 689)로 daemon 안정성 보장

**이유:** 사용자 신뢰 데이터(세션 목록, JSONL)가 부분 손상되어도 서비스 가용성을 유지합니다. "모 아니면 도"가 아닌 **Best Effort** 원칙입니다.

---

### 4. 타입 힌트와 함수 시그니처의 명확성

**근거:** `bot.py:217`, `bot.py:288`, `bot.py:329`, `bot.py:456`, `bot.py:484`

```python
# 라인 217
def _parse_lock(session_id: str) -> tuple[str, int] | None:
    """락파일에서 (tmux_name, chat_id) 반환. 없거나 파싱 실패 시 None"""

# 라인 288
def find_sessions(limit: int = 8) -> list[dict]:

# 라인 329
def _parse_session_msgs(path: Path) -> tuple[str, str, float]:
    """(첫 메시지, 마지막 메시지, 마지막 활동 timestamp) 반환"""

# 라인 456
def _response_key(self, text: str) -> str:
    """중복 판정용 정규화 키 — 빈 줄/공백 무시한 정규화 문자열"""

# 라인 484
async def monitor(self, app: Application, chat_id: int):
```

**강점:**
- **Python 3.10+ Union 문법** (`tuple[str, int] | None`)으로 현대적 작성
- 모든 public 함수에 반환 타입 선언
- 함수 docstring에서 반환값 의미를 명시
- `async def`로 비동기 성질을 명확히 표현

**이유:** 타입 힌트는 IDE 자동완성, 정적 분석, 팀원 의도 전달을 동시에 돕습니다. Python 커뮤니티 best practice를 따릅니다.

---

### 5. Asyncio 활용의 적절성

**근거:** `bot.py:877-883`, `bot.py:920-941`

```python
# 라인 877-883: 기존 세션 재연결
if data == "reattach":
    await q.edit_message_text("기존 세션 재연결 중…")
    if bridge.task:
        bridge.task.cancel()
    bridge.task = asyncio.create_task(bridge.monitor(ctx.application, q.message.chat_id))
    return

# 라인 920-941: 권한 모드 토글 시 graceful restart
if bridge.skip_permissions:
    await q.edit_message_text(f"권한 모드 -> {perm_str}\n재시작 중...")
    ok = bridge.restart()
    if ok:
        bridge.task = asyncio.create_task(
            bridge.monitor(ctx.application, q.message.chat_id)
        )
```

**강점:**
- `asyncio.create_task()`로 monitor 함수를 daemon처럼 관리
- 기존 task 재시작 시 `task.cancel()` → 새 task 생성으로 안전한 전환
- Telegram API 호출(`await q.edit_message_text()`)이 블로킹되지 않음
- `await asyncio.sleep()`으로 상태 변화 감지 루프의 CPU 낭비 방지

**이유:** Telegram 봇의 callback 핸들러는 빠르게 반환해야 하는데, monitor 함수는 오래 실행됩니다. 별도 task로 분리하여 봇 응답성을 보장합니다.

---

### 6. 함수 분리와 가독성

**근거:** 전체 구조

**작은 함수들:**
- `strip_ansi()` (라인 100): ANSI 제거만 담당
- `is_trust_prompt()` (라인 103): 신뢰 프롬프트 패턴 매칭만
- `summarize_approval()` (라인 109): 승인 박스에서 도구명만 추출
- `busy_status()` (라인 141): 화면에서 상태 라인만 추출
- `extract_last_response()` (라인 157): Claude 응답만 추출 (가장 복잡한 로직이지만 단일 책임)

**큰 함수들 (필요한 경우만):**
- `monitor()` (라인 484): 상태 머신 전체 로직 (길지만 단일 책임: 모니터링)
- `_parse_session_msgs()` (라인 329): JSONL 파싱 (복잡도 높지만 분리 불가능)

**강점:**
- 각 정규식 패턴(`APPROVAL_RE`, `TRUST_RE`, `BUSY_RE`, `LIMIT_RE`)이 상수로 선언 (재사용 가능)
- 헬퍼 함수들이 **순수 함수**에 가깜 (부수 효과 최소)
- 대규모 함수(`monitor`)도 주석으로 논리적 블록 분리

**이유:** "관심사의 분리"를 지켜서, 각 함수가 하나의 문제만 해결합니다. 테스트/유지보수 비용 절감.

---

### 7. 정규식 패턴의 신중함과 문서화

**근거:** `bot.py:56-76`

```python
# Claude Code 승인 프롬프트 감지 패턴
# "Do you want to proceed" 가 정식 승인창 표식. ❯ 1. Yes 단독은 신뢰 프롬프트와 충돌하므로 사용 X
APPROVAL_RE = re.compile(
    r"Do you want to proceed|"
    r"Allow\s+\w+\s+to|Proceed\?|\(Y/n\)|\(y/N\)"
)

# 폴더 신뢰 프롬프트 (새 디렉토리 진입 시 한 번 뜸)
TRUST_RE    = re.compile(
    r"Quick safety check|"
    r"Is this a project you created|"
    r"trust this folder|"
    r"Yes, I trust|"
    r"No, exit|"
    r"Security guide"
)

# Claude가 "작업 중" 상태 - "esc to interrupt"만 신뢰 가능한 활성 신호
# (Running/Compacting 등은 과거 로그에도 남아서 오탐 발생)
BUSY_RE     = re.compile(r"esc to interrupt")
```

**강점:**
- 각 패턴 위에 **설계 의도 설명** (왜 이 정규식인가)
- `❯ 1. Yes`를 의도적으로 제외한 이유 명시 (신뢰 프롬프트 혼동 방지)
- "과거 로그도 남아서 오탐"이라는 **실제 경험에 기반한 주석**
- 최소한의 매칭으로 false positive 줄임

**이유:** 정규식 패턴은 버그의 온상입니다. 명확한 주석으로 미래의 자신(또는 팀원)이 패턴을 수정할 때 의도를 이해하도록 합니다.

---

### 8. 상태 메시지 관리의 세심함

**근거:** `bot.py:620-641` (busy 상태 표시)

```python
# busy 진입
if busy_now and not self.was_busy:
    self.was_busy = True
    self.busy_started_at = time.time()
    label = busy_status(clean)
    self.last_status_label = label
    _log("AI-BUSY", label)
    try:
        msg = await app.bot.send_message(chat_id, f"⏳ {label}… (0s)")
        self.status_msg_id = msg.message_id
    except Exception:
        self.status_msg_id = None

# busy 지속: 상태 메시지 편집
if busy_now:
    if self.status_msg_id and self.busy_started_at:
        elapsed = int(time.time() - self.busy_started_at)
        label = busy_status(clean)
        new_text = f"⏳ {label}… ({elapsed}s)"
        if new_text != getattr(self, "_last_status_text", ""):
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=self.status_msg_id,
                    text=new_text,
                )
```

**강점:**
- "상태 메시지 생성 실패"를 처리 (`status_msg_id = None`)
- **중복 편집 방지**: `new_text != getattr(self, "_last_status_text", "")` 조건 체크
- 경과 시간 표시로 사용자에게 진행 중임을 명확히 함
- busy 종료 시 상태 메시지 삭제 (채팅 깔끔함)

**이유:** 사용자는 "아무것도 안 하고 있나?"라고 불안해합니다. 진행 상황 피드백이 신뢰를 높입니다.

---

### 9. 설정 로드 및 보안 기본기

**근거:** `bot.py:22-36`

```python
_CONFIG_PATH = Path(__file__).parent / "config.json"

def _load_config() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

_cfg = _load_config()

TOKEN       = _cfg["token"]
TMUX        = _cfg.get("tmux_session", "claude_bridge")
CLAUDE      = _cfg["claude_path"]
PROJECTS    = Path.home() / ".claude" / "projects"
ALLOWED_IDS: set[int] = set(_cfg.get("allowed_ids", []))
```

**강점:**
- `token`은 필수 (`_cfg["token"]`), `allowed_ids`는 선택 (`_cfg.get(...)`)로 구분
- 환경변수 대신 `config.json` 사용으로 배포 시 민감 정보 격리 가능
- `ALLOWED_IDS`를 set으로 변환하여 O(1) 조회 성능 (라인 48: `in ALLOWED_IDS`)
- `Path.home()`으로 하드코딩 경로 회피

**이유:** 설정과 코드 분리, 타입 최적화(set), 경로 상대성(home) 모두 프로덕션 기본기입니다.

---

### 10. ANSI 제거와 텍스트 정규화

**근거:** `bot.py:76`, `bot.py:100-101`, `bot.py:533-540`

```python
ANSI_RE     = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)

# 사용 예
clean = strip_ansi(out).strip()
```

**강점:**
- tmux 터미널 출력의 **색상 코드, 커서 위치, 스타일을 정규식 1줄**로 제거
- ANSI 패턴 검증 (표준 ESC 시퀀스 구조 준수)
- `strip()` 추가로 양쪽 공백 제거 (정규화)

**이유:** 터미널 출력에는 사용자에게 보이지 않는 제어 문자가 많습니다. 이를 제거해야 정확한 패턴 매칭이 가능합니다.

---

### 11. 이미지 처리의 안전성

**근거:** `bot.py:985-1010`

```python
if msg.photo:
    photo = msg.photo[-1]   # 가장 큰 해상도
    import time
    fname = f"tg_{int(time.time())}.jpg"
    fpath = IMAGE_DIR / fname
    try:
        f = await ctx.bot.get_file(photo.file_id)
        await f.download_to_drive(custom_path=str(fpath))
        _log("USER→BOT", f"image {fname} ({photo.file_size or 0} bytes)")

        payload = f"[텔레그램 이미지 첨부: {fpath}]"
        if caption:
            payload += f"\n{caption}"
        send_input(payload)
```

**강점:**
- 여러 해상도 중 가장 큰 것 선택 (`msg.photo[-1]`)
- 타임스탬프 기반 파일명으로 충돌 방지
- 이미지 다운로드 실패 시 사용자에게 명시 (`except Exception as e`)
- `capture` → `send_input`로 Claude에 파일 경로만 전달 (효율)

**이유:** 파일 처리는 많은 예외 케이스가 있습니다. 명시적 예외 처리로 안정성을 높입니다.

---

### 12. 로깅 전략

**근거:** `bot.py:38-45`

```python
def _log(tag: str, msg: str = ""):
    """구조화된 포그라운드 로그"""
    import time
    ts = time.strftime("%H:%M:%S")
    if msg:
        print(f"[{ts}] [{tag}] {msg}")
    else:
        print(f"[{ts}] [{tag}]")
```

**사용 예시:**
- `_log("BOOT-SEED", f"마지막 ⏺ 블록 {len(seed_resp)}자 무시 처리")`
- `_log("AI→BOT", f"pre-approval flush ({len(response)} chars)")`
- `_log("USER-ACK", "approved")`

**강점:**
- **시간:태그:메시지** 구조로 쉽게 필터링 가능 (grep `[AI→BOT]`)
- 태그가 데이터 흐름 방향을 명확히 함 (`USER→AI`, `AI→BOT`, `BOT→USER`)
- 메시지 선택적 (상태 변화만 기록)
- 프로덕션 환경에서 logging 모듈로 쉽게 확장 가능

**이유:** Daemon 모니터링에서 터미널 로그가 유일한 디버그 수단입니다. 구조화된 로그로 문제 추적을 빠르게 합니다.

---

### 13. 텍스트 청킹과 메시지 크기 제한

**근거:** `bot.py:699-739` (_chunk_text, _send_output)

```python
def _chunk_text(text: str, size: int = 3500) -> list[str]:
    """긴 텍스트를 줄 단위로 size 이하로 나눔"""
    chunks = []
    buf = []
    cur = 0
    for line in text.splitlines(keepends=True):
        if cur + len(line) > size and buf:
            chunks.append("".join(buf))
            buf = [line]
            cur = len(line)
        else:
            buf.append(line)
            cur += len(line)
        # 한 줄이 size보다 크면 강제 분할
        while cur > size:
            s = "".join(buf)
            chunks.append(s[:size])
            remain = s[size:]
            buf = [remain]
            cur = len(remain)
    if buf:
        chunks.append("".join(buf))
    return chunks or [""]
```

**강점:**
- 줄 단위로 분할하여 의미 있는 경계 유지 (줄 중간에서 자르지 않음)
- 한 줄이 size보다 크면 강제 분할 (무한 루프 방지)
- Edge case 처리 (빈 결과 시 `[""]` 반환)
- 청크 수를 `[i/{total}]` 헤더로 사용자에게 표시

**이유:** Telegram은 메시지 크기 제한(4096)이 있습니다. 여러 메시지로 나누되, 줄 구조를 존중합니다.

---

### 14. 승인 박스 추출의 정확성

**근거:** `bot.py:741-758` (_approval_box)

```python
def _approval_box(text: str) -> str:
    """'Do you want to proceed?' 위쪽 가장 가까운 divider~prompt 사이만 추출."""
    lines = text.splitlines()
    proceed = -1
    for i in range(len(lines) - 1, -1, -1):
        if "Do you want to" in lines[i]:
            proceed = i
            break
    if proceed < 0:
        return text[-400:]
    start = max(0, proceed - 30)
    for i in range(proceed - 1, start - 1, -1):
        s = lines[i].strip()
        if s and len(s) > 20 and all(c in "─" for c in s):
            start = i + 1
            break
    end = min(len(lines), proceed + 6)  # Yes/No/Esc 안내 몇 줄 포함
    return "\n".join(lines[start:end]).strip()
```

**강점:**
- "Do you want to proceed"를 **역순 탐색**으로 가장 최근 승인창 찾기
- 위쪽으로 30줄 범위에서 divider(`─` 라인)를 찾아 승인 박스 시작점 결정
- 아래쪽으로 6줄만 포함하여 사용자에게 필요한 정보만 표시
- Fallback: divider 없으면 마지막 400자 반환

**이유:** Claude Code 출력에서 "승인 요청" 부분을 정확히 추출하면 사용자가 빠르게 판단할 수 있습니다.

---

### 15. 세션 락 실패 처리의 graceful degradation

**근거:** `bot.py:244-245`

```python
except Exception:
    return True  # 락 디렉토리 문제 시 허용 (방어적)
```

**근거:** `bot.py:963-967`

```python
if session_id and _is_locked(session_id):
    await ctx.bot.send_message(q.message.chat_id, "⚠️ 이 세션은 다른 인스턴스에서 사용 중입니다.")
else:
    await ctx.bot.send_message(q.message.chat_id, "시작 실패 - tmux/claude 경로를 확인하세요.")
```

**강점:**
- 락 파일 생성 실패(권한 부족 등)는 **soft failure**: 서비스 계속
- 락 획득 실패는 **hard failure**: 명시적 오류 메시지
- 사용자에게 오류 원인을 구분 제시 (세션 점유 vs. 경로 문제)

**이유:** 완벽한 락이 불가능한 환경도 있습니다. Graceful degradation으로 최대한 서비스합니다.

---

### 16. 시간 기반 활동 추적

**근거:** `bot.py:288-318` (find_sessions)

```python
now = time.time()
candidates = []
for p in PROJECTS.rglob("*.jsonl"):
    # ...
    activity_ts = last_ts if last_ts > 0 else p.stat().st_mtime
    # 최근 60초 내 활동은 현재 활성 세션 가능성 → 제외
    if now - activity_ts < 60:
        continue
    # 다른 인스턴스가 resume 중인 세션 제외
    if _is_locked(p.stem):
        continue
    # ...
    candidates.append({
        # ...
        "mtime": datetime.fromtimestamp(activity_ts).strftime("%m/%d %H:%M"),
        # ...
    })

candidates.sort(key=lambda x: x["activity_ts"], reverse=True)
return candidates[:limit]
```

**강점:**
- JSONL의 timestamp와 파일 mtime을 비교하여 **정확한 활동 시간 추출**
- 최근 60초 활동 세션은 제외 (현재 실행 중일 가능성)
- 락 상태도 확인하여 다른 인스턴스 점유 세션 제외
- 시간순 정렬로 "가장 최근에 작업한 세션" 먼저 표시

**이유:** 사용자는 "어떤 세션을 마지막에 했지?"라는 직관적 질문에 답하려고 합니다. 이 로직이 정확하면 경험이 좋습니다.

---

## 종합 평가

### 주요 설계 철학

1. **원자성과 동시성**: Exclusive file create로 분산 락 구현
2. **상태 머신의 정확성**: Edge 감지로 중복/누락 방지
3. **방어적 프로그래밍**: Exception handling 다층화, graceful degradation
4. **사용자 경험**: 상태 피드백, 오류 구분, 시간 정보 표시
5. **코드 가독성**: 함수 분리, 주석, 타입 힌트, 로깅

### 특히 우수한 부분

- **Lock 구현**: 간단하면서도 견고한 원자성 보장
- **Monitor 루프**: 복잡한 상태 전환을 명확하게 처리
- **Error Recovery**: Stale lock 자동 정리, partial data 계속 처리
- **UX 디테일**: 진행 상황 업데이트, 오류 원인 구분 표시

### 프로덕션 준비 상태

이 코드는 다음 특징으로 프로덕션 서비스에 적합합니다:
- ✅ 예외 처리가 체계적
- ✅ 상태 추적이 명확
- ✅ 디버그 로깅이 풍부
- ✅ 멀티 인스턴스 안전성 고려
- ✅ Daemon 안정성 (크래시 후 자동 복구)

---

## 결론

Python 백엔드 개발자 관점에서, 이 프로젝트는 **비동기 시스템 통합의 실제 난제들(상태 동기화, 분산 락, 재시도 논리)을 깊이 있게 해결**한 품질 높은 코드입니다. 특히 복잡한 로직을 이해하기 쉽게 구성한 점, 그리고 "완벽함" 대신 "견고한 graceful degradation"을 추구한 철학이 돋보입니다.
