# Dev/Testability Critic — Risk Assessment

## Top 3 Blocking Gaps (Rank by Severity)

### 1. **Feature Parity Gap §4: case-02 user-echo 구분은 미정의 복잡도** (3–5 days)

**What will fail in practice**:
The design documents "case-02 user-echo 판별" as "send_input log 상관으로 구조 해결 (tail-scan 대비 상위 호환)" (§2, 매트릭스) but provides **zero implementation path** in §4. The old parser's `is_approval()` tail-60 heuristic is designed to filter out user-echoed scrollback text that looks like approval modals. When case-02 is replayed (user pastes approval-like text into pane), the analyzer must distinguish:
- Real modal: Claude Code rendered it with divider above, `❯ 1. Yes` selection, footer
- Scrollback echo: User pasted the same text; divider + footer are **absent** in tail 60

The current `RegionTagger._row_in_modal_envelope()` call does check screen snapshot, but **only if row indices align**. If Claude Code renders the modal on rows 10–15 and the echo-paste lands on rows 25–30 (different part of pane), the envelope check will miss it, causing **false positive `approval_show`** events.

**Concrete example from case-02**: 
- Offset 12000–12200: real modal appears on rows 5–8
- Offset 50000–50200: user pastes "Do you want to proceed? ❯ 1. Yes / 3. No" on rows 25–27
- RegionTagger tags both as `modal_overlay` because `_looks_like_modal_body()` regex matches
- EventClassifier emits two `approval_show` events (should be one)
- Bridge sends approval reminder to user **twice** → UX failure

**What needs to change**:
1. **EventClassifier must track send_input log timestamps** to suppress echoed blocks:
   - When bridge calls `tmux.send_input("text")`, record the text + offset + time.
   - When EventClassifier sees a block whose text **exactly matches** a recent send_input, mark it as `suppressed_user_echo` instead of `block_commit`.
   - This requires **BridgeAdapter (Step 4-γ)** to pre-populate EventClassifier with a `user_send_history: dict[int, str]` lookup (offset → sent text).

2. **Add fixture test** for case-02 before Step 4-β:
   - Collect case-02 raw bytes (scrollback echo scenario).
   - Manually write expected JSON showing `approval_show(method=real)` + `suppressed_user_echo(text=..., reason=case02)`.
   - Implement and pass.

**Effort**: 
- Implement send_input → EventClassifier integration: **2 days** (adds state to classifier, needs careful offset matching).
- Case-02 raw fixture + fixture test: **1 day**.
- Integration test with BridgeAdapter: **1 day**.
- **Total: 3–5 days, blocks Step 4-γ dispatch cutover.**

---

### 2. **RegionTagger Row Envelope Detection ±Row Drift Not Validated** (2–3 days)

**What will fail in practice**:
The design claims "shadow run 으로 실측 diff. 실패 시 row 탐색 범위를 ±12 로 확대" (§6 risks). However, the current `RegionTagger._row_in_modal_envelope()` function (assumed in `tools/region_tagger.py` line 108 but not shown in last read) is **not yet validated against real tmux output**.

VT 200-row memory model differs from tmux's **actual terminal** in two ways:
1. **tmux rotate** — When pane scrolls past 200 rows, older rows are evicted from history. VT screen's row 0 may not correspond to actual tmux row 0.
2. **Claude Code rendering lag** — Between token parsing and on-screen appearance, render lag can cause row indices to shift. If modal is being drawn while response text is still appending, envelope detection may compare wrong rows.

**Concrete failure mode**:
- Case-03 (500-line eviction scenario): pane has scrolled 300 lines, VT screen row tracking wraps. Modal appears and `_row_in_modal_envelope()` compares `commit.row=150` against screen snapshot where modal is actually on rows 10–15 (post-eviction indices). Detection fails, EventClassifier does **not** emit `approval_show`, response blocks leak out of buffering. Telegram gets garbled output.

