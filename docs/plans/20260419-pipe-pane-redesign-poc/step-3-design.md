# Step 3 — Decision Gate 결과 및 TO-BE 설계

- 작성일: 2026-04-19
- 상태: **초안** — 분야별 critic 검토 대기
- 선행: [step-2-results.md](step-2-results.md), [README.md](README.md)
- 지향: Step 4 (온라인 통합) / Step 5 (기존 파서 제거) 의 설계 기준선

## 0. Gate 판정 (갱신)

step-2-results.md 의 원안 "3/5 Pass + Criterion 3 기준 재정의 권고" 에 대해
사용자 확인 후 **Criterion 3 폐기 + Criterion 3' 신설** 로 재정의.

| # | Criterion | 판정 |
|---|-----------|------|
| 1 | 재현성 | Partial — case-04 pass, case-01/02/03 raw 부재 (후속 수집) |
| 2 | 모달 정확도 | Pass |
| 3 | ~~LoC ≤ 70%~~ → **유지보수 면적** (§7) | Pass (재정의) |
| 4 | 스피너 rate | Pass |
| 5 | 관측성 | Pass |

→ **Gate 조건부 통과**. 단, 본 문서의 전환 전략이 critic 검토 후에도 유지될 때 한함.

## 1. 전환 전제 (사용자 결정 반영)

step-2-results 직후 사용자 제기 논점:

> "기존 분석 로직을 **냉정히 유지해야 하는가** 가 주제"

**합의된 답**: pipe-pane raw + VT paradigm 으로 전환하는 순간 기존 `parser.py` /
`stream_queue.py` 의 로직은 **capture-pane 제약의 산물** 이므로 본질적 가치가
사라진다. 최소 변경 재사용은 "두 paradigm 공존" 을 만들어 유지보수 비용이
**2배** 가 되고, 경계에서 버그가 새로 태어난다. 따라서:

- **로직**: 폐기 (parser.py / stream_queue.py 전체, 약 518 LoC).
- **도메인 지식**: 채광 — UI 패턴 카탈로그, edge case 경험, 문자열 fingerprint.
- **테스트**: 채광 — 회귀 invariant 는 보존하되 fixture 를 raw ANSI 로 교체.
- **인터페이스**: 교체 — `bridge/core.py` 가 분석기 이벤트를 직접 구독.

## 2. 스코프 — 유지 / 폐기 매트릭스

| 구성요소 | 위치 | 정책 | 이유 |
|----------|------|------|------|
| `_response_region` 경계 탐색 | parser.py:214-257 | **폐기** | VT row 가 직접 주어짐 |
| `StreamQueue.idx/last_fp` | stream_queue.py | **폐기** | `(offset, t)` 가 구조적 dedup 키 |
| `is_approval` tail-scan | parser.py | **폐기** | envelope 구조 검사가 상위 호환 |
| `_approval_context`/`_box` | parser.py | **폐기** | region_tagger + screen snapshot 대체 |
| `extract_response_blocks` | parser.py | **폐기** | commit stream `⏺` + continuation 이 1:1 대체 |
| `_is_block_active` | parser.py | **폐기** | `_block_buf_text` 유무로 자연스럽게 해결 |
| `_find_trust_prompt_region` | parser.py | **포팅** | 같은 envelope 패턴으로 분석기에 이식 필요 |
| bypass permissions / shortcut HUD 인식 | parser.py chrome 감지 | **일부 이식됨** | region_tagger `_is_chrome()` 에 반영 |
| resume picker / compact / limit 배너 | parser.py | **포팅** | event_classifier 에 타입 추가 + regex 이관 |
| case-02 user-echo 판별 | parser.py (is_approval tail 60) | **재설계** | send_input log 상관으로 구조 해결 |
| `strip_ansi` (dump.py) | dump.py | **유지** | 로깅/디버그 용도, 분석 경로에 쓰이지 않음 |
| events.jsonl 스키마 | dump.py | **확장** | 분석기 표준 이벤트와 merge |
| tmux 기반 send-keys / resize | tmux.py | **유지** | 입력 경로는 무관 |
| pipe-pane 수집 훅 | tmux.py / core.py (Step 1) | **유지** | 이미 검증됨 |

## 3. 모듈 경계 및 책임 (TO-BE)

```
 raw bytes            ┌───────────────┐
 (pipe-pane)    ───▶  │  Tokenizer    │──── Token 스트림
                      └───────────────┘
                              │
                              ▼
                      ┌───────────────┐
                      │  VTScreen     │──── screen state (24×200, alt)
                      └───────────────┘
                              │
                              ▼
                      ┌───────────────┐
                      │ LineCommitter │──── LineCommit(offset, row, text, reason, screen)
                      └───────────────┘
                              │
                              ▼
                      ┌───────────────┐
                      │ RegionTagger  │──── TaggedLine(content|input_box|modal_overlay|chrome)
                      └───────────────┘
                              │
                              ▼
                      ┌───────────────┐
                      │EventClassifier│──── AnalyzerEvent{offset, t, region, payload}
                      └───────────────┘
                              │
                              ▼
                      ┌───────────────────────────┐
                      │   BridgeAdapter (신규)    │
                      │   · Telegram dispatch     │
                      │   · dump.events() 로깅    │
                      │   · send_input 상관       │
                      └───────────────────────────┘
```

