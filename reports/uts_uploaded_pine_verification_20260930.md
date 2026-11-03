# UT SMART EXIT: uploaded Pine parity audit

Audit date: 30 September 2026, Asia/Kolkata. Reference: the user's uploaded `UT Bot + Smart Peak Exit v6` source and NIFTY 15-minute TradingView image.

## Result

The running scanner was using a different, previously tuned indicator profile. On the same observed NIFTY candles, that profile produces BUY at 10:00 and SELL at 14:15. The uploaded defaults reproduce all four supplied chart labels: BUY at 09:15, SELL at 10:30, BUY at 11:45 and SELL at 13:30. The 11:45 long also produces SMART EXIT at 13:15, matching the orange marker.

The repaired code uses the uploaded defaults across 1-, 5- and 15-minute timeframes. Existing explicit per-timeframe overrides remain available. Dashboard fallbacks match the backend defaults.

| Input | Previous 15m profile | Uploaded profile now used by default |
|---|---:|---:|
| UT sensitivity | 2.0 | 1.0 |
| ATR period | 14 | 10 |
| Heikin Ashi source | Enabled | Disabled |
| Smart Exit arm | 2.5 ATR | 0.80 ATR |
| Peak retracement | 0.70 ATR | 0.45 ATR |
| Emergency retracement | 2.5 ATR | 1.10 ATR |
| Momentum confirmations | 4 | 2 |

Previously, 5m used key 4.5/ATR 21 and 1m used key 15/ATR 21. Their defaults now also use key 1/ATR 10.

## Marker time and execution time

The uploaded script defaults to candle-close confirmation. Its chart labels sit on the candle's opening timestamp; the signal is available only when that candle closes. This is TradingView's documented [bar-state behavior](https://www.tradingview.com/pine-script-docs/concepts/bar-states/) and [time convention](https://www.tradingview.com/pine-script-docs/concepts/time/).

| Chart label | Signal first confirmed |
|---|---|
| BUY 09:15 | 09:30 |
| SELL 10:30 | 10:45 |
| BUY 11:45 | 12:00 |
| SMART EXIT 13:15 | 13:30 |
| SELL 13:30 | 13:45 |

Tests check the last available candle at 09:29:59 and 09:30:00. The 09:15 BUY appears at 09:30 even without a subsequent candle arriving. A UTC evaluation clock produces the same result as IST.

## Formula and chart repairs

- EMA now follows the first-source seed in TradingView's documented [v6 reference](https://www.tradingview.com/pine-script-reference/v6/#fun_ta.ema). RMA retains its separate SMA seed. Missing values do not reset an unfinished RMA seed or become fictitious zero RSI moves.
- RSI highest/lowest lookbacks ignore missing values, as the Pine functions do.
- The indicator's entry peak/trough uses the signal candle high/low, exactly as the uploaded script assigns it. Actual option profit protection still uses observed premium prices after entry; an index-candle extreme cannot manufacture an option profit peak.
- Rejection-wick ratios respect the symbol's minimum tick, with an index default and an explicit data-frame override.
- Pine's UT direction remains separate from the indicator's Smart Exit trade state. The UT trail colour follows source versus trail; a Smart Exit does not turn the still-directional UT trail grey.
- Forming trail/EMA previews update without creating confirmed entries or exits. Future candles cannot supply preview values or the displayed current price.
- Chart refresh checks settings, closed-candle corrections and the candle-close clock, rather than relying only on the newest timestamp. Chart calculation uses a private engine and does not overwrite scanner or multi-timeframe results used for orders.

## Final-session feed anomaly

The FYERS history contains an extra final-session path that does not match the supplied chart. On 29 September, 14 one-minute candles from 15:15 through 15:28 are frozen at 22,683.75; the 15:29 candle jumps to 22,716.20. Their 15-minute aggregate creates a BUY at 15:15 on the previous day, suppressing the next morning's 09:15 BUY. Today's final-session minute path is also mostly frozen.

The existing 15:15 exclusion remains in place for this feed, and the UTS chart uses the same filtered candles as its signal calculation. This is a feed correction outside the Pine formula. It is not evidence that every provider's legitimate final-session candle should be discarded. The raw fixture retains these vendor bars so tests can prove the discrepancy rather than hide it.

The four expected entries reproduce with 137 runtime bars, 300 merged bars and 1,153 stored bars. Insufficient warmup does not explain today's difference. Today's runtime and stored OHLC values agree; the earlier warmup snapshot has one 1.25-point open difference on 23 September.

## Option execution policy

The indicator and real option execution deliberately differ. The user's 5% stop for cascade/alignment, filtered bursts and strong trends, 10% regular stop, 15% choppy 15m stop and guarded one-time 5m extension remain active. Native extremely-weak-setup checks, freshness, position/capital limits, broker protection and squareoff also remain. Consequently, matching every chart marker does not imply taking every marker as an option trade or holding an option until the indicator exit.

## Verification and activation

All 7,558 tests passed: 7,554 in the default suite and the four normally excluded synthetic integration checks run separately. There were no failures, errors or skipped tests. The default run retained one pre-existing warning about an unrelated speed test returning a dictionary.

The focused UTS/chart gate passed 523 tests. An independent final chart review passed 26 tests. These sets overlap the full suite and are not added again to its total. Python syntax checks passed for the four changed modules; Node accepted the dashboard settings JavaScript. All 12 recorded source hashes remained unchanged during the final run.

The companion JSON records test totals, source hashes, fresh-process parameters and activation status. Tests cover documented formula cases, the real NIFTY fixture, candle-close timing, chart cache changes and preservation of the option-risk rules.

The audit's read-only runtime snapshot shows PID 31600 imported the older build at 14:34:35 IST. Source changes require a system restart to activate in that process. No live order, Auto setting, mode or broker stop was changed by this audit.

This verifies the uploaded formula's documented behavior and today's supplied signal labels. It does not establish unconditional 100% identity across different vendor OHLC histories, undisclosed TradingView input overrides, intrabar tick paths or executed option fills. No TradingView compiler/export comparison was available.

Evidence: `uts_nifty_chart_data_audit_20260930.md`, `uts_nifty_pine_parameter_trace_20260930.json`, `uts_nifty_fyers_late_session_parity_20260930.json`, the saved raw NIFTY fixture, and the final test XML files.
