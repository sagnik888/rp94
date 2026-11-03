# Trading-system repairs and final verification — 14 September 2026

The reproduced defects are repaired and the clean full suite passed **6,256 tests, zero failures/errors/skips**. The final frozen-input replay returned **−₹2,116.37 after configured costs across 102 option trades**, with 49 wins and 53 losses. **Reliable profitability has not been established.**

This final result supersedes the earlier corrected 86-trade / −₹63.92 provisional result and the stopped `offline_final_20260914` run. The latter was invalidated when runtime verification exposed the remaining expiry-to-stop mismatch. No strategy parameter was fitted to this sample to manufacture a profit.

The originally reported **−₹60,093.48, 96 trades, 22 wins and 74 losses** remains a captured backtest result. Its old running process had no trustworthy source fingerprint or original contract-input trace. The saved-source baseline below therefore cannot be presented as an exact reconstruction of those 96 trades.

## Implemented corrections

| Failure | Correction and evidence |
|---|---|
| Historical signals could trade before their 5/15-minute source candle closed | Signal availability is source-bar start plus timeframe. Entries use the first eligible option one-minute open; identifiers retain original signal time. Regression covers 1/5/15 minutes and time zones. |
| Future higher-timeframe, VIX, breadth, session and detector state could influence historical decisions | Only completed bars and observations at/before decision time are available. Historical regime/PCR/OI state is isolated; future-dated confidence-calibration outcomes are excluded. Missing VIX is unavailable rather than evidence of low volatility. |
| Wrong option strike/expiry could stand in for a requested historical contract | Archive lookup requires exact strike and verified expiry under the configured rollover policy. Monthly symbols need a saved instrument master. Original contract, input digest, price path and exit trace are retained. |
| Stops and trailing decisions could produce non-executable historical fills | Resting stop/target order is conservative when OHLC cannot reveal order; gap stops fill at the observed open; close-time guards fill at the observed close. Incomplete option price windows are rejected in `fetch_or_skip` mode. |
| Historical post-entry checks still queried live caches | Both general and 15-minute management adapters resolve the matching full historical context at the replay clock. Context carries its timeframe and as-of timestamp. Per-index strike spacing replaces a hardcoded 50. |
| Futures/options observations were collected without a decision consumer | Eligible, correctly identified 1/5/15-minute evidence can adjust existing aggregate conviction once within ±10%. It cannot create or reverse direction or become a new hard entry gate. This weighting has correctness tests; no empirical profitability claim is made for it. |
| Cumulative day OI was treated as interval evidence or mixed with another provider | Complete measured windows remain separate from day changes. Provider, expiry, strike, option family, clock and units must match; complete replacement cohorts carry both levels and deltas together. Sparse receipt-time snapshots abstain from precise intraday claims. |
| Selected option ticks lacked matched CE/PE coverage | Existing background chain refresh registers paired nearby contracts at the exact expiry. Futures price/volume and paired option premium/volume evidence remain separate. High-rate observations retain fifteen-minute coverage; archive disk writes do not hold the tick lock. |
| A later backtest was mistaken for proof that edited code was loaded | Runtime source fingerprint and reload-required diagnostics now identify the imported source snapshot. An authenticated local replay-audit endpoint exposes the original trade path and guards and rechecks mode on the scanner loop. |
| Synthetic options and futures index proxies could be mistaken for observed derivative prices | Each replay input labels `OBSERVED_OPTION_OHLC`, `SYNTHETIC_OPTION_ESTIMATE` or `INDEX_PRICE_PROXY`. The legacy BACKTEST family tag is retained for compatibility. |
| The application silently discarded most of a requested historical window | A 30-session historical load now retains its one-minute candles plus warmup; the REAL-mode capacity returns to the existing live limit. Loaded-row counts reflect retained unique rows. Broker fetch planning allows 60 calendar days; the seven-day constraint applies only to Yahoo's one-minute fallback. Disk merges preserve 60 dated sessions with the existing 5,000-row minimum instead of cutting all minute archives to 5,000 rows. |
| Historical entry helpers could inspect the next unfinished candle or future peer signal | Thrust, VWAP, structure, reclaim and cached cross-index votes use the same candle-close availability clock. Empty historical depth cannot fetch live depth. |
| Historical DTE depended on today's broker login/master and could change option stop geometry | Historical expiry uses archived identity; observed stop mapping uses the exact traded contract expiry. The actual option open and mapped/capped stop feed risk sizing. Missing chain expiry abstains from IV/Greeks calculations. Live execution keeps its current-master requirements. |