**단일 책임 원칙**: 각 모듈은 한 단계만 담당. 교체 시 이웃 모듈의 인터페이스만
맞추면 됨 (예: tokenizer 를 Rust 구현으로 바꿔도 VTScreen 는 무변경).

**BridgeAdapter** 가 신규 경계:
- 입력: `AnalyzerEvent` 스트림 + `send_input` log (user prompt 상관용).
- 출력: Telegram 메시지 전송, dump 이벤트 기록, 상태 캐시 갱신.
- 기존 `parser.py`/`stream_queue.py` 호출 지점을 **전부** adapter 로 대체.

## 4. Feature Parity Gap

분석기 현재 이벤트 카탈로그 (Step 2):
`block_commit`, `approval_show`, `approval_cancel`, `busy_enter`, `busy_exit`,
`session_boot`, `session_resume`.

Step 4 통합 전 **반드시** 채워야 하는 갭:

| 이벤트 | 상태 | 포팅 출처 | 난이도 |
|--------|------|-----------|--------|
| `approval_confirm` / `approval_deny` | 미구현 | 기존 코드엔 없음 (분석기 신규) — Enter 감지 + `❯` 최종 위치 관찰 | 중 |
| `user_prompt` | 미구현 | `send_input` log 상관 — bridge 이 직접 push 한 입력을 이벤트화 | 하 |
| `compaction_start` | 미구현 | parser.py 의 compact 배너 regex 이관 | 하 |
| `limit` | 미구현 | parser.py 의 rate limit 배너 regex 이관 | 하 |
| `trust_prompt` | 미구현 | `_find_trust_prompt_region` → envelope 패턴으로 재작성 | 중 |
| `resume_picker` | 미구현 | parser.py resume picker regex 이관 + `❯` 위치 tracking | 중 |
| case-02 user-echo 구분 | 미구현 | user_prompt 상관으로 구조 해결 (tail-scan 대비 상위 호환) | 중 |

## 5. 전환 단계 (Migration Plan)

각 단계는 **가역**. 중단 지점에서 이전 paradigm 으로 즉시 복귀 가능해야 함.

### Step 4-α: Shadow Run (최소 24h, 권장 72h)

- 분석기를 bridge 와 **병행 실행**. dispatch 는 기존 parser 경로가 계속 담당.
- 분석기 출력을 별도 파일 `~/.claude-bridge/panes/<session>/analyzer-events.jsonl`
  로 기록. 기존 events.jsonl 과 **이벤트 diff** 를 주기적 (1h) 계산.
- 판정 기준:
  - `block_commit` 개수가 기존 `response_block` dump 와 ±5% 이내.
  - `approval_show` 발생이 기존 `is_approval=1` dump 의 superset (기존이 못 본
    케이스를 추가로 잡아도 OK, 그 반대는 NG).
  - 스피너 rate 가 실측 환경에서도 ≤ 5건/초.
- 종료 조건: 72h 연속 diff 허용 범위 내 + 사용자 확인.

### Step 4-β: Feature Parity

- §4 의 모든 이벤트 타입을 분석기에 추가 + fixture test.
- case-01/02/03 raw 재수집 — shadow run 기간에 자연 재현되지 않으면 재현 스크립트
  작성 (bridge 자체 리플레이 경로 권장, tmux replay 보다 안정).
- 단위 테스트 커버리지: 각 모듈 ≥ 80%, 신규 이벤트는 fixture + synthetic 양쪽.

### Step 4-γ: BridgeAdapter 도입 (dispatch cutover)

- `bridge/core.py` 의 parser 호출을 adapter 로 교체.
- 기존 parser.py / stream_queue.py 는 **남겨두되 비활성**. rollback 스위치
  `BRIDGE_DISPATCH_SOURCE=parser|analyzer` 환경변수로 선택.
- 프로덕션 1주 관찰. regression 발생 시 env 하나로 즉시 롤백.

### Step 5: 기존 파서 경로 제거

- parser.py / stream_queue.py 삭제, 관련 테스트도 삭제.
- dump.py 이벤트 스키마 정리 (분석기 필드만 남김).
- 문서 (design/02, 03) TO-BE 재작성.

## 6. 리스크와 완화

| 리스크 | 영향 | 완화 |
|--------|------|------|
| VT 구현 ≠ tmux 렌더 (row 어긋남) | region_tagger 가 envelope 을 놓침 | shadow run 으로 실측 diff. 실패 시 row 탐색 범위를 ±12 로 확대하거나 col 패턴 보강 |
| pipe-pane 파일 유실/회전 race | 이벤트 누락 | byte offset + **inode 감지** (이미 Step 1 rotate 존재). 재기동 시 last_offset 기록에서 이어받음 |
| alt-screen 도구 (fzf, git log, less) 혼입 | 오탐 또는 content 침범 | tokenizer 가 alt-screen 분리 유지, region="modal_overlay" 로 격리. Telegram 에는 미송출 |
| perf: chunk 당 tokenize+VT 비용 | CPU 증가 | 현재 450KB 처리 < 1s (offline). 온라인은 chunk 단위 (≤ 64KB) 증분. 실측은 shadow run 에서 |
| edge case 누락 (bypass 경고, custom prompt) | 미검출 또는 오탐 | shadow run diff 가 **조기 경보**. 패턴 catalogue 는 별도 data file 분리 (regex 아카이브) |
| 분석기 state 손상 시 복구 불가 | 이벤트 영구 누락 | offset-based resume 가능. 재기동 시 마지막 커밋된 offset 부터 재생 |
| send_input 과의 상관 누락 | case-02 user-echo 미구분 | adapter 에서 `user_prompt` 이벤트를 **우선 발화**, 이후 동일 텍스트가 content 로 오면 suppress |

