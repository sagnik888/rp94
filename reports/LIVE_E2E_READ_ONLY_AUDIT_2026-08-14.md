# Port 8500 live-market E2E audit — 14 August 2026

**Scope.** Read-only review of the single system listening on `localhost:8500`, performed during the live Indian index-derivatives session.  No order was placed, amended, cancelled, or simulated against either broker; no source, configuration, database, or service change was made.  Other ports were deliberately ignored.

**Audit window.** Initial audit approximately 14:11–14:29 IST, followed by an independent report-verification pass at 14:36–14:39 IST.  Live assertions were checked through the dashboard, REST status and logs on multiple fresh observations.

## Bottom line

The **platform is live and scanning all four configured indices**, with live UI updates, an advancing scan counter, fresh Kite and Fyers ticks, and successful Fyers option-chain reads for every index.  The safety/trailing design is substantially exercised by its isolated test and replay suite.  That availability result carries operational-performance qualifications: the audit also observed scan-latency spikes, dashboard trade-row update timeouts, high system RAM pressure, and recurring MIDCPNIFTY cross-feed disagreement.

It is **not possible to certify “highest accuracy” or current tuning of the four volume workflows** today.  All four instruments are currently using a Kite near-month futures-volume fallback instead of the Fyers cash-index volume on which the application says its volume thresholds were calibrated.  The runtime marks each series `degraded` and `comparable_to_thresholds=false`.  In addition, confidence calibration is `ADVISORY_ONLY`, has zero outcome samples, and has no live adjustments enabled.

| Decision area | Audit result | Reason |
|---|---|---|
| Service/UI/scanner availability | Pass with performance/freshness constraints | Port 8500 remained live and scans/prices advanced, but three scans took 10–12s and the dashboard trade-row updater timed out twice. |
| Concurrent broker data integration | Pass with recurring data-quality alerts | Kite and Fyers sessions/ticks were fresh and Fyers returned all four option chains, but MIDCPNIFTY repeatedly crossed the 0.05% disagreement threshold. |
| Volume-source provenance and transition handling | Pass | Source/unit are carried into candles; mixed-source windows are rejected and the fallback is surfaced. |
| Current volume-gate calibration | **Not certified** | Every live volume feed is futures contracts, not the cash-volume basis declared for calibration. |
| Protective-stop/trailing logic | Pass in isolated lifecycle/replay tests | Ratchet-only broker protection, recovery handling and exit invariants passed. No real order mutation was performed. |
| Automatic live execution | Off at audit time | Runtime was `REAL`, but `auto_mode=false`. |
| “Highest accuracy” claim | **Not substantiated** | Zero calibration samples and one small restored-session ledger are not enough statistical evidence. |

## 1. Live end-to-end observations

### Runtime and dashboard

The sole listener on port 8500 was the project’s Python `main.py` process.  It returned the terminal page and the read-only status endpoints normally.

First checked state:

- `status=running`, `mode=REAL`, `scanner_running=true`.
- Scan count advanced from roughly 4,625 in the first dashboard view to 4,778 by the final REST recheck.
- The original final REST recheck was at **14:29:38 IST**; its last completed scan was **14:29:32** and its reported latency was 4.58 seconds.  Independent rechecks at **14:36:02** and **14:39:07** were also live; the latter was scan 4,923 with last scan 14:38:59, Kite/Fyers tick ages 0.01/0.15s, Auto Mode still off, calibration still `ADVISORY_ONLY/0`, and all four volume feeds still degraded.  These endpoint snapshots do not describe worst-case latency: logs recorded scan cycles of 10.245s, 10.084s and 11.971s at 14:29–14:31.
- A fresh dashboard load at 14:27 showed all four active index toggles and fresh prices: NIFTY 24,373.95; BANKNIFTY 57,533.55; SENSEX 77,942.65; MIDCPNIFTY 14,986.65.
- `Auto Mode` was visibly and programmatically **off**.  The system was in REAL mode, but the unattended entry switch was not enabled.

The dashboard initially paints non-live/default-looking content before its live state arrives; after roughly five seconds the page reconciled to `LIVE`, current scan counts, prices and the actual Auto Mode state.  That is an operator-observability concern, not evidence of a scanner failure.

The visible Live Tape was materially older than the scan on each fresh UI observation: newest visible item around 12:43 while the scanner was live at 14:27, then 13:04 while the dashboard clock was 14:37 and scan count 4,901.  Prices, scan count and backend telemetry continued to move, so this does not demonstrate a scanner failure; it does demonstrate that the tape should not be used alone as a real-time signal/trade freshness indicator.