## Completed paired replay

| Identical frozen inputs and settings | Before repairs | Final corrected source |
|---|---:|---:|
| Trades | 143 | 102 |
| Net after configured costs | ₹27,049.10 | −₹2,116.37 |
| Win rate | 58.74% | 48.04% |
| Profit factor | 2.1007 | 0.8876 |
| Chronological maximum drawdown | ₹6,921.40 | ₹6,706.16 |
| Configured costs | ₹24,402.48 | ₹17,544.03 |

The paired net change is **−₹29,165.47**. This comparison measures changes in software decisions and replay correctness; it does not establish that the old build's apparent gains were tradable. The corrected gross result before configured costs is ₹15,427.66. Costs of ₹17,544.03 then produce the net above.

| Index | Trades | Net after costs | Wins/losses |
|---|---:|---:|---:|
| BANKNIFTY | 11 | −₹340.41 | 5/6 |
| MIDCPNIFTY | 35 | ₹5,574.96 | 18/17 |
| NIFTY | 30 | −₹1,651.58 | 19/11 |
| SENSEX | 26 | −₹5,699.34 | 7/19 |

| Timeframe | Trades | Net after costs | Wins/losses |
|---|---:|---:|---:|
| 15min | 18 | ₹293.88 | 13/5 |
| 1min | 14 | −₹1,930.89 | 3/11 |
| 5min | 70 | −₹479.36 | 33/37 |

| Grade | Trades | Net after costs | Wins/losses |
|---|---:|---:|---:|
| A (Hist) | 32 | −₹3,251.41 | 9/23 |
| A+ (Hist) | 68 | ₹1,098.40 | 38/30 |
| B+ (Hist) | 2 | ₹36.64 | 2/0 |

54 trades share an original signal ID and exact contract across the two builds. Their net changed from ₹24,021.39 to ₹2,164.39. There are 1 shared IDs with changed contracts, 88 removed IDs and 47 added IDs. Aggregate differences cannot be attributed solely to filtering.

Chronological slices are descriptive: earlier −₹3,618.05, later ₹1,501.68; the last five available sessions returned ₹1,874.78. These are not a prospective holdout certification. Removing a losing index or changing gates after reading these results would require a fresh independent evaluation.

## Trade-path verification

All **102 observed option trade paths** passed the independent contract, input digest, signal-availability, first-eligible-open and exit-OHLC checks. All **102** passed exact expiry/DTE, delta sign, stop mapping and sizing-entry/risk/lot reconciliation. There were **zero price/contract issues and zero `ERROR_ABSTAIN` guard events**. The validated Parquet bundle accounted for 19 digests containing extra null telemetry columns; their original OHLCV and complete bundle hashes were matched.

10 trades lacked a complete underlying one-minute management window. Their option price paths were present; missing underlying observations cannot be recreated by a code fix. The run contains observed options, with no synthetic-option or futures-proxy trades.

The delta used for natural stop sizing remains an **estimate using the existing 15% IV assumption** when historical Greeks are absent. The audit records that fact. Observed option prices are not evidence that historical Greeks or real execution were observed.

## Broker and data meaning

FYERS constituent-index candle volume, futures-contract cumulative volume/OI, and option-contract volume/OI remain separate data families. Futures/option interval evidence must match provider, contract, expiry, strike/leg, units, exchange clock and 1/5/15-minute coverage. Receipt-timed or sparse chain fallbacks cannot impersonate an exchange-measured minute interval. Missing participation does not become bearish, zero activity, or a second cash-volume confirmation.