## 7. Decision Criterion 3 재정의 — **유지보수 면적 (Maintainability Surface)**

**폐기**: 원문 "ANSI tokenizer + commit 판정 LoC ≤ parser.py:214-332 의 70%".

**신설 — 3개 sub-criterion 중 최소 2개 충족 시 Pass**:

### 3a. 단일 책임 계층화
- 각 모듈이 한 단계의 변환만 담당, 다음 단계 입력/출력은 dataclass 로 고정.
- 측정: 모듈 간 **순환 import 0건**, 각 public 함수의 반환 타입이 단일 dataclass.
- 현재 상태: **Pass** (Tokenizer→VTScreen→LineCommit→TaggedLine→AnalyzerEvent 단방향).

### 3b. 변경 국소성
- 새 이벤트 타입 추가 시 touch 되는 모듈 ≤ 2개 (event_classifier + 테스트).
- 측정: `compaction_start` 추가 시뮬레이션 → classifier 에 regex 1개 + handler
  1개 + 테스트 2개. VTScreen/tokenizer 무변경.
- 현재 상태: **Pass**.

### 3c. 테스트 격리도
- 각 모듈이 **독립** 단위 테스트 가능 (상위 모듈 없이).
- 측정: `test_ansi_tokenizer.py` 는 VTScreen 의존 없음, `test_vt_screen.py` 는
  Tokenizer 의존 없음 등.
- 현재 상태: **Pass** — 6개 테스트 파일 전부 자기 모듈 + 직접 의존만 사용.

**정성적 보충**: LoC 는 복잡도 proxy 로서 **부적합** (저밀도 보일러 plate 가
유지보수 비용에 거의 기여하지 않음). 대신 "변경 난이도" / "테스트성" / "경계
명확성" 을 측정. 이 세 축에서 분석기가 기존 parser.py 대비 **명확 우위** — 기존
코드의 `_response_region` + `is_approval` + `extract_response_blocks` 는 한 함수
안에서 tail 정책 + 경계 탐색 + dedup 이 **뒤엉킴** (side-effects.md §B 참조).

## 8. 관측성 표준

- 모든 이벤트는 `{offset, t, region, payload...}` 포맷. offset 은 pipe-pane 파일
  내 byte offset (session 시작 이후 누적).
- dump.events() 는 아래 merge:
  - `analyzer.<t>` prefix — 분석기 직접 발화.
  - `bridge.dispatch` — adapter 가 Telegram 으로 보낸 최종 결과.
  - `bridge.suppress` — adapter 가 drop 한 이벤트 + 이유.
- `--until-offset N` 분석기 CLI 옵션은 **유지**. 장애 보고 시 "이 offset 까지 어떻게
  해석됐는지" 를 결정적으로 재현할 수 있어야 함.
- shadow run 동안엔 두 경로의 이벤트 diff 를 `~/.claude-bridge/panes/<session>/
  shadow-diff.jsonl` 에 기록. diff 가 이상적으로 0 이 되진 않음 — 분석기가
  **기존을 대체** 하는 의도이므로 "기존이 놓친 것" 은 superset 으로 허용.

## 9. Step 4/5 Exit Criteria

### Step 4 완료 조건 (adapter cutover 합격)

- [ ] shadow run ≥ 72h, event diff 허용 범위 유지.
- [ ] case-01/02/03 fixture 분석기 pass.
- [ ] §4 gap 이벤트 전부 구현 + 단위 테스트.
- [ ] BridgeAdapter 가 Telegram 경로에서 parser 와 동일 이상 동작 (수동 QA 30분).
- [ ] rollback env 스위치 동작 검증 (`BRIDGE_DISPATCH_SOURCE=parser` 로 즉시 회귀).

### Step 5 완료 조건 (기존 파서 제거)

- [ ] Step 4 이후 **프로덕션 2주** 무회귀.
- [ ] parser.py / stream_queue.py 제거 PR, 관련 테스트 삭제 또는 raw-fixture 로 치환.
- [ ] design/02, 03 TO-BE 재작성.
- [ ] 삭제된 LoC 대비 분석기 LoC 공식 기록 (참고용, 판정 기준 아님).

## 10. Open Questions (사용자 판단 대기)

1. **case-01/02/03 raw 재수집 방법**:
   - (A) shadow run 기간 중 자연 재현 기다림 — 현실성 낮음 (case-03 은 500줄
     eviction 요구).
   - (B) 리플레이 스크립트 작성 — bridge 의 기존 send_input 으로 동일 입력 시퀀스
     재생. 안정적이나 작성 비용 2-3일.
   - (C) 합성 fixture — synthetic ANSI 로 symptom 재현. 빠르나 실제 Claude Code
     출력과 괴리 가능.
   - 권장: (B) + Step 4-α 병행.

