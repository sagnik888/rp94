# UT SMART EXIT loss investigation — 30 September 2026

UT SMART EXIT's recent losses have three supported contributors: intrabar entry/exit churn, admission of setups rejected by the other engines' quality policy, and a mismatch between the chart's exit horizon and the option's premium stop. Two exit-evaluation defects are also reproducible. The candle-close defect fits a recorded BANKNIFTY exit, but the original input snapshot is unavailable, so its historical attribution is not proven.

This investigation made no changes to the trading implementation, live settings, or orders. It created a read-only evidence collector, its JSON output, this report, and a focused test-result XML.

## Recorded losses and evidence boundaries

The September 29 session archive contains seven `trade_manager_terminal_fill` closed rows. These represent app-recorded filled trades, distinct from mirrored session signals and ghost trades. Their recorded net total is **−₹5,502.50**: six `UTS_REPAINT` exits total **−₹2,419.50**, and one manually closed NIFTY trade contributes **−₹3,083.00**. All seven entered before their signal candle closed.

| September 29 filled trade | Signal candle | Recorded exit | Net P&L | Normal quality verdict |
|---|---|---|---:|---|
| SENSEX long, 5m | 11:20 | UTS_REPAINT | −₹264.00 | Allowed |
| BANKNIFTY long, 5m | 11:25 | UTS_REPAINT | −₹84.50 | Rejected: insufficient confirmation |
| NIFTY long, 5m | 11:30 | UTS_REPAINT | +₹154.00 | Allowed |
| SENSEX short, 15m | 12:30 | UTS_REPAINT | −₹1,159.00 | Rejected: confidence 8.1% |
| SENSEX short, 15m | 13:30 | UTS_REPAINT | −₹380.00 | Rejected: confidence 43.2% |
| SENSEX short, 5m | 13:40 | UTS_REPAINT | −₹686.00 | Rejected: confidence 27.6% |
| NIFTY short, 15m | 13:30 | MANUAL | −₹3,083.00 | Rejected: confidence 48.0% |

On September 30, the filled NIFTY 22700 CE trade entered at **₹203.80 at 10:15:19 IST** and exited at **₹183.50 at 10:33:16**, recording **−₹1,400**, including the app's ₹80 cost estimate. Its signal candle started at 10:00 and closed at 10:15: this particular fill was a closed-candle entry despite the overall intrabar mode. Peak recorded P&L was **+₹372.86**.

Broker contract notes and actual charges are unavailable. These totals are app-recorded results, not reconciled exchange statements. Do not add session/ghost results to these filled-trade totals. The current database retains only a small trade window; the archive is required to recover yesterday's terminal fills.

## Workflow actually running

The runtime snapshot reports REAL mode, Auto on, `scanner_engine=ut_smart`, `signal_fire_mode=INTRACANDLE`, and `ut_timeframe_entry_policy=INCLUDE_5MIN` (5m plus 15m). The engine uses HA close as its source, ATR/key 14/2.0 on 15m and 21/4.5 on 5m. Forming flips wait 20 seconds, then the lane checks for disappearance every 10 seconds until confirmation.

The path is: candle input, UT/HA crossover, signal processor and option contract selection, stabilization, live data/safety checks, lane-specific admission, final geometry policy, broker order/fill, then lane exit evaluation and broker premium protection. The live admission gate returns early for UTS, before the normal quality/location selection stack. Both real trades and session rows use the same lane exit evaluator. UTS returns HOLD unless its chart reports an exit; it supplies no discretionary index stop, premium trail, or profit lock. Its nominal target is not a profit-taking rule. [Admission](C:/Users/sagnik/Desktop/rp94/rp46-3/scanner.py:29420), [lane exit policy](C:/Users/sagnik/Desktop/rp94/rp46-3/engine/uts_lane.py:155), [monitor](C:/Users/sagnik/Desktop/rp94/rp46-3/engine/exit_cycle.py:222).

This explains why changing the scanner changes more than its entry signal. UTS does not inherit the same selection and profit protection as the other engines.

## 1. Confirmed defect: a candle-close gap can produce a false repaint exit

`_process_ut_smart` always removes the final input candle. It only evaluates that candle as forming while its calculated close time is later than the wall clock. Just after the candle closes, but before the next input candle exists, the final candle is evaluated as neither closed nor forming. `repaint_exit` then interprets the missing signal as REPAINT. [Dropped final candle](C:/Users/sagnik/Desktop/rp94/rp46-3/engine/alt_scanners.py:1049), [repaint decision](C:/Users/sagnik/Desktop/rp94/rp46-3/engine/uts_lane.py:245).

