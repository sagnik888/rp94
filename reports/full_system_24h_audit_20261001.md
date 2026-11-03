# UT SMART EXIT: full integration audit, 1 October 2026

## Scope and evidence

The audit covers the scanner changes from the preceding 24 hours: native entry admission, closed and intrabar signals, stabilization, the independent repaint worker, exit monitoring, option premium caps, controlled stop extension, historical replay, dashboard accounting and restart visibility. Tests use isolated SQLite and disable settings persistence; no broker orders were submitted by this audit.

The initial full suite completed with 7,750 passing tests, one failing structural assertion, four deselected integration tests and one existing warning. The assertion expected exactly three calls to the shared quality evaluator; the final-arrow preflight correctly adds a fourth. The assertion now requires that additional shared call explicitly.

## Bugs reproduced and repaired

1. **A fresh empty UTS scan retained buffer time.** A valid evaluation with no arrow now discards the pending entry. Reappearance starts the full buffer again. All three timeframes are covered.
2. **A populated UTS result could bypass observation freshness.** Empty and populated scans now share engine, instrument, timeframe, observation-age and candle-coverage validation. Unknown or stale observations cannot mature a buffer or prove a repaint.
3. **Normal exit monitoring did not persist a repaint obligation.** A proven repaint now latches at the common policy layer. Broker retries, a returning arrow and restart cannot erase the obligation. Both ordinary trades and session rows track the dirty state. Auto/Manual exit authority stays intact.
4. **Missing or invalid volume became measured zero.** Native admission now distinguishes unavailable volume from a valid measured zero. Only measured evidence can establish joint extreme weakness. Generic grade restrictions remain excluded from UTS admission.
5. **Paired option replay sized trades from an estimated premium.** UTS risk sizing now uses the observed option entry and actual initial stop. A cheap estimate cannot admit an over-budget contract; an expensive estimate cannot reject an affordable one. Quantity-dependent path values and costs stay aligned.
6. **Futures replay used pre-entry candles and skipped final-arrow intraday stops.** UTS futures paths now start at confirmation, use only completed one-minute bars and walk stops/targets for the final signal. Gap stop fills retain the modeled opening price. These are explicitly index-price proxies, not actual futures contract history.
7. **Dashboard normalization overwrote futures gap fills and costs.** A completed, validated causal index-proxy path now carries evidence bound to its entry, exit and times. The adapter preserves that modeled outcome and its accounting. Missing, incomplete or mismatched evidence retains the legacy path. Chart exit reasons use the canonical UTS name.
8. **MIDCPNIFTY session rows missed an approved stop extension.** The mirror assumed every ordinary stop began at 90% of premium. It now reads the instrument profile, including MIDCPNIFTY/MIDCAPNIFTY at 93%, and mirrors an exact matching broker trade's acknowledged extension. Initial-risk metadata remains unchanged.
9. **Runtime reload detection omitted UTS execution modules.** The loaded-build fingerprint now includes all audited execution, repaint, risk, persistence and display modules.

## Verification coverage

- A 240-case matrix covers NIFTY, BANKNIFTY, SENSEX and MIDCPNIFTY; 1m, 5m and 15m; both directions; ten market/setup cases. Each case exercises admission, filled premium caps and ordered stop/peak/bounce sequences.
- The risk-focused gate passed 477 tests. The first neighboring integration gate passed 348 tests. Subsequent review regressions cover stale observations, database restore, manual authority, futures report normalization and invalid replay evidence.
- Stop rules remain 5% / 10% / 15% for the three standard indices and 3.5% / 7% / 10% for MIDCPNIFTY. Ordinary 1m entries remain disallowed; eligible special entries retain their narrow cap.
- Buffer rules remain 20 seconds for 5m/15m and the configured 5–7 seconds for 1m. The post-entry repaint check remains independent of normal trailing and scanning.
- Syntax checks passed for dashboard settings, trade table, app and chart JavaScript.
- The previous full run was interrupted at 83% without a final result. Its source snapshot stayed unchanged. Verification was restarted with a detached runner and a durable per-test progress log. **Final complete suite: 8,058 passed, zero failures, zero errors, zero skips; 996.54 seconds. All four integration tests ran. One existing PytestReturnNotNoneWarning remains in test_pipeline_speed_e2e. The final source and protected settings/state hashes remained unchanged throughout the run.**

## Offline production replay

A copied candle/contract store ran the real Scanner, SignalProcessor and TradeManager with network connections blocked and a separate database. All four indices and three timeframes were enabled. The run produced 49 closed option trades for 30 September, 24 winners and approximately +Rs.4,786 net modeled P&L. It made zero network attempts and used no synthetic option fallback. No qualifying 1m trades were admitted; the trades were on 5m and 15m. Results were mixed: NIFTY +Rs.2,219; BANKNIFTY -Rs.125; SENSEX +Rs.3,754; MIDCPNIFTY -Rs.1,061. This is a correctness smoke test on available records, not a profitability guarantee or a tick-level intrabar backtest. The offline reader does not supply recorded option-chain or derivative participation context, so its result is not directly comparable to the dashboard's earlier multi-day run.

The run completed before the final stale-buffer and futures-display refinements, which do not change its option replay fills. Those refinements have separate integration regressions. All 49 persisted option rows were additionally passed through the final dashboard accounting adapter: exits, charges and P&L were unchanged, net remained Rs.4,786.13, and no exit preceded entry. The result and input workspace are retained in `full_system_24h_offline_session_20261001.json` and its matching `.workspace` directory.

## Performance and runtime limits

The original running process had completed a roughly 2h18m historical rebuild; it was no longer stuck. A profiled offline run crossed an execution interruption and reported 10,538 wall seconds, almost all attributed to array copying. That is not sufficient evidence of a copying defect: a subsequent 150-copy check over the current NIFTY frames took approximately 0.063 wall seconds total. No speculative change was made to candle ownership or copying.

A later read-only runtime check found a new process, PID 16596, in HISTORICAL mode, idle with unchanged inputs, and reported 94% system RAM usage. That process completed its latest rebuild in 226.39 seconds: 55.93 seconds scanning and 170.40 seconds waiting. Its build correctly reports that the latest scanner, UTS observation and replay accounting changes require a reload. The final read-only check at 10:42 IST found the same process in REAL mode, with system performance reported OK, no current issues, no stale scanner and recalculation idle. It still requires a reload for scanner.py, engine/signal_processor.py, engine/uts_lane.py and engine/trade_row_adapter.py. This audit did not restart the application, change account settings or place orders. Backtest latency under resource pressure remains a measured limitation rather than a claimed repair.

Historical one-minute OHLC cannot prove 6-second/20-second buffer survival or every intrabar tick ordering. Those transitions are verified with deterministic event tests. Synthetic market cases cover the named regimes, not every possible market path. Exchange fills can cross a stop during a gap; percentage caps are stop levels, not guaranteed maximum realized losses.

## Artifacts

- `full_system_24h_changes_20261001.patch`: audit-only source changes against the pre-audit snapshot.
- `full_system_24h_before_20261001/`: source backups preserving earlier work.
- `full_system_24h_complete_20261001.xml` and matching log/manifest: final complete-suite evidence. The durable progress file records each executed test.
- `full_system_24h_runtime_start_20261001.json` and `full_system_24h_runtime_finish_20261001.json`: selected read-only runtime evidence.
- `uts_futures_display_final_20261001.xml`: observed/proxy fill and accounting regression gate.
- `full_system_24h_runtime_final_20261001.json`: final read-only runtime activation and performance status.