2. **shadow run 최소 기간**:
   - 72h 가 spec. 프로덕션 사용빈도에 따라 1주 이상도 고려. 사용자 세션 패턴에 좌우.

3. **기존 parser.py 의 "잠금 해제" 시점**:
   - adapter 가 dispatch 를 담당하는 동안 parser.py 를 **수정 금지 영역** 으로
     동결할지. 회귀 수정 압력이 있으면 갈등 가능.
   - 권장: Step 4-γ 시작부터 freeze, Step 5 에서 제거.

4. **BridgeAdapter 위치**:
   - (A) `bridge/adapter.py` 신규 모듈.
   - (B) `bridge/core.py` 안에 계층 추가.
   - 권장: (A) — 단일 책임 유지, Step 5 에서 core.py 정리 작업 분리 가능.

5. **env 플래그 / 디폴트**:
   - `BRIDGE_DISPATCH_SOURCE` 디폴트 값. Step 4-γ 초기엔 `parser`, shadow 결과
     검증 후 `analyzer` 로 전환.

## 11. Critic 검토 요청 항목

아래 5명의 분야별 critic 에게 병렬 리뷰 요청 (`docs/reviews/20260419-step-3-design/`):

1. **Architecture critic** — 모듈 경계, paradigm 일관성, 책임 분리.
2. **Dev/Testability critic** — 구현 난이도, 테스트 전략, fixture 유지성.
3. **Reliability/Ops critic** — 실패 모드, 복구, 관측성, perf.
4. **UX/Telegram critic** — 사용자 노출 표면, 잠재 regression.
5. **Migration critic** — 전환 safety, 롤백 경로, 기존 로직 폐기 합리성.

각 critic 출력:
- `<role>_positive.md` — 현 설계의 강점/지지.
- `<role>_critic.md` — 누락/위험/개선 권고.

취합: `SUMMARY.md` — 공통 concern, 단일 의견, 설계 수정 제안.

## 12. Decision Log (임시)

- **2026-04-19** — Criterion 3 재정의 승인 (LoC → 유지보수 면적 3축). 근거:
  ANSI tokenizer/VT 가 기존 아키텍처 외부(`tmux capture-pane`) 였던 역량을
  내재화하면서 LoC 비교가 비공정해짐.
- **2026-04-19** — 기존 parser.py / stream_queue.py **폐기** 결정.
  근거: "로직이 아니라 도메인 지식만 가치 있음" 사용자 판단.
- **2026-04-19** — Step 4 = shadow run + feature parity + adapter cutover 3단계
  분할. 각 단계 가역.
- **2026-04-19** — 5명 분야별 critic 병렬 리뷰 완료
  ([../../reviews/20260419-step-3-design/SUMMARY.md](../../reviews/20260419-step-3-design/SUMMARY.md)).
  P0 5개, P1 5개, P2 3개 의 설계 수정/보강 내역을 §13 에 반영.

## 13. Critic 반영 — 보강/수정 내역

5명 critic (Architecture / Dev·Testability / Reliability·Ops / UX·Telegram /
Migration) 리뷰 취합
([SUMMARY](../../reviews/20260419-step-3-design/SUMMARY.md)) 을 본 설계에 반영.
원본 §1–12 의 기조는 유지하되, 아래 항목을 추가/수정.

### 13.1 BridgeAdapter 명세 (P0 — C1+U1+U2+C6 해소)

§3 의 BridgeAdapter 블록이 "Telegram dispatch + dump 로깅 + send_input 상관"
한 줄로만 서술됐다는 지적. Step 4-γ cutover 전에 아래 수준의 spec 이 확정돼야 함.

#### 13.1.a 공개 인터페이스 (`bridge/adapter.py`, 신규)

```python
class BridgeAdapter:
    def __init__(
        self,
        telegram_sender: ...,   # 기존 sender.py 주입
        dump_writer: ...,       # 기존 dump.py 주입
        config: AdapterConfig,  # 아래 §13.1.c
    ) -> None: ...

    def process_event(self, event: AnalyzerEvent) -> None:
        """분석기 이벤트 1건을 소비해 Telegram/dump 에 반영."""

    def register_send_input(self, offset: int, text: str) -> None:
        """bridge 가 send-keys 로 보낸 입력의 offset 기록.
        user_prompt 이벤트 emit + 이후 동일 텍스트 echo suppress 용."""

    def flush(self) -> None:
        """세션 종료/롤백 시 held state 안전하게 비움."""
```

#### 13.1.b 이벤트 × 액션 × Telegram 매핑 (기본값)

