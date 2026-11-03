# UT/ATR signal and trade-management investigation — 10 September 2026

The strongest diagnosis is that several parts of this system disagree about the market, the meaning of confidence, and the profit an option has actually earned. There is evidence of excessive or misapplied filtering, but there is also evidence of overconfident admission. Loosening every gate would not address both failures.

This investigation inspected the current entry, intelligence, and active-trade paths; read persisted configuration; queried SQLite in read-only mode; inspected September 9–10 logs and existing evaluation reports; and ran isolated calls to scoring/intelligence functions. No product code, trading configuration, orders, or positions were changed. Existing modified workspace files were left intact. The accompanying evidence JSON contains selected database observations and report summaries; the probes JSON contains reproducible function outputs and configuration values. Database P&L is the app's recorded result, not a new independent broker reconciliation.

**1. Highest-priority defect: synthetic option peaks can activate real profit exits.**

`Scanner._trade_candle_extreme_price` reconstructs an option price from a favorable underlying-index candle extreme using entry premium plus underlying movement multiplied by the entry delta. The active-trade updater takes the maximum of this estimate, actual premium ticks, the current mark, and the previous peak. The resulting single `peak_pnl` drives profit protection. An estimated price can therefore override the observed option-price history even when premium quotes are available.

Evidence: [peak estimator](/C:/Users/sagnik/Desktop/rp46-3/scanner.py:22941), [delta projection](/C:/Users/sagnik/Desktop/rp46-3/scanner.py:22992), [peak merge](/C:/Users/sagnik/Desktop/rp46-3/scanner.py:35235), [retention exit](/C:/Users/sagnik/Desktop/rp46-3/scanner.py:35568). The estimator excludes the entry-minute candle, so the evidence does **not** support blaming a pre-entry extreme in this helper.

Today's MIDCPNIFTY 14600 PE provides a concrete example:

| Event | App evidence |
|---|---|
| Entry | 09:15:37 trade timestamp; actual premium fill ₹181.50; 120 quantity |
| Last market-to-market event | 09:16:00.403, premium ₹178.85, recorded peak profit ₹6 |
| Exit decision | 09:16:00.733: `Smart Profit Lock Hit ... Peak ₹623, Current ₹-318` |
| Final fill | 09:16:05.910, premium ₹178.85, recorded net P&L −₹398 |
| Protective stop | Initial/current stop approximately ₹172.22, unchanged |

The final peak is ₹623.1168, exactly consistent with an underlying move from 14575.95 to 14565.15 multiplied by −0.4808 delta and 120 quantity. The saved event history does not establish every tick or the exact intermediate candle snapshot, but the jump from ₹6 to ₹623, the production code, and the non-tick-sized peak strongly support synthetic-peak contamination as the mechanism. This was a profit-retention exit while the option was losing money, well before its protective stop.

