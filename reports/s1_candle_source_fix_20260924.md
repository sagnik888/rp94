# S1 signal timing — root cause and repair

## Reproduced and fixed

For **23 September, NIFTY 15m**, the same saved market candles and same S1 settings produce:

| Candle source used by S1 | Last BUY candle |
| --- | --- |
| Standard OHLC — previous scanner behavior | 12:00 |
| Heikin Ashi — supplied manual chart reference | **10:45** |

The missing integration was **the chart's candle type**. The screenshot's OHLC and internal entry values closely match Heikin Ashi computed from the stored candles. The unmodified Pine indicator reads the chart's visible OHLC, so a Heikin Ashi chart changes its body, trend, score and exit calculations. [TradingView's official explanation](https://www.tradingview.com/pine-script-docs/faq/general/#get-real-ohlc-price-on-a-heikin-ashi-chart)

The previous scanner's dedicated S1 path always used standard candles. Its calculation also did not preserve the requested chart type for the 5m RSI and 15m EMA inputs. Pine requests can inherit the ticker's chart modifiers. [TradingView context documentation](https://www.tradingview.com/pine-script-docs/concepts/other-timeframes-and-data/#custom-contexts)

This cause reproduces the reported **75-minute difference without changing any signal threshold**. My earlier focus on end-of-day price differences missed this distinction; it did not explain the user's example.

## Why 10:45 was rejected

Using the exact same raw input history:

| Gate at 10:45 | Previous standard source | Corrected Heikin Ashi source | Rule |
| --- | ---: | ---: | --- |
| ADX | 19.25 | 21.03 | At least 20 |
| Bull score | 59.56 | 78.89 | At least 60 before loss-streak adjustments |
| Candle body | 1.35 points | 25.12 points | At least 15% of ATR |
| Required body | 4.66 points | 5.47 points | Depends on selected candle ATR |
| Close position in range | 34.11% | 76.28% | Above 50% for BUY |

The standard candle fails four gates at 10:45 and first qualifies at 12:00. The Heikin Ashi replay qualifies at 10:45 and remains LONG through the 15:15 cutoff, matching the reported reference trade direction. The application chart builder also emits a BUY marker at epoch `1790140500` — **10:45 IST**.

Screenshot cross-check: the manual internal entry is approximately 23,390.89; independently computed HA entry is 23,391.07. At the last continuous-session NIFTY 15m candle, screenshot O/C 23,434.55 / 23,424.55 closely matches computed HA O/C 23,434.65 / 23,424.44. Raw O/C for that candle is 23,422.55 / 23,431.65. NIFTY 5m and BANKNIFTY 15m screenshots also closely match HA candle/RSI values.

## Implemented

- Added `s1_candle_type`, default `heikin_ashi` for this reference. `standard` remains selectable in S1 settings. The global UT candle setting is unchanged.
- Current-timeframe indicators, scoring, patterns and Pine trade state use the selected candle type.
- Native 5m and 15m **raw** candles are converted separately before requested-timeframe indicators run. Aggregating lower-timeframe HA candles is not used as a substitute.
- Multi-timeframe cache keys include source selection and native candle dependencies. Chart fallback also fetches both required timeframes.
- Dashboard candles use the selected source, with a visible `S1 · Heikin Ashi` badge. Source changes request a complete chart and clear stale markers.
- S1 setting changes trigger the existing full recalculation path.
- Stored market candles remain raw. Published signal/order reference prices, fixed SL and target prices use raw market close. Separate `s1_chart_*` fields preserve synthetic indicator levels for display.
- The optional close-entry evaluation mode uses real market candles for entries, stop hits and fills while evaluating S1 gates on the selected chart type.
- Your signal, risk, trailing, momentum and circuit-breaker settings remain unchanged. Forecast removal remains in effect.

## Verification

- **201 focused tests passed**, covering core math, HA conversion, independent requested timeframes, settings validation, raw order prices, cache invalidation, dashboard JavaScript and nearby scanner paths. One existing FYERS dependency deprecation warning.
- Offline scanner replay checked NIFTY, BANKNIFTY, MIDCPNIFTY and SENSEX on **1m, 5m and 15m**. All 12 scanner results matched the selected-source reference computation.
- Full history and the scanner's 900-bar window agreed on 23 September's entry/exit timestamps in all 12 cases.
- The actual chart payload contains the NIFTY 15m **10:45 BUY** marker.

Evidence: `s1_candle_source_verification_20260924.py`, `s1_candle_source_verification_20260924.json`, `s1_candle_source_tests_20260924.xml`.

## Practical limits and loading

The 10:45 marker identifies the candle opening at 10:45. In closed-candle mode its final signal is known when that candle closes at 11:00. This fix does not backdate an executable fill to the synthetic HA open.

The reproduced timing defect is fixed. This is not a claim that every indicator number or every historical marker is identical across all data feeds. Small source-price differences remain, and the manual 15m ADX is not numerically identical in the available history.

Restart the scanner to load the Python changes and refresh the dashboard. Verify S1 candle source displays **Heikin Ashi**. This audit did not restart the running trading process or submit orders.