The audit reproduces this using the production engine and archived BANKNIFTY candles. At **11:30:03**, with input ending at **11:25**, the confirmed 11:25 BUY disappears and the trade receives REPAINT. Adding a flat 11:30 sentinel candle, without changing any earlier price, restores the same BUY and removes the exit. Yesterday's BANKNIFTY session exit occurred at **11:30:03**, followed by the actual fill close at **11:30:09**. That timing is consistent with this defect; the saved final candles cannot prove what the original input contained at that instant.

Repair: classify candles using their close time and an explicit evaluation time/input contract. A closed final bar must remain eligible even when its successor is missing. Preserve the historical replay's evaluation clock. Test absent next bar, delayed next bar, timezone-aware input, and both signal-fire modes.

## 2. Confirmed defect: unavailable or stale results are treated as signal disappearance

`repaint_exit` validates that the result is a dictionary but does not require a successful computation, current observation, correct evaluated bar, or coverage of the trade's signal candle. A synthetic production-helper probe returns REPAINT for both `{"error": "Insufficient UT SMART EXIT data", "signals": [], "state": null}` and a stale result ending before the signal candle. Both callers read the result cache directly. [Missing validation](C:/Users/sagnik/Desktop/rp94/rp46-3/engine/uts_lane.py:208), [real-trade caller](C:/Users/sagnik/Desktop/rp94/rp46-3/engine/exit_cycle.py:229), [session caller](C:/Users/sagnik/Desktop/rp94/rp46-3/scanner.py:29921).

Repair: require a successful, coherent, sufficiently fresh result that actually evaluates the wanted candle before asserting disappearance. An unavailable observation should defer the chart-based exit while the existing premium protection remains active. Capture result version, input timestamp, evaluated candle, source/ATR/trail, and the exact missing-flip reason in exit forensics. No saved evidence proves that this defect caused a specific filled exit.

## 3. Observed contributor: 20 seconds of stability does not confirm a 5m/15m candle

Six of yesterday's seven fills exited through repaint protection. Median hold across those six was **148.67 seconds**; BANKNIFTY was held about **17.16 seconds**. The current 20-second entry wait is much shorter than either candle duration.

Reconstructing finalized candles with prior history and the current parameters produces the same-direction closed flip for four of those six repaint trades: BANKNIFTY 11:25, NIFTY 11:30, SENSEX 13:30, and SENSEX 13:40. The other two SENSEX entries do not confirm. Full available history and 300-bar warm-up agree on all seven final flip directions. This makes inadequate warm-up a weaker explanation for this sample.