### Audit visibility and dashboard-update limitation

The sensitive read endpoints (`/api/state`, `/api/diagnostics`, `/api/trades`, `/api/signal-report` and `/api/calibration-status`) correctly returned 401 without the dashboard token, so this read-only audit did not bypass that boundary.  Trade/ledger statements in this report come from the rendered dashboard and logs, not from an unauthenticated direct trade API pull.

The log recorded `periodic_trades_updater` `TimeoutError`s at 14:29:02 and 14:31:55.  The updater catches that exception and continues its background loop, while the scanner itself continued to advance.  The practical qualification is that dashboard open/closed-trade rows may be temporarily delayed during scanner contention; it is not evidence that the execution engine stopped.

At 14:37:32, host memory use was 90.6% (1.48 GB free of 15.71 GB); the UT1 process working set was about 912 MB.  The application’s telemetry warns at 88%, and the rendered tape had already shown `System RAM pressure (89.0)`.  This establishes host-level RAM pressure, not its root cause or an attribution of all usage to UT1.

### Four-index workflow coverage

The active basket was exactly:

| Index | Price observed at 14:27 fresh-dashboard check | Live volume source at 14:36–14:39 verification |
|---|---:|---|
| NIFTY | 24,373.95 | Kite index futures, contracts |
| BANKNIFTY | 57,533.55 | Kite index futures, contracts |
| SENSEX | 77,942.65 | Kite index futures, contracts |
| MIDCPNIFTY | 14,986.65 | Kite index futures, contracts |

The combined dashboard, scanner and log evidence supports the four-index route: live price resolution, 1m/5m/15m candle availability, signal/filter evaluation and Fyers option-chain acquisition.  Logs at 14:27 confirmed an `ok` Fyers option-chain response and 123 chain rows for each index, using the current active expiry.

## 2. Dual-broker integration

The architecture is deliberately asymmetric:

- **Kite** is the execution-side broker and the authoritative fallback for a material price disagreement.  Its session was live; tick age was 0.17 seconds at the final check.
- **Fyers** supplies live market/option-chain and intended primary index-volume data.  It was connected, did not require authentication, and had a 0.06-second tick age at the final check.

This demonstrates simultaneous broker/data operation, but not dual-broker execution.  The code routes real account mutation through Kite; Fyers is not used as a second execution venue.  A genuine live order submission was intentionally excluded because it would change account state and was not necessary for a read-only audit.

### Recurring cross-feed discrepancy

At 14:28, the system raised three `BAD MARKET DATA` alerts for MIDCPNIFTY: Fyers was approximately 7.5 points (0.05%) below Kite.  The application’s divergence guard selected Kite, explicitly described in code as the execution broker, and marked the Fyers-sourced figure suspect.  The divergence **recurred** at 14:34–14:36, with observed differences of 0.05–0.06%; it must not be described as resolved.  The public status endpoint does not expose the divergence map, so a momentary clear state cannot be independently established through that endpoint.

This is positive evidence that the disagreement detector and fallback path work.  It is nevertheless a recurring live data-quality event, so it limits any claim that every MIDCPNIFTY intrabar decision was working from perfectly aligned feeds throughout the audit/verification window.

## 3. Volume workflow audit

### Current health — consistently degraded on all four indices

The endpoint was checked twice.  At the final check all four rows were identical in the important respects:

| Index | Source | Unit | Comparable to calibrated thresholds | Degraded | Futures available | Last 10 candle bars dry? |
|---|---|---|---:|---:|---:|---:|
| NIFTY | `kite_index_futures` | contracts | No | Yes | Yes | No |
| BANKNIFTY | `kite_index_futures` | contracts | No | Yes | Yes | No |
| SENSEX | `kite_index_futures` | contracts | No | Yes | Yes | No |
| MIDCPNIFTY | `kite_index_futures` | contracts | No | Yes | Yes | No |

This is not a total-volume outage: all four have non-zero, non-dry futures-volume candles.  It is a **provenance/calibration outage**.  The primary Fyers cash-index volume is absent, so the system substitutes futures contracts and its own API reports that those contract counts are not comparable to the cash-index threshold basis.

### The four volume stages and their status

