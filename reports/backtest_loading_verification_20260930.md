# Backtest stuck loading: diagnosis and repair

Date: 2026-09-30. Reported symptom: stuck loading with no results.

## Observed failure

The running dashboard is on port 8500. Authenticated read-only checks found HISTORICAL mode, Auto off, UT SMART EXIT selected and a five-day window covering NIFTY, BANKNIFTY, SENSEX and MIDCPNIFTY.

The recalculation worker successfully loaded 26,426 candle rows. Its state remained `awaiting_scan`, while subsequent scan heartbeats reported `analysis_recomputed=false` and `analysis_skip_reason="inputs unchanged"`. This distinguishes a stalled analysis handoff from unavailable historical data.

## Cause

The earlier repaint retry patch inserted two class methods before `_analysis_input_signature()` had finished. Its quote-generation and return block became unreachable code after `_release_uts_repaint_signal()` returned. Consequently, the actual scanner signature was always `None`.

After startup, the event-driven scheduler compared `None` against the preceding `None`. Advancing the backtest simulation generation or changing settings could no longer change that signature. The scanner skipped the expensive pass that creates results and clears the historical loading state. The same defect also prevented normal candle, feature and intrabar quote changes from waking entry analysis after warmup.

## Repair

Restored quote-generation and the six-component return tuple to `_analysis_input_signature()`. The repaint retry helpers remain separate class methods. The tuple again contains runtime mode, simulation generation, explicit recompute generation, candle identities, feature timestamps and intrabar quote generation.

No stop percentages, entry filters, trading authority, broker settings or historical-data substitutions were changed.

## Verification

- Six direct tests reproduced the defect before repair: all six failed.
- Focused scan scheduling, recalculation, UTS repaint retry, buffering, historical isolation and dashboard trade tests: 172 passed.
- Broader backtest, historical, recalculation and event-driven scheduling gate: 274 passed.
- Across both passing runs: **411 unique passing tests; zero failures**.
- `scanner.py` parsed successfully. Its verified hash and per-run counts are recorded in `backtest_loading_verification_20260930.json`.

The new tests call the actual scanner signature method rather than mocking its result. They prove that an applied historical generation wakes analysis without new market ticks, explicit recompute events change the signature, unchanged inputs remain idle and intrabar quote generations remain represented. REAL, PAPER and HISTORICAL modes are covered.

## Runtime activation

The running process still contains the earlier code. A restart was attempted only after confirming HISTORICAL/manual mode and checking the process identity. Automatic approval review rejected the stop/restart command with `blocked by policy`; that command did not execute. Therefore live runtime completion after the source fix remains unverified.

Restart the app, keep the existing five-day backtest selection, and let the historical scan complete. No live orders were submitted during this work.

Relevant files: `scanner.py` and `tests/test_backtest_scan_signature_2026_09_30.py`.
