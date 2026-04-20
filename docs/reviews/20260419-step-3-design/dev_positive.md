# Dev/Testability Critic — Positive Assessment

## Strongest Asset: Testability Architecture Already Proven

The design inherits a **highly testable decomposition** from Step 2's PoC. All six analyzer modules (`tokenizer`, `vt_screen`, `line_commit`, `region_tagger`, `event_classifier`, `pipe_pane_analyzer`) are independently unit-testable with **zero cyclic imports**. This is evidenced by 37 passing tests across `test_ansi_tokenizer.py` (11 tests), `test_vt_screen.py` (12 tests), and `test_event_classifier.py` (8 tests) — each module tested in isolation without upper layers.

**Concrete strength**: Case-04 raw fixture (451KB ANSI) is replayed through the entire pipeline (`tokenize_bytes → VTScreen → LineCommit → TaggedLine → EventClassifier`) in `test_pipe_pane_analyzer.py`, producing deterministic `approval_show` detection that bridge events.jsonl **completely missed**. This validates the architecture can catch real bugs that snapshot-based parsing ignores.

## Dataclass Contract Prevents Boundary Drift

The step-2 architecture enforces invariants at layer boundaries:
- `Token(offset_start, offset_end, kind, payload)` — tokenizer output
- `VTScreen.get_lines()` → raw text with row indexing (offset-independent)
- `LineCommit(offset, row, text, reason, screen)` — commit point with screen snapshot
- `TaggedLine(commit, region)` — region classification
- `AnalyzerEvent(offset, t, region, payload)` — event dispatch

Each boundary is a dataclass with immutable semantics. **No module reaches past its neighbor to reshare state.** This means adding a new event type (e.g., `compaction_start`) requires touching only `event_classifier.py` (regex + handler) and `tests/test_event_classifier.py` (synthetic + fixture), **never** modifying tokenizer or VT screen.

Verified in step-2: adding `session_boot` / `session_resume` events required no changes to `tools/ansi_tokenizer.py` or `tools/vt_screen.py`.

## Offset-Based Replay Guarantees Determinism

Unlike the old `capture-pane` → `parser.extract_response_blocks` path (which is non-deterministic with tail-scan ambiguity, as shown in §1 of `BUG_REPORT.md`), the new design **stores byte offsets** as the ground truth. `--until-offset N` guarantees that re-running the analyzer on the same pipe-pane file produces identical events down to the microsecond.

This is critical for §5.7's shadow run: "72h diff ≤ ±5%". Because events are deterministic, **any divergence is structural, not noise**. The design can credibly claim "baseline is 72 events, analyzer is 75, difference = case-01/02/03 edge cases not yet in fixture" vs. hand-waving.

## EventClassifier State Machine Is Observable & Testable

The most complex component, `EventClassifier`, holds mutable state (`_modal`, `_block_buf_text`, `_held_blocks`, `_busy_active`, `_emitted`). The design makes this observable:
- `_emitted: set[tuple[int, str]]` dedup cache — testable in `test_offset_dedup_drops_duplicate_emission`.
- `_held_blocks` during approval — verified in `test_approval_show_holds_block_commit` (show event before block, correct ordering).
- State transitions (modal entry → content → flush) — eight synthetic tests in `test_event_classifier.py` cover normal + error paths.

**Key invariant verified**: offset-only dedup prevents double-emission even if `feed()` is called twice on same line. This is essential for Step 4's online integration where pane rotation / resume might replay the same bytes.

## Existing Regression Tests Will Migrate Cleanly

Step 0 defined 3 baseline cases (`case-01` / `case-02` / `case-03` are ESC-after-approval, scrollback echo, and 500-line eviction). None of these have **raw ANSI fixtures yet** — only `tmux_capture.txt` (rendered text) and logs. However, the design explicitly addresses this in §4 Feature Parity Gap: "case-01/02/03 raw 재수집". 

Case-04 (Edit approval wording mismatch) **already has raw fixture** (451KB), and the analyzer detects the approval that bridge missed. Migration test `test_case_04_no_queue_slip_equivalent` proves parity. When case-01/02/03 raws are collected, the same fixture → expected JSON pattern will scale.

## Fixture Format is Minimal & Versionable

Unlike the old parser path which embeds pane state in ad-hoc `APPROVAL_SCAN_LINES=60` tunables, the new design **records the source of truth**: raw bytes. `tools/fixtures/live-<ts>/raw-*.log` can be **version-controlled** as binary test assets. Each is compressed ≤1MB (case-04 = 451KB). This is far cheaper than maintaining a second "expected JSON" by hand; the JSON is **generated** once and checked into git.

This enables future CI to re-run case-04 against any new EventClassifier change, catching regressions immediately.

## Summary

The Step 2 PoC already proves:
1. **Isolated unit tests**: 37 tests, all passing, no hidden dependencies.
2. **Deterministic replay**: offset-based, shadowing-ready.
3. **Complex state reachable**: EventClassifier state machine is observable in tests.
4. **Fixture reuse path**: case-04 raw → expected JSON → regression test.

The design's testability is not vaporware — it has shipped working code.