| AnalyzerEvent | Adapter 동작 | Telegram 출력 | 비고 |
|---------------|--------------|--------------|------|
| `block_commit` | `sender.send_response(text, kind)` | 메시지 전송 | 기존 동작 유지 |
| `approval_show` | `sender.send_approval(tool_hint, summary, box_text)` | 승인 요청 + 버튼 | payload 에 `box_text` (multi-line snippet) 추가 필요 |
| `approval_confirm` | `sender.delete_approval_message` | 승인 메시지 편집/제거 | Step 4-β 후 활성 |
| `approval_deny` | `sender.update_approval("취소됨")` | 메시지 편집 | Step 4-β 후 활성 |
| `approval_cancel(method="flush")` | `sender.notify_cancel(reason)` | "⚠️ 승인 대기 중 연결 끊김" | **즉시 통보** (case-04 의 2분 대기 재발 방지) |
| `approval_cancel(method="esc")` | Adapter 내부 상태 reset | suppress | 사용자가 ESC 누른 경우 재시도 기다림 |
| `busy_enter` | internal state → spinner | **suppress (기본)** | /status 에서 조회 가능. throttle 5초 옵션 (§13.1.c) |
| `busy_exit` | internal state clear | suppress | 동 |
| `session_boot` | dump 기록만 | suppress | noise 증가 방지 |
| `session_resume` | dump 기록만 | suppress | 동 |
| `user_prompt` (Step 4-β) | Telegram echo 없이 내부 상태 (case-02 suppression key) | suppress | 동일 text 의 후속 block_commit 을 `suppressed_user_echo` 로 전환 |
| `compaction_start` (Step 4-β) | `sender.notify("컨텍스트 압축 중…")` | 1회 전송 | 기존 parser 의 compact 배너 동작과 유사 |
| `limit` (Step 4-β) | `sender.notify_limit(message)` | 1회 전송 | 동 |

#### 13.1.c AdapterConfig (기본값 + env override)

```python
@dataclass
class AdapterConfig:
    busy_throttle_sec: float = 5.0          # busy_enter → Telegram progress 갱신 간격
    busy_dispatch: bool = False             # 기본 suppress; True 시 throttle 적용
    boot_banner_dispatch: bool = False      # session_boot/resume Telegram 출력 여부
    approval_cancel_notify: bool = True     # flush 시 즉시 통보 (case-04 대응)
    analyzer_crash_fallback: str = "notify" # notify | silent | parser_fallback
    analyzer_max_restarts: int = 3          # fallback=parser_fallback 로 넘어가기 전 재시도
```

### 13.2 Shadow run 의 전제 조건 강화 (P0 — C2 해소; C4 는 §13.11 에서 폐기)

> **참고**: 본 섹션의 shadow_diff 판정 기준과 row drift 항목은 §13.11 에서 재정의됨.
> P0 체크리스트는 §13.7 최종본 참조.

§5 Step 4-α 의 착수 조건을 아래로 갱신:

- [ ] `tools/shadow_diff.py` 구현 + case-04 fixture 로 self-test 통과.
      입력: `events.jsonl` (parser) + `analyzer-events.jsonl` (analyzer).
      출력: markdown 리포트 — **집합 비교 기반** (§13.11.a):
      `parser ∩ analyzer` (공통), `parser - analyzer` (regression 후보),
      `analyzer - parser` (신규 탐지). spinner rate, tokenizer health 포함.
- [ ] EventClassifier 에 monotonic offset assertion 추가 (D1, 30 분).
- [ ] ~~RegionTagger 의 row envelope 탐색을 `match_distance` 기록~~ — **삭제 (§13.11.b)**.
      row 번호는 debug 보조 (non-authoritative) 로만 취급.
- [ ] Disk precheck: `BRIDGE_PIPE_PANE_MIN_FREE_MB=500` (기본값). 부족 시 start 거부.

Shadow run **통과** 조건 (§5 원문 보강):
- `parser - analyzer = ∅` (analyzer 가 parser 이벤트를 전부 포함). ±% 개념 폐기 (§13.11.a).
- `analyzer - parser` 는 허용 — case-04 등 신규 탐지는 진보 신호.
- `approval_show` ⊇ parser `is_approval=1` (superset) **AND** false positive 0건.
- `tokenizer_incomplete_escape` 누적 카운트 0 (disk/rotation 에 의한 desync 0).

### 13.3 user_prompt + case-02 스펙 (P0 — C3 해소; **§13.11.c 로 재설계**)

> **재설계 공지**: 본 섹션의 byte-offset suppression window 방식은 §13.11.c 의
> **region-based echo suppression** 으로 대체됨. byte-offset 은 tie-breaker 로 강등.
> 이하 내용은 역사적 기록으로 남김.

§4 gap 의 `user_prompt` 구현 상세:

- **emit 시점**: `BridgeAdapter.register_send_input(offset, text)` 호출 시점에
  adapter 가 즉시 `user_prompt` 이벤트를 dump.events 에 기록. 분석기는 불관여.
- **offset**: bridge 가 `tmux send-keys` 를 실행한 직후 pipe-pane 파일 크기 (**writer
  side offset**). 실제로 pane 에 렌더되는 offset 과는 ≤ 수 KB 차이.
- ~~**suppression key**: `(register_offset, register_offset + len(text) + K)` 창~~
  — **폐기 (§13.11.c)**. Prompt box region 내부 commit 은 정의상 user_echo.
- **스키마**: §13.11.c 최종본 참조.
- **테스트**: case-02 raw 재수집 없이 합성 fixture 로 Step 4-β 내 구현 + 테스트.
  case-02 raw 는 Step 4-β deliverable (§13.5).

### 13.4 Offset 무결성 safeguard (P0 — R1+R2 해소)

§6 리스크 표의 "pipe-pane 파일 유실/회전 race" 행을 아래로 치환/확장:

- **Pre-flight disk check**: `BRIDGE_PIPE_PANE_MIN_FREE_MB` 기본 500. 미달 시
  session attach 실패 + user 에게 `sender.notify_error("디스크 부족")`.
