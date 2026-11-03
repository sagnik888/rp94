# Workflow repair verification — 23 September 2026

## Changes

This patch addresses the code defects reproduced in the signal and exit audit. It does not retune grade floors or remove strategy filters.

- The selected signal's own timeframe intelligence, regime and quality metadata now feed routing, vetoes and candidate construction. Live 15-minute agreement is checked against current engine state at admission, including during startup; historical replay keeps its recorded causal evidence.
- Buffered signals save a versioned grading-input snapshot and rerun the canonical grader at maturity with current intelligence, regime, MTF confluence and session context. The replay reapplies nonlinear caps and calibration. Grade, reasons and machine-readable evidence refresh together, and the snapshot survives warm-memory serialization.
- Risky-location exceptions use direction-aligned numeric intelligence, not stale explanatory prose.
- Live option profit locks and major-win guards compare a verified option premium with the premium floor that armed them, and record the observed premium. Structural trailing stops retain separate authority.
- The scanner passes its early-loss-cut decision to TradeManager for that update only; stale cut-R state cannot carry across cycles. Adaptive HOLD preserves the active stop and cannot replace it with a conflicting stop proposed in the same pass. Hard-stop and observed contract-touch checks remain active.

## Verification

Focused regression run:

`venv/Scripts/python.exe -m pytest tests/test_selected_signal_context_2026_09_23.py tests/test_workflow_admission_evidence_2026_09_23.py tests/test_workflow_buffer_consistency_2026_09_23.py tests/test_workflow_exit_authority_2026_09_23.py tests/test_stabilization_regrades_on_fresh_intel_2026_09_18.py tests/test_manual_mode_exit.py tests/test_runner_mode_fixed_trail.py -q --disable-warnings --tb=short`

Re-audit rerun result: **187 passed, 2 warnings, zero failures** in 37.81 seconds. JUnit output: `workflow_fix_reaudit_20260923.xml`. `git diff --check` completed without whitespace errors (Git emitted line-ending notices for existing modified files).

A repository-wide test run was started but ended before producing its JUnit report, so it is not counted as a pass. The configured pytest run excludes integration tests that require live broker/data services. The audit also found only five sessions of shadow outcomes; these fixes correct demonstrated logic inconsistencies but do not establish profitable strategy performance.

No settings, live trades, orders or runtime process were changed by this repair.