1. **Source selection and health reporting — functional.** `MarketDataProvider` uses Fyers cash-index volume when available, otherwise retrieves Kite near-month futures volume/OI and labels it `kite_index_futures`, `contracts`, `fallback_only`, and non-comparable.  `Scanner.volume_source_health()` exposes this to the dashboard/watchdog and correctly marked every current index degraded.
2. **Candle construction/provenance — functional.** `CandleBuilder` carries `volume_basis` through 1m candles and 5m/15m resampling, rebases cumulative totals when a feed/unit changes, and preserves genuine volume against zero-volume fallback data.  This prevents a source switch from manufacturing a false surge.
3. **Volume-profiler pillar — functionally guarded.** The profiler rejects mixed-unit windows as unavailable; the intelligence aggregator only applies the direct volume score when that pillar is available.  It can still use independent price/regime/OI-related components.
4. **Entry/override gates — functional but not currently calibrated.** The power-move, 1m/5m thrust, cascade, reclaim, momentum and shock routes consume relative-volume measurements.  A mixed `volume_basis` window is refused.  A homogeneous all-futures window can continue through relative-ratio logic, because a ratio is scale-invariant within one source.  That is explicitly supported by the code, but it sits beside the runtime warning that thresholds do not apply until Fyers returns.  The two statements are not equivalent: absolute scale disappears in a ratio, while the futures-volume distribution still needs its own empirical calibration.

Therefore, the fallback logic is **operationally sensible and guarded against the dangerous source-switch false surge**, but the present market state does not support a claim that the numerical ratio thresholds are accurately tuned for all four indices.  This is the audit’s central readiness constraint.

### Relevant logical nuance

The market-LTP-shock path intentionally allows a sufficiently strong multi-index price move to qualify when volume is unavailable.  That is defensible for urgent protective exits; it means that some entry/exit context may proceed on price breadth rather than volume confirmation.  It should be counted as a deliberate availability/response trade-off, not as validated volume confirmation.

## 4. Gates, locks, filters and integration behavior

### Live evidence that gates are active

The fresh dashboard showed these classes enabled: intrabar signals, price-action and PA-adverse filtering, regime veto, early-loss cut, 5m trend continuation, power-move/15m spike handling, volume-spike/1m scalp checks, depth/OFR confirmation, stabilization override, 15m momentum, peak/loss-threat exits, cross-index reversal/cascade controls, re-entry, reclaim, and the APEX lane.  Auto Mode, pullback hedge, per-timeframe grading, futures-shadow comparison, Engine Lab, touch-quote execution and strict touch quote were off.

Two live trace examples support functional gating rather than just configured switches:

- A MIDCPNIFTY 5m short was rejected because its UT1 arrow vanished after 2.4 seconds, exercising the stabilization/repaint path.
- A SENSEX 5m short had survived its 31-second stabilization period but was then rejected by the live readiness/quality gate as grade C, 40% confidence, weak ADX and risky context.

The currently off concurrency-control setting relaxes certain cross-timeframe complement rules.  It does **not** remove the independent protections: per-index capacity, same-timeframe duplicate blocking, one-futures-position-per-index, an opposite-direction stack block, and correlated core-direction caps remain in the normal path.  With Auto Mode off and no open trades in the observed dashboard state, this was not producing new automated exposure during the audit.

### Logical assessment

The gate stack is internally rich and mostly fail-safe on the testable paths:

- Explicit stabilization avoids acting on a short-lived/repainting signal.
- Price freshness and projected stop-risk checks occur before an entry is admitted.
- Option-premium-health checks fail closed where IV/quote data is unavailable or stale.
- The correlation/capacity model distinguishes NIFTY/BANKNIFTY/SENSEX as a correlated core and MIDCPNIFTY as a separate bucket.
- Mixed volume units abstain rather than fabricating a signal.

The main limitation is empirical, not a missing boolean: current volume basis is degraded, confidence calibration has no samples, and no live outcome evidence establishes that the chosen numerical filters are optimal for today’s four instruments.

## 5. Trailing engine, exits and broker protection

### Design reviewed

The protective-stop path in `trading/trade_manager.py` obtains/retains broker protection only for real, non-ghost trades.  It serializes protection updates, adopts a pre-existing valid stop, refuses duplicate/conflicting protection, and escalates a missing/rejected/ambiguous stop to recovery handling.  Stop movement is ratchet-only: an option or long-futures stop only tightens upward; a short-futures stop only tightens downward.

