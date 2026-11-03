# S1 integration audit — 23 September 2026

## Conclusion

**Resolved root cause, 24 September:** subsequent paired replay identified a missing Heikin Ashi candle source. With identical stored data and settings, standard candles generate the reported NIFTY 15m BUY at 12:00, while Heikin Ashi generates it at 10:45. This is now fixed in the engine and chart. See `s1_candle_source_fix_20260924.md`; the older unresolved-parity statements below describe the earlier investigation stage.

**Correction after the 24 September cutoff investigation:** the last-close comparison below uses end-of-session candles after the operator's 15:15 square-off. It does not establish the cause of an earlier signal mismatch. Removing or drastically altering all 23 September candles that close after 15:15 leaves all S1 features, positions, stops and events through that cutoff unchanged in the four tested cases. See `s1_cutoff_verification_20260924.md` and its JSON evidence. Earlier indicator snapshots and the chart's end-of-day LONG/WAITING state are not matched-timestamp proof of an in-session feed mismatch.

The scanner contained real integration gaps; matching settings alone was insufficient. The identified calculation, data dependency, state publication and chart rendering defects have been repaired. Exact bar-for-bar TradingView parity is **not established**: the available FYERS candles differ from the supplied screenshots. Do not tune thresholds to compensate for different candle inputs.

The attachment was treated as reference source code, not as instructions. It was not modified. At the user's explicit request, S1 future forecast calculations, metadata, settings and entry filtering were removed. The Pine entry/exit state rules remain.

## Repairs

| Area | Defect | Change |
| --- | --- | --- |
| RSI / ATR / DMI / ADX | Wilder smoothing started from the first observation | RMA now starts from the required valid-value SMA; DMI warmup respects the missing previous bar |
| 15m scoring | The 5m RSI term selected the last historical intrabar | Uses the first intrabar's previous RSI, including timezone normalization |
| Missing 5m input | 15m processing could substitute chart RSI | Requires actual 5m candles; chart rebuild supplies them from cache/candle store/disk |
| Cached signals | 5m revisions and S1 settings could leave 15m output cached | Cache identity includes these dependencies |
| Engine output | S1 inherited UT indicator/stop arrays and could return UT output after failure | Dedicated S1 result/state path; errors propagate instead of publishing another engine's signals |
| Confirmed mode | Forming candle entries/exits could leak into confirmed results | Replay publishes only through the confirmed candle |
| Circuit breaker | Defaults were 6 losses / 3 pause bars | Defaults now match 3 losses / 5 pause bars for every index |
| Live levels | Default stop mode differed from the requested fixed percentage | Default is fixed 0.20% from signal close |
| Chart exits | Exit changes could be absent or remain cached without price changes | Timestamped S1 exit markers, updated chart identity and marker refresh |
| Chart indicators | Only three overlay series; incremental transport removed overlay data | Five S1 overlays (EMA9/21/50/200 and VWAP), refreshed through broadcasts |
| Flat state | Missing stops could bridge old trades or leave stale lines | Explicit whitespace points clear the stop while flat |
| Browser cache | Existing asset versions could retain old UI | Chart/settings script versions bumped |

TradingView documents that lower-timeframe `lookahead_on` returns the first intrabar historically but the latest available intrabar in realtime. The implementation deliberately uses stable historical mapping. A realtime TradingView chart can therefore differ before reloading. [Official documentation](https://www.tradingview.com/pine-script-docs/concepts/other-timeframes-and-data/)

## Effective reference settings

- Score 60; bull/bear gap 20; ADX 20; 15m EMA alignment enabled.
- Cooldown 0; under the one-entry-per-bar state machine, 0 and 1 both permit the next bar.
- Fixed SL1 0.20%; T1/T2/T3 2/3/4 R.
- Trailing enabled; ATR offset 1.8; profit lock at T1 enabled; existing trail start 1.5 R retained.
- Momentum confirmation 3; loss streak 3; pause 5.
- Existing Pine state SL2 0.40% retained. Separate live SL2 remains disabled.

The replay artifact records settings loaded by a fresh process, rather than assuming source defaults. A running scanner must reload the updated Python code; refresh the dashboard for the new JavaScript. No runtime restart or broker order was performed during this audit.

## Candle evidence and remaining limits

| Chart | Screenshot last close | Saved candle last close |
| --- | ---: | ---: |
| NIFTY 5m | 23,424.90 | 23,446.80 |
| NIFTY 15m | 23,424.55 | 23,446.80 |
| BANKNIFTY 15m | 56,545.60 | 56,548.90 |
| MIDCPNIFTY 5m | 14,572.25 | 14,572.25 |

MIDCPNIFTY's matching close does not mean matching candles: its saved last open is 14,562.35 versus 14,559.95 in the screenshot. Each examined 5m feed also contains a flat, zero-volume 15:20 bar. These observations require a matched TradingView OHLCV export to resolve; screenshots cannot provide the full indicator input history.

After repair, local full-history replay ends flat for all four cases. The NIFTY 15m screenshot shows LONG, so that discrepancy remains explicitly unresolved. The other three screenshots show WAITING, which agrees only at the final-state level; it does not prove their earlier entry/exit markers match.

The Pine source itself books chart trades at the signal candle's **open**, although the signal uses its close. That convention was preserved for S1 state parity as requested. Actual signal prices and live stop levels use the close. The original Pine label also mixes close-based entry/SL text with targets computed from its open-based trade state. Separate `s1_chart_*` metadata now exposes those reference levels. This is an indicator-state comparison, not proof of achievable fills or reported returns.

State replay begins flat at the first supplied bar. Full history and the 900-bar window agreed on today's trades in these four local samples; this is not a guarantee for every earlier open position or loss streak. Original Pine decorative tables, pattern labels, zones and ribbons are not fully reproduced by the application chart; the core scoring, pattern contributions, gates, stops, targets, trailing and exits are the audited scope. Pattern group switches are not all exposed individually in the dashboard.

## Verification artifacts

Final focused run: **186 passed** in 30.47 seconds. One existing FYERS dependency deprecation warning; no test failures. The reviewed diff also passed whitespace/error checks.

- `s1_parity_verification_20260923.xml`: focused engine, settings, chart, scanner integration and adjacent tests; includes execution of chart JavaScript under Node.
- `s1_data_evidence_20260923.json`: pre-repair local candle and indicator evidence.
- `s1_parity_replay_20260923.json`: repaired engine hash, candle hashes, loaded settings, full versus 900-bar comparisons, and today's trade events.
- `s1_parity_replay_20260923.py`: reproducible offline replay; no broker connection or orders.

To prove exact parity, the remaining required evidence is a TradingView OHLCV export for the same symbols, sessions, timeframes and history, together with the reference indicator's settings and bar timestamps for mismatched markers.