A fixed delta projection is not an observed premium. Option value also changes with volatility, time, and changing delta; see [OIC's explanation of option Greeks](https://www.optionseducation.org/advancedconcepts/understanding-options-greeks). Use verified contract-premium observations for executable option-profit peaks. Keep an underlying-based estimate as separate context.

**2. Trailing-stop breathing room does not govern all exits.**

The active path applies option breathing room to a proposed stop and can defer a trend-option profit lock. Later, a separate P&L retention branch can close the trade without consulting that breathing-room decision or the same trend deferral. The code explicitly describes the backstop as covering situations where a stop was delayed or clamped.

Evidence: [breathing-room clamp](/C:/Users/sagnik/Desktop/rp46-3/scanner.py:35330), [trend deferral](/C:/Users/sagnik/Desktop/rp46-3/scanner.py:35352), [independent retention exit](/C:/Users/sagnik/Desktop/rp46-3/scanner.py:35568), [backstop rationale](/C:/Users/sagnik/Desktop/rp46-3/scanner.py:17038).

This is an architectural conflict, even if parts were intentionally written as protection. Widening the ATR stop alone cannot fix exits triggered elsewhere. A production-helper probe also demonstrated the reachable sequence: actual premium profit ₹240, projected peak ₹1200, proposed lock ₹186.60 clamped back to ₹168, followed by the independent retention condition still evaluating as breached.

The read-only ledger snapshot contains eight filled, closed LIVE trades from September 9–10, with recorded net P&L −₹2299. Five `SMART_PROFIT_LOCK` exits total −₹125; three `EARLY_LOSS_CUT` exits total −₹2174. One additional margin-preflight rejection was excluded, along with all BACKTEST rows. This is a small sample, but it shows which exit families actually dominated these trades.

The right repair is a common exit decision that records which rule won and why, using the same verified price, peak, risk basis, and trade state. It is not simply a larger trailing distance.

**3. Grades amplify correlated trend evidence and are not calibrated win probabilities.**

For RANGING, VOLATILE, and UNKNOWN, the intelligence aggregator multiplies the entire aggregate contribution by 0.15. The grade then applies a separate regime penalty. Trend/ADX evidence also contributes through the technical pillar, regime classification, multi-timeframe confluence, and intelligence. These are overlapping transformations of price, not independent confirmations.

Evidence: [aggregate regime multiplier](/C:/Users/sagnik/Desktop/rp46-3/intelligence/intelligence_aggregator.py:445), [application to whole score](/C:/Users/sagnik/Desktop/rp46-3/intelligence/intelligence_aggregator.py:597), [separate grade regime pillar](/C:/Users/sagnik/Desktop/rp46-3/engine/signal_manager.py:217), [logistic confidence](/C:/Users/sagnik/Desktop/rp46-3/engine/signal_manager.py:283).

An isolated illustrative probe held BUY direction, ADX 25, confluence 0.55, and one-ATR stop distance fixed. With aligned intelligence 0.5 and TRENDING_UP, it graded A+ at 94.97%. Applying the 0.15 intelligence scaling and RANGING produced B+ at 57.44%; UNKNOWN produced C at 33.18%. This is a sensitivity demonstration, not a measured probability of success.

The current settings collect calibration outcomes but have `ut_confidence_calibration_live_apply_enabled=False`. Therefore a displayed 96% is a transformed heuristic score, not evidence that 96 of 100 similar option trades win. [Calibration switch](/C:/Users/sagnik/Desktop/rp46-3/intelligence/confidence_calibration.py:148).

This explains how an early turn can be rejected while its regime label lags, and the same direction can later receive a very high grade once lagging indicators agree. Fixing this requires evaluating each evidence family's incremental contribution and calibrating against actual after-cost trade outcomes. Merely reducing the B+ threshold changes trade count without establishing better selection.

**4. The market engine computes a changing session phase, but important consumers still use the original phase.**

`SessionContextAnalyzer` emits `effective_day_type` and `effective_bias` when rolling market mood contradicts the opening classification. The aggregator's top-down adjustment still reads `day_type`, `bias`, and `confidence`. The range-day regime guard also checks the original `day_type`.

Evidence: [effective phase production](/C:/Users/sagnik/Desktop/rp46-3/intelligence/session_context.py:118), [original phase consumption](/C:/Users/sagnik/Desktop/rp46-3/intelligence/intelligence_aggregator.py:688), [range-day guard](/C:/Users/sagnik/Desktop/rp46-3/intelligence/regime_detector.py:724).

An isolated call with opening TREND_DAY_UP/BULLISH at 0.8 and effective TREND_DAY_DOWN/BEARISH at 0.85 still returned a **+0.096 bullish** top-down adjustment. Different modules can consequently vote from different descriptions of the same current market.

Regime smoothing is also enabled with a five-observation window. Smoothing is a deliberate noise/latency tradeoff, not automatically a bug, but it compounds the stale-phase problem. The richer regime conviction vector explicitly remains for shadow/reporting use while live gates use the hard label. [Conviction-vector contract](/C:/Users/sagnik/Desktop/rp46-3/intelligence/regime_detector.py:429).

The primary repair is a consistent, timestamped effective phase for current decisions, while retaining the opening phase as a distinct prior.

**5. Some market-intelligence votes are price proxies presented as available flow evidence.**

The order-flow fallback classifies buyers/sellers from where the last five closes sit within their candles. It sets `available=True` even if all five volumes are zero. The isolated zero-volume probe returned BULLISH/BUYERS, zero buy volume, zero sell volume, and ratio 1.0.

Evidence: [close-position classification](/C:/Users/sagnik/Desktop/rp46-3/intelligence/order_flow.py:54), [availability flag](/C:/Users/sagnik/Desktop/rp46-3/intelligence/order_flow.py:93), [aggregator admission of flow](/C:/Users/sagnik/Desktop/rp46-3/intelligence/intelligence_aggregator.py:473).

The module honestly labels this `ohlcv_close_position` and says true tick footprint is unavailable. The issue is that downstream voting can still treat this as an available additional source. A directional price proxy may be useful, but it does not independently establish aggressive buying or selling. The same price move can receive technical, regime, and flow votes.

September 10 logs also show all four cash-index volume feeds falling back to futures volume, with comparable cash-volume ratio gates abstaining. Some abstention is correct data handling. Do not loosen freshness or comparable-volume requirements merely because they reduce entries. Instead distinguish actual option/depth evidence, futures activity, and price-derived proxies in scoring and exits.

**6. Expected-move admission is disconnected from the actual target and option payoff.**

The forecast is a rolling quantile of historical directional maximum favorable excursion over earlier bars. Its inputs do not condition on this UT signal, the current regime, ADX, spread, or option-price behavior. Final admission independently checks geometric reward/risk ≥1.5 and whether the forecast clears a small index-specific floor. It does not establish the probability of reaching the actual target before the stop, or expected option profit after costs.

Evidence: [forecast inputs](/C:/Users/sagnik/Desktop/rp46-3/engine/entry_policy.py:335), [historical MFE calculation](/C:/Users/sagnik/Desktop/rp46-3/engine/entry_policy.py:480), [independent RR check](/C:/Users/sagnik/Desktop/rp46-3/engine/entry_policy.py:708), [independent expected-move check](/C:/Users/sagnik/Desktop/rp46-3/engine/entry_policy.py:774).

For the September 10 MIDCPNIFTY session candidate, forecast q25 was 0.0434%, passing its 0.031% floor; the underlying target distance was approximately 0.3553%, with a 0.1421% stop. Its reported 83.1% probability referred to crossing the small floor, not hitting the trade's target. All 16 recent saved session candidates examined had forecast q25 below their target distance. A conservative q25 being below a target is not itself a failed strategy; it demonstrates that these two passing checks do not validate the claimed payoff.

The old 0.15%/0.25% expected-move floors have already been replaced. Current hard floors are NIFTY 0.023%, BANKNIFTY 0.029%, SENSEX 0.022%, MIDCPNIFTY 0.031%. Blaming the removed values for current rejections would be incorrect. A common underlying-stop ceiling of 0.15% remains. Any proposal to change it needs segment-specific replay, not a guess.

**7. Current strategy and timing differ from a simple UT-only interpretation.**

Effective configuration is `scanner_engine=quad_prime`, with option execution. It combines UT, EMA, Supertrend, and consensus sources. A result from the full pipeline cannot automatically be attributed to the UT algorithm alone.

The effective 5-minute and 15-minute stabilization windows are both 30 seconds, despite current Python defaults of 15 seconds. Persisted settings override those defaults. Changing a default in code may therefore leave live behavior unchanged.

A fixed 30-second persistence check also does not confirm a five- or fifteen-minute candle close. It reduces transient signals but preserves an intrabar strategy. [TradingView's repainting documentation](https://www.tradingview.com/pine-script-docs/concepts/repainting/) explains why live, developing bars can differ from historical bars. ATR measures volatility rather than independently establishing direction; see [TradingView's ATR documentation](https://www.tradingview.com/support/solutions/43000501823-average-true-range-atr/). The UT crossing rule provides the directional event, and that event's entry timing needs explicit validation.

**8. Existing evaluation artifacts do not yet establish that filtering adds an edge.**

The September 9 30-day report shows +₹17,276 over 69 simulated trades and 1,215 `Quality gate blocked` records. The existing exit-policy report covers 239 matched trades across 34 replayable sessions. Its last 20 sessions contain 159 trades: baseline net −₹31,911.69, candidate net −₹30,127.49, candidate profit factor 0.6116. The candidate is explicitly uncertified, and no calibration segment is ready.

These are different samples and execution models. The negative replay is not a new measurement of today's complete live strategy, and the profitable report is not proof of live profitability. The exit replay uses ACCEPTED rtracker records, selected runtime helpers, and minute OHLC; it does not execute the entire live exit stack. It can fall back to premium-proximity contract matching when exact identity is unavailable. [Replay entry loading](/C:/Users/sagnik/Desktop/rp46-3/scripts/backtest_exit_lifecycle.py:167), [contract matching](/C:/Users/sagnik/Desktop/rp46-3/scripts/backtest_exit_lifecycle.py:205), [simplified runner simulation](/C:/Users/sagnik/Desktop/rp46-3/scripts/backtest_exit_lifecycle.py:259).

There is also a validation-policy weakness: certification checks improvement over baseline, but does not explicitly require positive holdout expectancy or profit factor above one. A less negative policy can satisfy the relative-performance checks. The current report still fails other checks, so it has not actually been certified. [Certification conditions](/C:/Users/sagnik/Desktop/rp46-3/scripts/evaluate_exit_policy_walk_forward.py:129).

Rejection totals are not unique live raw-signal counts: persisted activity can include duplicate explanatory messages and replay records. September 9's rejection report scored only 94 of 179 rejected clusters; 42 were positive after 30 minutes in underlying points. That is neither an after-cost option P&L comparison nor proof that all filters should be removed.

**9. Confirmed entry defect: market location reads a stale price and a multi-session “day range.”**

The live location gate clips its 1-minute candle frame to `candidate.signal_timestamp`. That timestamp is the source bar's starting label, not the current entry-decision time. It then overwrites the candidate's `entry_spot` with that old close. For a fifteen-minute bar, up to fourteen subsequent one-minute rows can be omitted. The same method computes `day_high` and `day_low` over the entire retained frame, including previous sessions, because there is no current-session filter.

Evidence: [bar-start clipping and entry mutation](/C:/Users/sagnik/Desktop/rp46-3/scanner.py:26695), [unscoped day extrema](/C:/Users/sagnik/Desktop/rp46-3/scanner.py:26717), [active Quad Prime caller](/C:/Users/sagnik/Desktop/rp46-3/scanner.py:27521), [multi-session candle retention](/C:/Users/sagnik/Desktop/rp46-3/data/candle_builder.py:359).

An independent in-memory execution of the original extracted method reproduced both effects. The same fifteen-minute LONG blocked with the 09:45 price of 98.50, but allowed when evaluated at the latest available 09:59 price of 102.70. A current-day five-minute LONG blocked as a 99% day-position chase; adding previous-day candles changed it to ALLOW. These are synthetic causal probes, not claims about a particular historical trade.

The gate is skipped in HISTORICAL mode ([bypass](/C:/Users/sagnik/Desktop/rp46-3/scanner.py:26675)), so a historical result cannot expose this live gate's incorrect decisions. Use an explicit decision-time cutoff and calculate current-session location separately from multi-session indicator warmup.

**10. Candidate processing can lose a valid alternative before admission.**

The raw collection loop remembers every candidate identity as consumed. It then selects primarily by signal timestamp, using score only within the same timestamp. Canonical quality admission occurs afterward. A newer weak candidate can therefore become the selected candidate and fail admission while an older, still-eligible candidate has already been consumed.

Evidence: [identity consumption](/C:/Users/sagnik/Desktop/rp46-3/engine/signal_processor.py:5799), [timestamp-first selection](/C:/Users/sagnik/Desktop/rp46-3/engine/signal_processor.py:6087), [later quality admission](/C:/Users/sagnik/Desktop/rp46-3/scanner.py:27469).

There are real sibling/cascade mechanisms, so it would be wrong to claim the system always considers only one candidate. However, cascade siblings require a cascade winner, while rare siblings require a strong winner and a fast 1m plus structural 5m/15m pairing. They do not provide a general fallback for ordinary competing 5m/15m candidates after a weak winner fails. [Rare sibling requirements](/C:/Users/sagnik/Desktop/rp46-3/engine/signal_processor.py:1846), [pair restriction](/C:/Users/sagnik/Desktop/rp46-3/engine/signal_processor.py:1878), [cascade requirement](/C:/Users/sagnik/Desktop/rp46-3/engine/signal_processor.py:1942).

This control-flow failure is established; its frequency and missed P&L have not been measured. Candidate eligibility should precede final selection, or alternatives should remain available until admission is resolved.

**Recommended order of work.**

1. Establish an observed option-premium peak as the authority for profit protection and capture peak provenance at every exit.
2. Reconcile trailing, breathing room, runner state, loss cuts, and retention into one explicit exit decision. A defer/hold from one layer must have a documented precedence against another layer's close.
3. Align current session/regime inputs and decision clocks; separate unavailable flow from price proxies; count correlated sources as one evidence family where appropriate.
4. Correct the candidate selection and location defects above before adjusting grading thresholds.
5. Replay the same timestamped raw UT events and exact option contracts through a minimal baseline, current filters, and individual filter removals. Then compare exits on the same accepted entries. Include actual or defensible bid/ask execution, charges, slippage, entry delay, adverse excursion, favorable excursion, and exit order.
6. Keep only filters and exit rules that improve held-out after-cost expectancy and drawdown. Report four-index/timeframe/regime coverage and uncertainty; do not select settings from the best aggregate backtest or the highest win rate.

The evidence supports fixing decision consistency and measurement before tuning floors. It does not yet establish that raw UT alone is profitable, that every blocked signal deserved entry, or how much profit each individual correction would recover.