A flip may disappear during a candle and return by its close. The present policy intentionally exits on the temporary disappearance. Final OHLC cannot tell how much of the four confirmed cases came from real temporary disappearance, the candle-close defect, or revised/stale input. TradingView likewise documents that realtime HLC values change until close and that historical OHLC does not preserve intermediate movements. [TradingView repainting documentation](https://www.tradingview.com/pine-script-docs/concepts/repainting/).

Validate confirmed-candle UTS separately from intrabar UTS. A longer wait alone is not proof of confirmation or improved profitability. Do not remove repaint protection while keeping provisional entries without evaluating the resulting risk.

## 4. Observed contributor: UTS admits rejected quality and displays nonbinding grades

Five of the seven recorded filled trades have `quality.allowed=false` and a TELEMETRY lane. Together these trades recorded **−₹5,392.50**. The SENSEX 12:30 short had only 8.1% confidence; the 13:40 short had grade C, 27.6% confidence, a ranging regime, and volume ratio 0.68. They still entered because UTS intentionally bypasses quality selection. An A+ label is also a heuristic score, not a calibrated probability of making money.

This is an explicit lane policy, not an accidental skipped conditional. Applying the existing quality verdict would exclude these five specific entries, but the observed sum is **not** a forecast of savings: later entries, portfolio overlap and costs would change. Compare a small, explicit UTS selection policy on identical causal events and contracts before changing admission. Keep any such change separate from the proven exit defects.

## 5. Observed contributor: today's premium stop and Smart Exit operate on different risk scales

Today's NIFTY entry uses an underlying stop at **22,654.46**, **103.14 index points** from the entry spot. Entry delta 0.5375 maps that distance to approximately **₹55.44 of premium risk**. The actual premium stop allows only **₹20.25**, or **9.94%** of premium: about **36.5%** of that linear delta estimate. Until the stop minute, the saved index lows remained at or above **22,718.95**, well above the underlying stop. The option therefore stops before the chart's underlying invalidation. Delta mapping is an approximation; theta, IV, spread and nonlinear delta also affect the option.

Smart Exit needs a run-up of **2.5 ATR** and **two subsequent bars**, plus all four exhaustion votes for the ordinary smart exit. For this 15m entry, two later bars first finish at **10:45**. Reconstructed entry ATR is 42.81, so the arming move is approximately **107.04 index points**. The filled option exits at 10:33, before that minimum bar count is possible. The later runtime chart still shows LONG and run-up well below 2.5 ATR. Other engines' breakeven and profit locks do not run in this lane. A small positive excursion consequently has no Smart Exit protection.

Yesterday's terminal-fill geometry records roughly **50% premium stops**. The current source and today's fill use the newer shared 5%/10%/15% ceilings. This risk policy change must be distinguished from yesterday's entry/exit defects. Widening stops is not a demonstrated repair. Align the signal geometry, option selection/sizing, and approved premium budget, or reject an incompatible entry. [Current cap](C:/Users/sagnik/Desktop/rp94/rp46-3/engine/market_regime.py:92), [fill enforcement](C:/Users/sagnik/Desktop/rp94/rp46-3/trading/trade_manager.py:6417).

## 6. Additional reproduced accounting issue and tuning limitation

The chart state starts its peak/trough from the signal candle's high/low even though entry is at its close. A probe enters long at 100 after a signal candle touched 130; subsequent highs never exceed 101. The engine counts 3 ATR of run-up and emits SMART_HARD_EXIT, although post-entry run-up is only 0.1 ATR. This behavior may be inherited from the original Pine script; it is inconsistent with execution-based run-up accounting. Its contribution to the recent filled losses is unproven. [Peak initialization](C:/Users/sagnik/Desktop/rp94/rp46-3/engine/ut_smart_engine.py:369).

The original tuning study calculates P&L as signed **index movement × fixed delta 0.5 × lot size**, minus assumed costs. It does not reproduce the actual option's premium path or today's universal premium-stop cap. Its intrabar study uses minute steps and a 12:45 entry cutoff, unlike the current 20-second wait, 10-second repaint checks and later entry windows. Parameter selection examines performance in both halves, so those halves are not an untouched validation set. Historical tuning gains are therefore not evidence that this live policy has positive option expectancy. [P&L model](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_exit_study_20260925.py:156), [intrabar study assumptions](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_intracandle_study_20260926.py:1).

## Repair and validation order

1. Correct closed-final-bar classification and defer repaint decisions on failed/stale/incomplete input. These are correctness changes, independent of trading preferences.
2. Record the complete evaluated signal and result version at entry and exit. Persist the trade's actual confirmation state and policy identity; distinguish filled trades from mirrored session rows.
3. Compare confirmed-candle and provisional UTS using identical events, contracts, costs, stops, and entry windows. Evaluate explicit quality selection and exit protection separately.
4. Use actual option observations and the current stop policy for replay. Resolve incompatible stop geometry through selection or size; do not infer that larger stops or tighter profit locks guarantee gains.
5. Evaluate fixed candidates on untouched sessions with portfolio constraints before attributing a profitability improvement.

The runtime snapshot is fresh, with a 144 ms scan reading in one snapshot and no stale scanner flag; current evidence does not identify general scan delay as the main loss driver. This does not establish that every historical result was fresh. The recent filled sample contains UTS trades only, so it cannot verify the claim that every other scanner currently makes gains on a matched executable portfolio.

## Verification and reproducibility

The focused existing suite passes **167 tests**. The audit additionally asserts the candle-close reproduction, stale/error repaint behavior, pre-entry peak accounting, and absence of a chartless UTS profit lock. Passing the existing suite establishes tested behavior, not profitability; the newly demonstrated edge cases are missing from those tests.

```powershell
venv/Scripts/python.exe -B reports/ut_smart_loss_audit_20260930.py
venv/Scripts/python.exe -B -m pytest -q tests/test_ut_smart_exit_2026_09_25.py tests/test_ut_smart_live_path_2026_09_26.py tests/test_universal_option_stop_policy_2026_09_29.py --junitxml=reports/ut_smart_loss_audit_20260930_tests.xml
```

The collector reads production SQLite in read-only mode, uses GET requests for dashboard inspection, never saves the dashboard credential, and writes only its adjacent evidence JSON. The JSON includes source hashes, sanitized runtime configuration, trade cohorts, reconstructed final-bar signals, and explicit limitations. A rerun refreshes its runtime snapshot and adjacent output.

[Evidence](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_loss_audit_20260930.json) · [Collector](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_loss_audit_20260930.py) · [Test results](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_loss_audit_20260930_tests.xml)
