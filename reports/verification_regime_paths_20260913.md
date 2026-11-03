# Regime, session and historical intelligence verification — 13 September 2026

Read-only verification of the current production methods. No broker calls, orders, settings changes or production edits were made. The companion Python probe extracts the production methods from `scanner.py`; the JSON records its outputs.

## Verification result

`python -m pytest tests/test_remaining_audit_repairs_2026_09_11.py tests/test_session_context.py tests/test_per_timeframe_regime_reaches_the_candidate_2026_08_13.py -q --disable-warnings --tb=short`

**114 passed, 228 warnings, 12.32 seconds.** These test cases confirm the intended repairs under their fixtures. They do not prove profitability or the completeness of a historical replay.

## Reproduced remaining boundaries

1. **Historical session context can read the future part of a higher-timeframe candle.** `scanner.py:13901` clips by bar start time. At a 10:02 cutoff it includes the complete 10:00–10:05 historical candle. Changing only that candle's high changed the returned session high from 101 to 150, and session range from 2% to 51%. The historical 1-minute signal route calls `_compute_historical_intel_at` with the signal bar's start at `engine/signal_processor.py:4005`; this builds the 5-minute context at `scanner.py:31330`. The own-timeframe close can legitimately be used for a confirmed signal, but a complete 5-minute bar whose close is still in the future cannot. Prior repairs clipped later bar timestamps; they did not reconstruct partial higher-timeframe bars from observations available by the decision time.

2. **Historical VIX can use a later same-day observation or same-day daily close.** `_get_historical_vix` (`scanner.py:31015`) returns the earliest same-day value if none is at or before the requested time. The probe requested 09:20 with only a 15:30 value of 28 and received 28. A daily close recorded at midnight was likewise returned at 09:20. `_vix_percentile` explicitly recognizes that midnight daily rows contain session closes, but the historical value resolver does not apply that rule. These values feed historical grading and volatility sizing. Current local `vix_history.json` ends on 1 August 2026, so a replay of later dates instead uses the fixed 15.0 fallback; that would not validate actual historical volatility conditions.

3. **An aware cutoff can fail open in the hourly anchor.** `_hourly_anchor_regime` (`scanner.py:13708`) catches an aware/naive timestamp comparison failure and keeps the unsliced frame. On the same naive cache, a naive 8 September 10:02 cutoff passed 385 source bars through 8 September, while its IST-aware equivalent passed all 675 source bars and a final hourly label of 11 September 16:00. CandleBuilder normally normalizes stored indices to naive IST. The session-context clipping fix does not cover this separate helper.

These probes establish representation and time-boundary defects. They do **not** establish that any one defect caused the user's reported loss or that removing it will improve P&L.

## Why the 30-day backtest does not certify live derivative integration

`_compute_historical_intel_at` explicitly uses `options_chain=None` (`scanner.py:31356`). Historical option OI, PCR and chain Greeks are therefore unavailable to the main historical intelligence calculation. The derivative interval store is in memory, has restart warmup and has no historical derivative-data loader. Missing historical observations cannot be reconstructed by running current OHLCV candles through the same aggregator.

The new `derivative_participation` results contain independently identified futures/options intervals, but current production references only publish and forward these fields (`intelligence/intelligence_aggregator.py:232`, `scanner.py:18014`, `scanner.py:29850`). No filtering, score, trailing or exit function consumes their computed interval values. This was disclosed in the 12 September repair report as contextual analysis without added directional votes. It falls short of interpreting the original user request as an independently validated derivative-sensitive entry-to-exit strategy.

The distinction is necessary for diagnosis: the passing repair tests validate narrower correctness contracts; the negative historical result tests an OHLCV-oriented replay with important omitted inputs and the remaining time-boundary defects above.