The exit stack layers initial/early-loss protection, trailing/target evaluation, dynamic runner locks and peak/loss-threat evidence.  The threat path requires price-domain evidence plus another adverse domain and persistence before a non-severe exit; that reduces one-tick exit risk.  The runner path retains at least 85% of a proven peak in the configured breakout/volatile cases in the replay suite.

The first dashboard snapshot’s restored-session ledger displayed diverse executed exit reasons, including EARLY LOSS CUT, PROFIT GIVEBACK GUARD, STOP HIT, OI/PCR reversal, OPPOSITE SIGNAL, PEAK THREAT EXIT and SMART PROFIT LOCK.  That shows the event taxonomy is being recorded; it does not, by itself, prove the economic quality of each exit.

### Four-index tuning caveat

The option replay uses actual configured lot sizes (SENSEX 20, BANKNIFTY 30, NIFTY 65, MIDCPNIFTY 120).  A rupee floor in the give-back arm translates to different premium-point thresholds: 25.00, 16.67, 7.69 and 4.17 respectively — a sixfold span.  The tests document this as deliberate rupee-risk sizing, and all lifecycle invariants passed.  It is not inherently wrong, but it means a uniform “equally tuned in points” claim across the four indices would be false without per-index outcome analysis.

## 6. Test evidence

I ran a non-network, non-account-mutating focused test/replay set using the project virtual environment.  It included dual-broker lifecycle, every index/timeframe matrix, four-index option-premium exit replay, volume pipeline/provenance, mixed-unit guards, volume spike gates, runner/peak-threat paths, broker mutation authority, real-account interlocks, execution-lease failure handling and unattended-live safety.

| Batch | Result |
|---|---:|
| Dual broker, four-index/timeframe, volume and spike suites | 210 passed |
| Trailing/exits, broker authority/interlocks, lease and unattended safety suites | 100 passed |
| **Total** | **310 passed** |

The only pytest warnings were an unknown `asyncio_mode` configuration option with plugin autoload disabled and a third-party `pkg_resources` deprecation warning from Fyers; neither was a test failure.

These tests validate code-path invariants and mocked/replayed data.  They do not substitute for a production-order E2E test or for a statistically valid live-performance study.

## 7. Accuracy and performance evidence

At the first dashboard snapshot, the restored-session ledger displayed 14 trades, 6 wins / 8 losses, daily P&L about +₹413, profit factor 1.13 and maximum drawdown about ₹1,301.  That is an informational dashboard reading only.  It is too small and too context-dependent to demonstrate an edge, high accuracy, or robust tuning.

The authoritative calibration status is more decisive:

- Mode: `ADVISORY_ONLY`
- State: `COLLECTING_EVIDENCE`
- Live adjustments: disabled
- Samples: `0` of a configured minimum `200`

Accordingly, no claim of self-learning or calibrated high accuracy is supportable from the running system today.

## 8. Final audit conclusion

**Functional status:** The port-8500 service, UI, scanner, four-index pipeline, broker-data integration, option-chain route, unit-transition guards, safety interlocks and trailing/exit invariants are functioning to the level demonstrated above.  Dashboard trade-row freshness and scan latency were not consistently healthy in the same observation window as elevated host RAM pressure; this audit does not establish a causal link.

**Accuracy/tuning status:** Do **not** treat the current system as certified for highest accuracy across NIFTY, BANKNIFTY, SENSEX and MIDCPNIFTY.  The primary reason is not a code crash: it is the live all-index volume-source fallback plus zero calibration evidence.  Recurring MIDCPNIFTY quote divergence and observed timing/host-pressure constraints further support retaining a data-quality qualification.

**No-fix assurance:** This audit made no implementation or configuration changes and did not invoke an order-creating, order-modifying or order-cancelling broker action.

## Evidence locations

- Runtime source/provenance: `data/market_data.py` (volume source selection and quote divergence) and `data/candle_builder.py` (basis-preserving candle construction).
- Volume/gate routing: `intelligence/volume_profiler.py`, `intelligence/intelligence_aggregator.py`, `engine/signal_quality_policy.py`, and `scanner.py`.
- Account protection/trailing: `trading/trade_manager.py`, `engine/risk_manager.py`, and `scanner.py`.
- Current runtime logs: `logs/ut1.log`.
- Test evidence: `tests/test_e2e_dual_broker_lifecycle_2026_08_14.py`, `tests/test_e2e_index_timeframe_matrix_2026_08_14.py`, `tests/test_e2e_option_premium_replay_2026_08_14.py`, and the focused safety/volume suites named above.
