**Verification of the reported 30-day loss — 13 September 2026**

**Verdict: the original end-to-end requirement is not fully implemented or verified.** The earlier repairs fix specific representation, freshness and accounting boundaries. The previous broad completion claim exceeded that evidence. This verification reproduced remaining decision-integration and historical-timing defects. Passing the repair suite did not establish that the system understands derivative participation, manages trades correctly under every historical condition, or improves returns.

This was a read-only product investigation. It captured the running app through its local read endpoints, inspected current code and local data, ran focused tests, and created isolated production-method probes. It did not send orders, change broker state, alter settings, retune thresholds, or rerun the live app's backtest.

**The exact run was recovered.** Simulation `1789237249558`, HISTORICAL mode, 30-day setting, 96 unique closed option trades, 22 winners and 74 losers, net **−₹60,093.48**, PF **0.20617**. Trades cover 21 sessions from 13 August through 11 September 2026. The replay accounting reports no duplicate rows. Price differences and quantities independently reconcile to **−₹52,413.48 before charges**, with **₹7,680 charges**, matching the displayed loss. The preference is `OPT`, and futures shadow is disabled; zero futures trades is expected under those settings.

| Timeframe | Trades | Wins / losses | Net P&L |
|---|---:|---:|---:|
| 1 minute | 4 | 0 / 4 | −₹3,028.45 |
| 5 minutes | 60 | 17 / 43 | −₹29,144.80 |
| 15 minutes | 32 | 5 / 27 | −₹27,920.23 |

94 of the 96 trades were graded A or A+, and those rows lost **₹57,654.08**. Only two were B+. The run uses `quad_prime`: UT Bot generated 64 trades and −₹45,124.54; Supertrend 21 and −₹12,001.08; EMA 11 and −₹2,967.86. It is therefore not a UT-only experiment.

The 56 `SL HIT` exits account for **−₹67,889.74**. Eight early loss cuts add −₹6,312.00. Average winning trade is ₹709.42 and average losing trade is −₹1,022.98. These are recorded outcomes, not proof that widening stops, lowering grade gates, or disabling profit locks would improve the strategy. The diagnostic rejection counts include 1,146 one-minute burst rejections and 970 quality rejections, but they do not include reliable counterfactual outcomes for every rejected opportunity.

**1. P1 — Futures/options interval features are collected but disconnected from decisions.**

The new `derivative_participation` payload is generated in [intelligence_aggregator.py](C:/Users/sagnik/Desktop/rp46-3/intelligence/intelligence_aggregator.py:232) and copied into candidate/trade contexts in [scanner.py](C:/Users/sagnik/Desktop/rp46-3/scanner.py:18014). Current production references do not use its computed interval values to change aggregate scores, regime, entry quality, tracking, trailing or exits.

Independent production-method probes across **all 12 index × timeframe combinations** held index candles constant and changed eligible exchange-timed futures/options evidence from price +100, OI +10,000 and 10× participation to price −100, OI −10,000 and 0.1× participation. Aggregate, regime and entry quality stayed identical. This is a missing consumer, not merely an excessively strict threshold. A prior test with “forwards them to live consumers” in its name verifies forwarding only.

An older, separate one-minute futures-rate path does affect pre-candle predictor confidence. It is not the new 1/5/15-minute service, does not cover complete options participation, and does not establish derivative-aware exit management. It would be inaccurate to say every futures input is unused.

**2. P1 — Historical trades can start before their source signal exists.**

[UT Bot](C:/Users/sagnik/Desktop/rp46-3/engine/ut_bot_core.py:476) calculates the signal from the completed bar's close and keeps that bar's original timestamp at line 563. Candle timestamps represent bar starts; local resampling explicitly uses left labels. [Historical option replay](C:/Users/sagnik/Desktop/rp46-3/engine/signal_processor.py:4406) passes this timestamp straight into its simulator. The [simulator](C:/Users/sagnik/Desktop/rp46-3/engine/signal_processor.py:2855) buys the first one-minute close at or after that timestamp, then starts checking the following minute. No signal-timeframe close offset is applied at this boundary.

**42 of the 96 recorded trades finish before their signal candle closes**, even if the recorded exit is interpreted as the *end* of its one-minute bar: 26 on 15 minutes and 16 on 5 minutes. Twenty of these are recorded SL hits. Their combined P&L is −₹24,358.38; this is the amount on temporally inconsistent rows, **not** the amount a fix would recover.

For example, `H_MIDCPNIFTY_15min_UTBOT_20260911091500` enters at 09:15 and records an exit at 09:17, while the source 15-minute bar closes at 09:30. The isolated current production simulator reproduces this class of defect: a 09:15 start can produce a 09:17 stop on one-minute candles despite being identified as a 15-minute trade. A genuine intrabar strategy would require an intrabar reconstruction of the signal, rather than the completed 15-minute OHLC used here.

**3. P1 — The historical replay omits the derivative inputs it is meant to validate.**

[Historical intelligence](C:/Users/sagnik/Desktop/rp46-3/scanner.py:31356) explicitly supplies `options_chain=None`. The new derivative history is an in-memory current-session store with no historical loader. It cannot recover last month's chain OI, PCR, Greeks, contract-level cumulative observations and comparable derivative interval changes from index and option OHLC candles alone.

The run's option-history diagnostic reports 1,371 real-option history hits, zero misses and zero futures fallbacks. This is useful price-history coverage evidence; it does not prove historical derivative-intelligence coverage, exact source identity for every decision, or fill correctness. The replay currently cannot be used as acceptance proof for the requested full broker-data integration.