Eligible derivative participation can adjust existing aggregate conviction once within ±10%; it cannot invent/reverse direction or add a hard entry gate. Exact historical intelligence feeds entry and post-entry management; regime/session data use completed bars at the decision clock. See `derivative_repairs_20260913.md`, `historical_context_repairs_20260913.md`, `final_entry_clock_repairs_20260914.md` and `historical_expiry_repairs_20260914.md` for implementation evidence.

Historical chain, exchange-timed futures/option participation and VIX coverage are unavailable for these saved sessions. The replay therefore verifies missing-data abstention and the observed price/regime paths, not the profitability of derivative-volume inputs. The new archive records future observations; it cannot reconstruct the past.

## Reproducibility and runtime

The inputs contain **345 checksummed files**. Input manifest SHA256: `8b71fd37366a1f399a01e5af131e89e93a889d139281e16b7421bf2a81d150c7`. Before/after settings hashes match and both runs recorded zero network attempts. The harness uses production historical candidate and management adapters, isolated databases, exact saved option prices and the same full cached frames. Application loading/retention is checked separately.

```text
venv/Scripts/python.exe -W ignore -m pytest tests -o addopts=--import-mode=importlib -q --disable-warnings --tb=short --junitxml=reports/all_expiry_verified_20260914.xml
venv/Scripts/python.exe -W ignore scripts/verify_offline_engine_replay.py --source reports/replay_expiry_final_source_20260914 --inputs reports/replay_frozen_inputs_v2_20260913 --output reports/offline_expiry_final_20260914.json
venv/Scripts/python.exe scripts/analyze_paired_engine_replay.py --corrected reports/offline_expiry_final_20260914.json --output reports/paired_replay_expiry_analysis_20260914.json --require-expiry-evidence
```

The clean full suite passed in **288.66 seconds**. Two synthetic-mark test stubs in the preceding run lacked the new expiry adapter; the fixtures were completed and the entire suite rerun. No failing test was disabled. Monetary figures use full-precision prices and charges because the generic serializer rounds displayed P&L to whole rupees; drawdown is ordered by exact exit timestamps.

The app was restarted in **HISTORICAL mode, Auto off, 30-day selection restored**. Verified runtime fingerprint: `3b071214aca34fcd2904d93d29bff6f28d62f29222202763c1c19514d92c2522`, with `reload_required=false`. No live orders were placed. The runtime proof records dashboard recalculation state separately from the completed frozen replay; a building dashboard is not a completed performance result. See `runtime_expiry_verified_20260914.json`.

## Remaining limits and live-market implications

- Saved one-minute index history covers approximately 14 sessions and five-minute history 22 sessions. A 30-day selection does not create the missing observations.
- The saved live configuration uses INTRACANDLE signals. OHLC cannot recreate actual intrabar signal arrival or event order. This replay waits for source-bar closure and uses conservative stop-first handling when candle ordering is unknown.
- Configured execution costs are included. Liquidity, queue priority, real spreads and actual broker fills have not been validated by a candle replay.
- A/A+ grades are not established win probabilities. Software correctness and profitable trading are separate claims. A fully recorded independent sample is still needed to test the strategy's edge.
- Historical market-location gating retains its pre-existing explicit bypass; the entry-clock repair does not claim that this gate runs on historical trades.

Backtest losses do **not** debit the broker account. Shared data, grading and trade-management defects can affect real trades, but a backtest amount is not an exact forecast of live loss. The fixes and passing tests do not justify a guarantee of profitability.

Runtime recheck at 2026-09-14 21:03:10 IST: the process had stopped during the interruption. It was restarted with the same verified source fingerprint, HISTORICAL mode, Auto off and 30 days restored. The dashboard is recalculating; the completed 102-trade performance result above is the frozen offline replay. Evidence: `runtime_expiry_resumed_20260914.json`.