- **File identity 기록**: rotate 와 resume 시 `{offset, inode, size, mtime}` 모두
  기록. resume 시 검증: inode 일치 + size ≥ last_offset → 정상 재개, 불일치 → offset
  reset 후 `dump.event("analyzer", "resume_reset", reason=...)`.
- **Rotate gap 명시**: rotate 시 stop→start 사이 gap bytes 를 측정해 이벤트로
  emit. gap > 0 이면 shadow-diff 계산에서 해당 경계 제외.
- **State atomic write**: 분석기 state 파일 (`analyzer_state.json`) 은 tmpfile
  → fsync → rename 순서. 2-version (`.current` + `.prev`) 유지.
- **Tokenizer robust streaming**: incomplete escape sequence 는 drop 금지, 다음
  chunk 까지 buffer 에 유지. `tokenizer_incomplete_escape` 카운트 기록.
- **Auto-restart**: analyzer crash 시 최대 3회 재기동 (마지막 커밋 offset resume).
  이후 `AdapterConfig.analyzer_crash_fallback` 정책 적용 (13.1.c).

### 13.5 Migration 보강 (P0 — C5, P1 — C7·C8·M2·M3)

§5 Step 4-α/β/γ 를 아래 사항으로 강화:

#### Shadow run 동안의 storage 분리 (C5)

- 분석기 이벤트는 **반드시 별도 파일** `~/.claude-bridge/panes/<session>/
  analyzer-events.jsonl`. 기존 `events.jsonl` 은 parser 전용 유지.
- Shadow-diff 결과는 `~/.claude-bridge/panes/<session>/shadow-diff.jsonl`.
- Step 4-γ cutover 시: `events.jsonl` 을 `.before-cutover` 로 rename, 분석기 기반
  새 파일 시작. rollback 필요 시 rename 복원 + 분석기 파일 `.rollback-N` 접미로 이동.

#### Step 4-γ frozen parser 강제 (M2)

- Step 4-γ 개시 시 `bridge/parser.py`, `bridge/stream_queue.py` 의 수정 차단 —
  pre-commit hook (`.githooks/freeze-parser.sh`) 또는 `CODEOWNERS` 에 freeze-owner
  지정. 긴급 수정 필요 시 freeze 해제 PR + 리뷰 경유.

#### Analyzer crash fallback (C7)

- `AdapterConfig.analyzer_crash_fallback`:
  - `"notify"` (default): Telegram 에 "⚠️ 내부 분석기 재기동 중" 1회 전송 + 자동
    재시도. 3회 실패 시 `"silent"` 로 격하.
  - `"silent"`: 사용자 무통보, dump 기록만. Step 4-γ 초기에 선호되지 않음.
  - `"parser_fallback"`: `BRIDGE_DISPATCH_SOURCE=parser` 로 자동 전환 +
    sender 에 "정상 동작, 구 버전 사용 중" 통보. frozen parser 해제 창구.

#### Case-01/02/03 재수집 deliverable (C8)

- Step 4-β 착수 시 deliverable 로 승격. 방법 (B) reproducer 스크립트 + 방법 (C)
  합성 fixture 병행:
  - case-01: reproducer 1일 (approval → ESC → send).
  - case-02: reproducer 1일 (외부 paste 시뮬레이션).
  - case-03: 합성 fixture 3일 (500줄 eviction).
- 기한: Step 4-α 종료 후 2주 이내.

#### "무회귀 2주" 정량 기준 (M3)

- Step 5 exit criterion (§9) 을 아래로 확장:
  - 2주간 `analyzer-events.jsonl` 의 `suppress`/`slip` 이벤트 0건.
  - `block_commit` 총수 대비 `bridge.dispatch` 총수 ≥ **95%** (드랍 ≤ 5%).
  - `approval_show` → `approval_{confirm,deny,cancel}` 해소율 100% (매달린 모달 0).
  - 사용자 "응답 없음" 보고 0건.

### 13.6 P2 반영 (선택적 — Step 4 진행 중 데이터 기반 결정)

- **A1 — LineCommit.screen 리팩터링**: Step 4-α 에서 RegionTagger 의 snapshot 의존도를
  측정 (매 호출 시 실제 screen 참조 여부 로그). 참조율이 낮으면 Optional 로 변환,
  완전 제거는 별도 티켓.
- **M1 — case-05 multi-boot fixture**: **§13.11.d 에서 목적 재정의**. 단순 "banner
  구분" 이 아니라 `compact_start/compact_complete` 이벤트 dispatch 검증 용도로 승격.
  Step 4-β deliverable 에 포함.
- **D1 — Monotonic offset assertion**: Step 4-α 전에 30분 작업으로 먼저 반영 (P0
  버킷과 함께).

### 13.7 P0 체크리스트 (Step 4-α 착수 blocker) — **§13.11 반영 최종본**

아래가 모두 완료돼야 Step 4-α 개시:

- [x] `bridge/adapter.py` skeleton + 13.1.a public interface. *(4df6f4d)*
- [x] 13.1.b 이벤트 매핑 테이블을 코드로 구현 (기본값).
      `compact_start`/`compact_complete` 행 추가 (§13.11.d). *(4df6f4d)*
