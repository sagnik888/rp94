# Historical context repair verification — 13 September 2026

The reproduced context time-boundary failures have been repaired. These checks validate causal inputs and integration, not profitability.

## Implemented

- Historical 1/5/15-minute intelligence, session levels, breadth and hourly anchors include only candles whose closing time is at or before the decision clock. Aware timestamps convert to IST correctly; invalid cutoffs cannot retain the complete future frame.
- Hourly anchors align to the 09:15 exchange open, require twelve source 5-minute candles, and exclude unfinished hours. Before the first complete session candle, yesterday's session classification is not presented as today's.
- Historical confluence and the 15-minute alignment gate wait for source candle closure before allowing a flip to vote. Signal timestamps in the raw MTF result remain opening timestamps; replay callers pass availability timestamps.
- VIX minute observations become available after their timestamped minute, daily midnight rows become available at 15:30, and intraday observations older than five minutes abstain. There is no invented 15.0 reading for missing history. Session-open VIX cannot come from a future query or a later cached query. The numeric compatibility value zero grants no low-VIX grade bonus.
- Historical analysis receives recorded same-session option chains at or before the decision, at most 120 seconds old. It never requests today's option chain as a historical replacement. Original chain expiry and quality remain attached to the archived observation. Missing chains stay explicitly unavailable.
- Replay gets isolated regime, PCR and OI objects, retaining deterministic closed-bar regime warmup. Future live detector history is neither consumed nor overwritten.
- `_get_historical_intel_context_at(instrument, ts, timeframe)` exposes the complete as-of result for entry and post-entry management. It includes `historical_data_availability`; `_historical_intel_coverage` counts actual available inputs by index/timeframe and simulation.

## Verification

`python -m pytest tests/test_historical_context_causality_20260913.py tests/test_intel_context_fixes.py tests/test_recalculation_queue.py tests/test_requested_open_and_reversal_policy_2026_09_06.py tests/test_remaining_audit_repairs_2026_09_11.py -q --disable-warnings --tb=short --junitxml=reports/historical_context_repairs_20260913.xml`

**136 passed, 853 warnings, 4.12 seconds.** Includes thirty new cases: all four indices and all three timeframes, unavailable-bar mutation invariance, timezone equivalence, VIX daily/minute availability, stale/missing observations, out-of-order regime history, historical chain boundaries and full-context caching. Two earlier assertions expecting unfinished candles were corrected to expect only closed candles.

Frozen pre-edit files: `reports/replay_baseline_source_20260913/scanner.py` and `engine/signal_manager.py` within that folder. Method-level patch: `reports/historical_context_repairs_20260913.diff`.

## Integration limits

The available older VIX history and derivative archives cannot supply observations never recorded. Missing data is disclosed and abstains; none is fabricated. A fresh chronological replay and out-of-sample performance assessment remain the parent task's responsibility. Live behavior affected here is closed-bar session/anchor/breadth handling and unavailable-VIX grading; historical timing fixes do not establish a forecast of live profit or loss.