**What needs to change**:
1. **Offline validation before shadow run**: 
   - Run case-03 raw through analyzer, check that `approval_show` is detected.
   - If it's not, incrementally widen the row search range (currently ±0, try ±5, ±12, ±24).
   - Document the final range in code comments with a link to case-03 analysis.

2. **Online safeguard during shadow run**:
   - Add a `debug_row_drift` flag to EventClassifier that logs `(modal_row_in_commit, modal_row_in_screen, match_distance)` for every approval detection.
   - If `match_distance > 0`, it means heuristic row shifting was needed — record it.
   - If all 72h of shadow run has `match_distance=0`, row detection is solid. If spikes appear, increase search range.

**Effort**:
- Collect case-03 raw fixture: **1 day** (or assume it's already in Step 1 live logs).
- Write `_row_in_modal_envelope()` if missing, add range search: **1 day**.
- Add debug logging + validation: **0.5 days**.
- **Total: 2–3 days before Step 4-α can start.**

---

### 3. **Shadow Run Diff Tooling Not Implemented; "±5%" is Aspirational** (2–3 days)

**What will fail in practice**:
The design specifies (§4-α):
> 분석기 출력을 별도 파일 `~/.claude-bridge/panes/<session>/analyzer-events.jsonl` 로 기록. 기존 events.jsonl 과 **이벤트 diff** 를 주기적 (1h) 계산.
> - `block_commit` 개수가 기존 `response_block` dump 와 ±5% 이내.

**But there is no code to compute this diff.** When Step 4-α starts, the operator will:
1. Start bridge with `BRIDGE_DISPATCH_SOURCE=parser` (old path).
2. Simultaneously launch `pipe_pane_analyzer.py` in shadow mode.
3. After 1h, run... what? `diff` command? A Python script?

**The tooling doesn't exist.** This creates two risks:
1. **Operator error**: "Did I compare correctly?" Manual JSON diffing of 1000+ events is error-prone.
2. **Silent divergence**: If the tool is missing, the operator might declare "72h passed" without actually checking diff, and Step 4-γ will deploy a broken analyzer to production.

**Concrete example of what can go wrong**:
- case-04 produces 390 `block_commit` events (known from Step 2).
- If analyzer regresses and only emits 300 blocks, diff should flag "±23%".
- But without automated diff, the operator might not notice until production users complain.

**What needs to change**:
Implement `tools/shadow_diff.py` with:
1. Load `analyzer-events.jsonl` (analyzer output from shadow run).
2. Load `events.jsonl` (parser output from bridge).
3. For each pair of offset ranges (e.g., 0–100KB, 100–200KB, etc.), count:
   - Old `is_approval=1` events → expect analyzer `approval_show` superset.
   - Old `flush_block` count → expect analyzer `block_commit` ±5%.
   - Old `busy_spinner` count → expect analyzer `busy_enter` ±10%.
4. Output markdown report with pass/fail per metric.
5. Integrate into `bridge/core.py` dump flush to call it hourly.

**Effort**:
- Implement shadow_diff.py: **1.5 days** (JSON parsing + metric computation).
- Integration into core.py periodic task: **0.5 days**.
- Validation with case-04 fixture: **1 day**.
- **Total: 2–3 days before Step 4-α monitoring can trust its own results.**

---

## Secondary Gaps (Track for Step 4)

### 4. **EventClassifier Missing Events: `approval_confirm` / `approval_deny` Require Send_Input Correlation** (1–2 days)

The design lists these as "미구현" (§4, gap table). Current code only emits `approval_cancel(method=reason)`. To distinguish confirm vs. deny:
- Confirm: user pressed Enter while `❯` was on "Yes" row.
- Deny: user pressed Enter while `❯` was on "No" row, OR user pressed Escape.

**Current limitation**: EventClassifier has no access to `send_input` log. It can't know if the text that followed the approval question was an Enter keypress.

**What needs to change**: 
- BridgeAdapter (Step 4-γ) must pass `send_input` history to EventClassifier.
- EventClassifier checks: if offset X has `approval_cancel` and offset X+ε (within ≤500ms) has `send_input=Enter`, then retrospectively upgrade to `approval_confirm` or `approval_deny` based on prior `❯` position.

**Effort**: Part of case-02 implementation (§1 above). Add to same 3–5 day estimate.

---

### 5. **Fixture Maintenance: Raw Bytes Don't Self-Update; case-01/02/03 Must Be Manually Collected** (1 week, async)

The design defers "case-01/02/03 raw 재수집" to Step 4-β:
> 자연 재현되지 않으면 재현 스크립트 작성 (bridge 자체 리플레이 경로 권장, tmux replay 보다 안정).

**Problem**: If Step 1 live logs didn't naturally capture case-01/02/03, **synthetic fixture creation** is required:
- case-01 (ESC-after-approval): Manually run Claude Code, trigger Edit approval, press Escape, send new message. Record raw. **4h lab time**.
- case-02 (scrollback echo): Manually paste approval-like text into pane, trigger approval in parallel. **6h lab time**.
- case-03 (500-line eviction): Create a 500-line response somehow (long bash output, deep recursion trace). **8h lab time**.

If a re-run framework is not built, this becomes manual tmux/telnet debugging — **not acceptable** for regression testing.

**What needs to change**:
- Add `tools/case_reproducer.py` script that:
  - Runs bridge with mocked telnet/telegram inputs (from YAML scenario file).
  - Records resulting pipe-pane raw.
  - Stops after 30s of stable state.
- Write YAML for case-01, case-02, case-03.
- Validate each produces expected symptoms in analyzer events.

**Effort**: **1 week, but can happen in parallel with Step 4-α shadow run.**

---

### 6. **Regression Test Coverage When parser.py Is Deleted (Step 5)** 

When parser.py is removed, these tests will fail if they exist:
- `tests/test_parser.py::test_extract_response_blocks_*` — **will not exist yet** (no parser tests found). This is **good news** — the parser has no test harness, so deletion has zero test breakage.
- `tests/test_*.py` importing from `bridge.parser` — **grep finds zero imports** (already migrated to analyzer).

**Verification needed**: Before Step 5 PR, run:
```bash
grep -r "from bridge.parser import" tests/ bridge/
grep -r "from bridge.stream_queue import" tests/ bridge/
```

If both are empty, deletion is safe.

**Effort**: **1h verification, 2h PR review** (Step 5 is just deletion, no logic changes).

---

### 7. **State Machine Determinism: Event Ordering Under Concurrent Region Changes** (0.5 days)

The EventClassifier assumes **sequential TaggedLine input** (§3). If, due to a bug in RegionTagger, one byte offset produces two region assignments (e.g., first marked `modal_overlay`, then `content`), the state machine might emit duplicate or out-of-order events.

**Mitigation**: Add an assertion in EventClassifier.feed():
```python
assert tl.commit.offset > self._last_offset, f"non-monotonic offset {tl.commit.offset} <= {self._last_offset}"
```
This catches any upstream ordering violation early, preventing hard-to-debug state machine bugs.

**Effort**: **30 min** (add assertion + test).

---

## Summary of Implementation Order

**Critical path for Step 4-α readiness (must be done)**:
1. Case-02 send_input integration + fixture test: **3–5 days**
2. Case-03 row envelope validation + debug logging: **2–3 days**
3. Shadow run diff tooling: **2–3 days**
4. Sequential offset assertion in EventClassifier: **0.5 days**

**Critical path total: 7.5–11.5 days** before Step 4-α can safely start.

**Can happen in parallel (Step 4-α background)**:
- Case-01/02/03 reproducer scripts: **1 week**
- Parser deletion verification + cleanup: **1h + 2h review**

**Decision gate for proceeding**:
- [ ] All case-01/02/03 raws collected or reproducer scripts validated.
- [ ] Shadow diff tooling tested on case-04.
- [ ] RegionTagger row range finalized post-validation.
- [ ] BridgeAdapter skeleton exists (even if incomplete).

If any of these are missing at Step 4-α start, the "72h shadow run" becomes a debugging session rather than a validation run.