- [x] `tools/shadow_diff.py` **집합 비교 기반** + case-04 self-test 통과 (§13.11.a). *(c0ec1c0)*
- [x] `EventClassifier.feed` monotonic offset assertion. *(test_event_classifier.py:95)*
- [ ] ~~RegionTagger `match_distance` 기록~~ — **삭제 (§13.11.b)**. row 는 debug 보조.
- [x] `RegionTagger` 의 `prompt_box_region` 출력 확정 + commit 마다 row range 발행
      (§13.11.c echo suppression 의 전제). *(region_tagger.py:181)*
- [x] `EventClassifier` 가 prompt_box region 내부 commit 을 `user_echo` 로 분류
      (이벤트 미발행) (§13.11.c). *(event_classifier.py:171)*
- [x] Disk precheck + `BRIDGE_PIPE_PANE_MIN_FREE_MB` 기본 500. *(c0eca1a)*
- [x] File identity (inode/size/mtime) 기록 + resume 검증. *(c0eca1a)*
- [x] Tokenizer incomplete escape 카운터 + robust buffering. *(ansi_tokenizer.py:76-78)*
- [x] State atomic write + 2-version. *(b00cb61)*
- [x] Shadow run storage 분리 (analyzer-events.jsonl 별도). *(626f7a4)*
- [x] `compact_start`/`compact_complete` 이벤트 classifier 구현 + 합성 fixture 통과
      (§13.11.d, 부팅 직후/세션 중 양쪽 케이스). *(test_event_classifier.py:148-)*

**상태**: 2026-04-20 기준 P0 체크리스트 전체 완료. Step 4-α (shadow run) 개시 가능.

### 13.8 Step 4-α → 4-β 승격 조건

- P0 체크리스트 ✅ 전부.
- Shadow run 72h 결과 §13.2 통과 조건 충족.
- case-02 회귀 재현 테스트 통과 (합성 fixture 기준).
- BridgeAdapter 이벤트 매핑 테이블 사용자 확인.

### 13.9 Step 4-β → 4-γ 승격 조건

- Case-01/02/03 raw 수집 완료 + 분석기 pass.
- `approval_confirm`/`approval_deny`/`compaction_start`/`limit`/`trust_prompt`/
  `resume_picker` 전부 구현 + 테스트.
- Frozen parser 강제 메커니즘 (pre-commit / CODEOWNERS) 활성.
- Step 4-α 기간 내 analyzer crash 0건 또는 `AdapterConfig.analyzer_crash_fallback`
  정책에 따라 100% 복구.

### 13.10 Critic 재리뷰 시점

Step 4-γ cutover **전** BridgeAdapter spec 확정본으로 5명 critic 재리뷰 1회.
UX/Migration 중점.

### 13.11 사용자 2차 피드백 반영 (2026-04-19 PM)

SUMMARY 를 읽은 사용자가 제기한 4개 재해석을 설계에 반영. **이전 섹션 우선순위
위**에 있음 — 충돌 시 §13.11 이 최종.

#### 13.11.a Shadow-diff 판정 기준 재정의 (C2 재해석)

이전 안의 "±5% 이내" 는 parity 증거로 약함. **parser 자체가 case-04 를 놓치므로**,
둘이 일치한다는 것이 곧 "둘 다 틀림" 일 수 있음. 따라서:

- **집합 포함 기반**:
  - `parser ∩ analyzer` = 공통 (정상).
  - `parser - analyzer` = **regression 후보** — 반드시 0건. 1건이라도 있으면 analyzer
    측 gap, 조사 필수.
  - `analyzer - parser` = **신규 탐지** — case-04 유형. 0건 이상 허용 (오히려 목표).
- `tools/shadow_diff.py` 출력은 세 집합을 별도 섹션으로 리포트. 판정은
  "parser - analyzer = ∅" 단일 불변식.
- 기존 "±5% block_commit 개수" 비교는 **sanity log 로만** (FAIL 트리거 아님).

#### 13.11.b Row drift 측정 폐기 (C4 재해석)

분석 경계는 **`⏺` 블록의 논리적 순서**로 정의됨 (offset stream 순서). tmux row 번호
와 VT row 번호의 불일치는 debug 시 UI 대조에만 불편할 뿐, analyzer 결과 정확도와
무관함.

- `drift_probe.py` / `match_distance` 기록 → **전부 삭제**.
- `LineCommit.row` 는 **"debug 보조, non-authoritative"** 로 docstring 에 명시.
- §13.2 의 "p95 ≤ 5" 조건 삭제.
- Shadow run 중 tmux capture 스냅샷 수집 **불요**.

효과: P0 체크리스트 2개 항목 (drift probe 구현 + match_distance 계측) 삭감 →
Step 4-α 착수 기간 **~1~2일 단축**.

#### 13.11.c Region-based user-echo 억제 (C3 사용자 재제안 수용)

사용자 제안: *"승인 키워드는 반드시 명령어 뒤에 옴. 시작점(명령어) + 요구사항 문구를
같이 묶어 보는 방향성"*. 이는 byte-offset 윈도우 방식보다 구조적으로 더 강함.

**설계 변경**:

1. `RegionTagger` 는 commit 마다 현재 `prompt_box_region` 의 row range 를 발행
   (이미 `_is_chrome()` 에 일부 로직 있음, 확장).
2. `EventClassifier` 의 entry 에서 **commit row ∈ prompt_box_region** 이면 즉시
   `region_tag="prompt_box"` 로 분류 → **이벤트 미발행** (user_echo).
