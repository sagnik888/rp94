# S1 recheck at 15:15 IST

## Corrected conclusion

**Follow-up resolution:** the later candle-source audit reproduced the reported 10:45-versus-12:00 difference. The manual chart uses Heikin Ashi; the scanner had used standard candles. This integration is now fixed. See `s1_candle_source_fix_20260924.md`. The cutoff findings below remain valid for the earlier standard-candle engine.

The user's objection is correct for the current day's earlier signals. The prior comparison of saved NIFTY close 23,446.80 against screenshot close 23,424.90 used an out-of-window saved candle and did **not** demonstrate why signals differed during the trading window. That causal explanation is withdrawn.

This recheck finds no dependency of the tested pre-cutoff S1 signals on 23 September candles that finish after 15:15. It does not establish full TradingView parity or prove that every candle before 15:15 matches TradingView.

## Reproduction

Replayed the four original symbol/timeframe cases with unchanged indicator settings and history before 23 September. Candle timestamps denote their opening time, so the last completed 5m candle is 15:10–15:15, and the last completed 15m candle is 15:00–15:15. The 15:15-labeled candle is excluded because it finishes after the cutoff.

Each case was run three ways:

1. Full available saved history through the end of 23 September.
2. Input cut at the last candle that closes by 15:15, including the 5m dependency for 15m charts.
3. Full input with all post-cutoff OHLCV replaced by extreme prices/volume, including the 5m dependency.

Compared every feature array, position and stop through the cutoff, plus entry and exit events available by that time. An exit attached later to an earlier entry was excluded from the earlier event snapshot.

**All four cases passed both comparisons.** There were zero feature differences and no changes to pre-cutoff positions, stops, entries or exits.

| Case | Last completed bar opens | Close at 15:15 | Raw S1 state at 15:15 |
| --- | --- | ---: | --- |
| NIFTY 5m | 15:10 | 23,431.65 | Flat |
| NIFTY 15m | 15:00 | 23,431.65 | Flat |
| BANKNIFTY 15m | 15:00 | 56,542.85 | Flat |
| MIDCPNIFTY 5m | 15:10 | 14,562.80 | Flat |

These values are **cutoff snapshots**, not comparisons to unverified screenshot timestamps.

## NIFTY events in the corrected local replay

- 5m: BUY on the 12:00 candle, available at 12:05; momentum exit on 13:30, available at 13:35.
- 15m: BUY on the 12:00 candle, available at 12:15; momentum exit on 15:00, available at 15:15.

The NIFTY 15m local exit therefore belongs to the trading window and is not caused by the saved 15:15–15:30 bar. The screenshot's later LONG dashboard does not by itself reveal the exact reference state at the cutoff. Timestamped TradingView signal events or the chart at 15:15 are required to establish the first divergent bar.

## Session ownership verified in code

Freshly loaded settings report:

- `ut_session_start`: 09:15.
- Normal no-entry cutoff: 14:55 for 15m, 15:01 for 5m/1m; separate qualified late-entry handling exists.
- `ut_force_exit_time`: 15:15.
- `ut_session_end`: 15:25.

`TradeManager.check_session_end` reads the 15:15 force-exit setting; the scanner invokes it when positions/orphans exist. Historical S1 exit handling also caps the session at the configured square-off. These are code/configuration checks, not confirmation that any particular live broker position was filled or closed.

The raw S1 indicator has `session_rules=False` to preserve the supplied Pine rules. It can calculate chart signals on later available candles; the chart display is not restricted to the execution cutoff. Thus a post-cutoff raw indicator marker or state is not equivalent to a system trade. The actual underlying session data begins at 09:15, although the application may be running earlier.

Past days' late candles remain in indicator history because that is what the manual full-session indicator sees. This test excludes later candles only on the comparison day. Removing every prior day's post-15:15 candles would alter moving averages, RSI and indicator trade-state history and would be a separate strategy change.

## Changes and evidence

Focused validation: **75 tests passed**, with one existing FYERS dependency deprecation warning. The four saved-data cutoff probes also passed independently.

No strategy or session setting was changed during this recheck. The earlier report now carries a correction.

- `s1_cutoff_verification_20260924.py`: reproducible offline probe, no broker orders.
- `s1_cutoff_verification_20260924.json`: complete features at the cutoff, timestamped events, configuration, engine hash and comparison results.
- `s1_cutoff_tests_20260924.xml`: focused engine, candle-cutoff and session-exit test results.

The remaining parity question must be investigated at matching timestamps **within** the trading window. Post-cutoff last-price differences are no longer being used as its explanation.