**4. P1/P2 — Session, regime and VIX still contain time-boundary failures.**

- [Session context](C:/Users/sagnik/Desktop/rp46-3/scanner.py:13901) clips higher-timeframe candles by their opening timestamp. At 10:02, changing the future high of the finalized 10:00–10:05 candle changes session high from 101 to 150 and session range from 2% to 51%.
- [Hourly regime anchor](C:/Users/sagnik/Desktop/rp46-3/scanner.py:13708) catches an aware/naive timestamp comparison error and retains the full frame. Equivalent naive and IST-aware cutoffs in the probe produced 385 versus 675 source bars; the aware call included later days.
- [Historical VIX](C:/Users/sagnik/Desktop/rp46-3/scanner.py:31015) may return a later same-day observation or a same-day daily close stored at midnight. The current local VIX file ends on **1 August 2026**. With this file, the resolver returns the **15.0 fallback** throughout the captured August–September trade window. The run does not validate actual historical volatility conditions.

These are reproduced correctness defects. Their individual P&L effect, including its direction, has not been established.

**5. P2 — Broker fallbacks have different information content and remain incompletely integrated.**

| Source | Actual role | Remaining limit |
|---|---|---|
| FYERS | Index OHLCV and primary option chain; optional exact-expiry Kite OI overlay | Chain observations are receipt-timed and commonly at least 90 seconds apart. A reported day OI change cannot represent 1/5/15-minute change. |
| Kite | Near-month futures observations and selected registered option FULL ticks | Can supply exchange-timed contract observations after warmup; new intervals still lack decision consumers. Selected option contracts are not the whole option universe. |
| Angel | Fallback option chain around five complete nearest CE/PE pairs | Narrower coverage and receipt timing are not equivalent to a complete exchange-timed chain. It cannot supply exact one-minute participation from sparse snapshots. |

The futures quote lane is Kite-based, not an equivalent three-broker futures failover. Cash-index candle quantities, futures contract quantities, chain cumulative quantities and tick counts must continue to retain distinct identities.

A FYERS production-method probe observes genuine put OI growth of 1,000 over 90 seconds, but nonzero broker day-change fields take precedence and receive a horizon of 86,400 seconds. OITracker correctly withholds that day change from short-timeframe direction. The missing piece is a separately measured, appropriately covered rolling interval consumer. When a day-change field is zero, the fallback poll-to-poll change is still not automatically a complete five- or fifteen-minute change.

**6. Deployment verification is incomplete.**

All 16 source hashes in the prior certification still match the current files. However, the active app processes started at **20:47:06 on 12 September**, while `scanner.py` was last edited at **21:28:46**. The captured replay began later, but a later replay start does not establish that the existing Python process reloaded edited modules. No runtime code fingerprint was available to prove that this run included the last scanner edits. The earlier report explicitly limited implementation to local files; the running build still needs separate verification.

**Additional reconciliation issue requiring the original execution trace.** The MIDCPNIFTY example records an SL fill at 150.88 at 09:17. The currently stored `MIDCPNIFTY26SEP14350PE_1min.csv` has a 09:17 low of 178.00 and open of 178.00. This fill cannot be explained by that candle. The API capture does not include the original contract-specific replay price path and guard trace, and the historical row was not persisted in the trade database. Treat this as an unresolved input/fill mismatch, not proof of which individual function changed the price. A corrected replay must retain selected expiry/contract, input hashes and per-step stop/fill evidence.

**Verification evidence and repair order.**

The fresh focused run passed **114 tests, 228 warnings, 10.77 seconds**:

`python -m pytest tests/test_remaining_audit_repairs_2026_09_11.py tests/test_session_context.py tests/test_per_timeframe_regime_reaches_the_candidate_2026_08_13.py -q --disable-warnings --tb=short --junitxml=reports/verification_focused_20260913.xml`

The existing 6,025-test result remains evidence for the matching source files and covered fixtures; it is not an end-to-end strategy certificate. The independent probes expose acceptance failures those fixtures missed.

1. Establish causal signal availability and fills, remove future higher-timeframe/VIX/anchor inputs, preserve contract and guard traces, and verify the actual loaded build.
2. Complete derivative consumers with explicit provider/contract/expiry/units/time-window/coverage semantics, plus historical input coverage. Missing data must abstain and be reported; it must not be replaced with a misleading directional value or another asset's volume.
3. Run the same fixed-data, fixed-settings replay through old and corrected behavior with per-trade attribution. Only after that, evaluate grading, filtering and trailing on chronologically separate holdout sessions. Changing gates on this losing sample alone would not demonstrate an improvement.

The verified conclusion is **incomplete integration plus an unreliable historical timing model**, alongside a reconciled recorded loss. No defensible profitability improvement or “all bugs fixed” claim is available from this run.

Detailed evidence: [replay probe and accounting](C:/Users/sagnik/Desktop/rp46-3/reports/verification_losing_replay_20260913.json), [reproducible replay probe](C:/Users/sagnik/Desktop/rp46-3/reports/verification_losing_replay_20260913.py), [derivative-path report](C:/Users/sagnik/Desktop/rp46-3/reports/verification_derivative_paths_20260913.md), [regime/session report](C:/Users/sagnik/Desktop/rp46-3/reports/verification_regime_paths_20260913.md), [focused test result](C:/Users/sagnik/Desktop/rp46-3/reports/verification_focused_20260913.xml), [runtime start times](C:/Users/sagnik/Desktop/rp46-3/reports/verification_runtime_processes_20260913.json).