3. Approval 판정은 3-조건 AND **강화**:
   - (a) 직전에 `⏺` 명령 블록 commit 이 buffer 에 있음.
   - (b) 아래쪽 envelope (±8 row) 에 `Do you want to...` 류 question text.
   - (c) `❯ 1. Yes` + `3. No|Skip` choice 라인.
   → 명령 앵커 (a) 가 없으면 echo 일 가능성이 높아 **approval 판정 기각**.
4. byte-offset 기반 `(register_offset, +K)` 윈도우는 **tie-breaker 로 강등**. 어떤
   edge case 에서만 보조로 쓰이는지 Step 4-β 에서 관찰.

**신규 스키마**:
```json
{"offset": N, "t": "user_prompt", "region": "prompt_box",
 "text": "...", "source": "bridge.send_input"}
{"offset": N, "t": "user_echo", "region": "prompt_box",
 "row": R, "text": "..."}   // 내부 분류, Telegram 미전송
```

**테스트**:
- case-02 는 "prompt_box region 안 commit 이 이벤트화되지 않는다" 를 검증하는
  합성 fixture 로 재작성. 명령어 + question text + prompt box paste 3 조합.
- Approval 3-조건 AND 의 단위 테스트 (a/b/c 각각 누락 시 거부).

#### 13.11.d Compact 이벤트 1급 승격 (multi-boot HIGH 재해석)

사용자 관찰:
> "화면이 압축되면 welcome banner 가 나온다. 이건 Claude Code TUI 가 뿌리는
> 예상 가능한 동작. 구분 못해서 이슈가 아니라, **compact 이벤트 자체를 처리**해서
> 사용자에게 '압축 중 → 완료' 로 알려주면 되는 문제."

**설계 변경**:

1. §4 Feature Parity Gap 의 `compaction_start` 를 **`compact_start` +
   `compact_complete`** 2-이벤트로 분할 + 1급 이벤트로 승격.
2. `EventClassifier` 는 다음 시퀀스를 패턴 매치:
   - trigger: "Compacting..." / "Compacting conversation..." 류 텍스트 또는
     `/compact` 명령 echo 직후 + 일정 버스트.
   - completion: welcome banner (`Claude Code v...`) + `※ Tip:` 조합이 새로
     나타나고 안정화됨.
3. `BridgeAdapter` 매핑 테이블 (§13.1.b) 추가 행:
   - `compact_start` → Telegram "🗜️ 컨텍스트 압축 중..." (새 메시지, message_id 저장).
   - `compact_complete` → 위 메시지 edit → "✅ 압축 완료".
   - 그 다음의 `block_commit(kind="response")` 은 **정상 처리** (이전엔 banner 오인 가능).
4. **세션 부팅 직후 compact 도 동일 처리**. 기존 parser 의 "초기/중간 구분" 로직
   (`has_content_above`) 은 **완전 폐기**. VTScreen paradigm 에서는 compact 는
   boot 시점이든 중간이든 같은 이벤트.
5. `case-05` multi-boot fixture 의 목적 재정의: "banner 를 숨기는 테스트" ❌
   → "compact_start/complete 이벤트가 올바르게 발행되고, 직후 block_commit 이
   정상 dispatch 되는지" ✅. **Step 4-β deliverable 로 승격** (이전 P2 에서).

**영향**:
- §4 Gap 테이블 사실상 갱신 (아래 13.11.e 참조).
- `_response_region` 경계 C 문제 해소 — 구분이 필요한 게 아니라 구분 자체가 정식
  이벤트로 표면화됨.
- `parser.py` 의 `has_content_above` 및 welcome 구분 잔재 **완전 제거 가능**.

#### 13.11.e §4 Gap 테이블 갱신

아래 행을 §4 테이블의 기존 `compaction_start` 행과 치환:

| 이벤트 | 상태 | 포팅 출처 | 난이도 |
|--------|------|-----------|--------|
| `compact_start` | 미구현 | parser.py compact 배너 regex 이관 + burst detection | 하 |
| `compact_complete` | 미구현 | welcome banner + `※ Tip:` 안정화 검출 (신규) | 중 |
| `user_echo` (내부) | 미구현 | prompt_box region 기반 (§13.11.c) | 하 |

기타:
- `user_prompt` 는 그대로 (emit 시점은 13.3 유지, suppression 방식만 13.11.c 로 교체).
- `compaction_start` (단수) 항목은 **폐기**.

#### 13.11.f 반영 영향 요약

| 섹션 | 변경 |
|------|------|
| §13.2 | "±%" → "집합 포함", drift 조건 삭제 |
| §13.3 | byte-offset 방식 → §13.11.c 로 대체 |
| §13.5 M1 | case-05 purpose 재정의 + Step 4-β deliverable 승격 |
| §13.7 | drift 항목 2개 삭제, echo-region / compact 항목 3개 추가 |
| §4 | `compaction_start` → `compact_start`/`compact_complete`, `user_echo` 신규 |
| §13.1.b 매핑 | compact 2행 + user_echo 무시 행 추가 |

Step 4-α 착수 기간 영향: drift 작업 삭감 (~1–2일) vs compact·echo 구현 추가
(~1–2일) → **net zero**, 대신 설계 정합성 향상.
