# Full end-to-end Auto readiness audit — 22 August 2026

## Verdict

**The repaired build passes code, replay, persistence, closed-market runtime, dual-broker connectivity, dashboard, and recent post-market archive checks. It is not yet honest to certify fully unattended Auto execution for the next live session.**

The remaining certification gate is operational rather than another known code failure: this audit was completed on Saturday while the Indian market was closed, intentionally placed/modified/cancelled no broker order, and Auto is deliberately non-persistent. Daily Kite/Fyers authentication, four-index morning readiness, an open-market quote-latency soak, and a human Auto arm are still required.

| Decision | Result |
|---|---|
| Final build and regression suite | **Pass** — 4,917 passed, 12 skipped, 4 integration tests deselected, 0 failed |
| Four indices × three timeframes | **Pass** — all 12 logical lanes and all 12 dashboard selections verified |
| Signal virginity, buffering, stabilization and post-buffer freshness | **Pass** |
| Market intelligence → selection → trade metadata hand-off | **Pass** |
| Option entry, liquidity, touch-price and scale-in safety | **Pass** |
| Post-entry marks, trailing/protective exits and EOD square-off | **Pass** in tests/replay; no live order mutation performed |
| Kite order-broker and Fyers data-broker connectivity | **Pass** in read-only/closed-market checks |
| Persistence, restart reconciliation and realized P&L baseline | **Pass** |
| Current post-market archive | **Pass** — 17–21 August are complete; 21 August is 15/15 contracts and 12/12 spots |
| Dashboard index-LTP delay fix | **Pass** for architecture, closed-market behavior and local latency; **open-market age still needs a live-session soak** |
| Fully unattended Auto after restart | **Not supported by design** — Auto resets OFF and requires daily human confirmation |

## Scope and evidence

### 1. Four-index, three-timeframe signal path

The configured basket is NIFTY, BANKNIFTY, SENSEX and MIDCPNIFTY, with 1m, 5m and 15m lanes.

The deterministic 4×3 regression proves each of the 12 lanes independently traverses:

1. same-bar/direction signal identity and virgin filtering;
2. buffering;
3. a fresh multi-timeframe recomputation after the buffer cutoff;
4. stabilization/repaint confirmation;
5. candidate acceptance; and
6. isolation from every other index/timeframe lane.

Production intentionally collapses simultaneous competing timeframes for one index; the test admits each lane separately to validate all 12 without bypassing that concurrency policy. The configured engine remains `UTBOT+CONS`. Per-timeframe grading propagation is implemented and tested but remains default OFF pending replay parity; this is a configuration choice, not a hidden enabled path.

### 2. Virgin-signal, buffer and MTF integrity fixes

- Opposite-direction signals on the same bar no longer collide in virgin identity.
- Repaint confirmation matches full signal identity instead of accepting any signal on the bar.
- Missing, underfilled or failed MTF recomputation invalidates the prior result instead of reusing stale intelligence.
- Buffered candidates require a recomputation newer than the buffer cutoff.
- Restarted buffer timestamps retain freshness semantics; stabilization stays UNKNOWN until a fresh result exists.
- Full context is routed to each timeframe when per-timeframe context mode is enabled.

### 3. Market data and two-broker integration

The integration is deliberately asymmetric:

- **Kite** is the main order broker and authoritative index-price source.
- **Fyers** supplies the secondary market-data/option-chain route and index volume.

No claim is made that Fyers is a second order-execution venue.

Read-only audit evidence:

- Both sessions connected after the final restart; no broker was marked unavailable or degraded.
- Kite subscribed to all four index tokens; Fyers subscribed to all four index symbols.
- All four Fyers option chains returned successfully and aligned to the active expiry.
- The bounded `scripts/full_system_audit.py` run exited 0 in 12.2 seconds, returned 1,725 unified candles for every index, and verified aligned chains for all four indices.
- Websocket shutdown is now bounded, so the audit exits instead of hanging on vendor threads.

Index source arbitration is now Kite-first. Fyers price is used only after Kite is stale beyond the configured window and a two-tick/250ms hysteresis confirms the failover. Exchange timestamps and local receipt timestamps are kept separately, and freshness uses the conservative age. Closed/replay packets cannot mutate the live candle builder.

### 4. Dashboard spot-LTP delay and truthfulness

The Aug-21 log established the original problem:

| Metric | Aug-21 evidence |
|---|---:|
| Normal scan samples | 2,646 |
| Median / p95 / p99 normal scan latency | 274 / 2,287 / 3,779 ms |
| Maximum normal latency | 8,460 ms |
| High-latency events above 10s | 16 |
| Worst high-latency event | 41,169 ms |
| Dashboard updater timeouts | 29 |
| Per-index live-scan timeouts | 7 |
| Bad-market-data/divergence events | 8 |

The index badges no longer depend on that full scan/dashboard updater:

- `/api/quotes` is local/token protected, cache-only, lock-bounded and performs no broker REST or scanner-loop work.
- The frontend polls it independently every 500ms and merges only quote fields.
- All four quotes carry source, exchange age, receipt age, effective age and session label.
- A live-session quote older than three seconds renders `STALE`.
- `HISTORICAL` or market `CLOSED` has precedence over a cached per-quote `LIVE` value.
- The global transport pill says blue `RUNNING`, not green `LIVE`.

Final runtime measurement over 50 authenticated local requests:

| `/api/quotes` latency | Result |
|---|---:|
| Median | 2.20 ms |
| p95 | 6.29 ms |
| Maximum | 32.80 ms |
| Four-index completeness | 50/50 requests |

Because the exchange was closed, all four final cards correctly rendered `HISTORICAL·10h`; none presented a stale reference price as live. This proves the delay isolation and closed-session semantics, but not the next open session's exchange-to-screen age distribution.

### 5. Contract selection and entry execution

- REAL Auto option entries require a fresh usable touch book.
- Bid/ask structure is validated and wide spreads are rejected by percentage/tick ceilings.
- Measured OI, volume and depth face configurable lot-scaled floors after contract selection.
- Missing values remain distinct from measured zero.
- Kite uses a bounded marketable LIMIT based on the fresh ask/bid instead of a zero-price option order.
- Option scale-ins require fresh, non-wide touch data and broker touch-quote support; the previous option `price=0.0` add path is removed.
- Kite's execution book is checked before any data-broker fallback.

One low-severity observability limitation remains: asynchronous `scale_in_trade()` can return `submitted` before its worker refuses a bad quote. The refusal sends no order and leaves the existing position unchanged, but the immediate caller does not yet receive the final refusal synchronously.

### 6. Trade management from entry through exit

- Entry provenance, price-action verdict, frozen intelligence score, strategy revision and strategy direction now survive candidate-to-trade hand-off on every open route.
- `entry_submitted_at` is stamped before the first broker call and the first timestamp survives retry/ambiguous acknowledgement handling.
- A synthetic/unverified option mark can provide display continuity only. It cannot ratchet peaks, P&L state, elective profit locks or intelligence exits.
- Hard protective stop/trailing, forced risk, EOD and operator exits remain allowed even if the display mark is unavailable.
- Broker protection is retained while an unverified option quote is skipped.
- The EOD safety clock is monotonic: stale candles can never move it behind trusted IST wall time and postpone the 15:15 square-off.
- Signal-ledger reconciliation/EOD sweep occurs before the TradeManager square-off; per-position failures do not suppress orphan handling.

### 7. Broker reconciliation, realized P&L and persistence

- Same-day restart restores Kite cumulative realized baselines from `broker_realised_cumulative`.
- Legacy rows use the controlled `pnl + charges` fallback; prior-day and ghost rows are excluded.
- SQLite uses WAL, `synchronous=FULL`, a 5s busy timeout and foreign keys.
- Persistence errors roll back, raise and enter a retry/dirty loop instead of being logged as successful.
- Invalid persisted OPEN rows are quarantined rather than revived into exposure.

The final service restart was performed only after confirming HISTORICAL mode, market CLOSED, Auto OFF and zero open trades. The restarted service returned the same safe state with all four indices.

### 8. Pre-market and Auto behavior

REAL Auto cannot enable until all active indices have usable option chains and a complete current-day morning summary. Daily Kite login, a fresh Fyers token and chain warmup are hard prerequisites.

Auto is intentionally runtime-only:

- every process start begins with Auto OFF;
- the pre-market worker may prepare REAL/manual feeds but explicitly does not enable Auto; and
- the watchdog warns when REAL is running with Auto OFF.

This prevents an unattended restart from silently resuming order entry. It also means the system cannot be called fully restart-unattended Auto-ready without changing a deliberate safety policy. This audit did not relax it.

### 9. Post-market archive and replay coverage

The archive now fails closed unless it has all 12 index/timeframe spot files and every traded option identity for the session.

Fixed scheduler faults:

- list and legacy numeric missing-contract manifest schemas are normalized;
- every due incomplete ledger gets a fair retry instead of the newest failure starving the backlog;
- historical requests are anchored to the ledger date, not process-now;
- a startup-deferred data provider causes a short retry of the same newest date and does not consume its retry slot; and
- a successful child is accepted only after manifest coverage verification.

The final restart proved the repair against the real cache/provider: it waited for initialization, fetched 13 missing Aug-21 contract histories, and verified 15/15 traded contracts plus 12/12 spot files with zero coverage errors. Manifests for Aug 17–21 are now complete.

Older archive gaps remain. Some Aug-14-and-earlier expired instrument tokens now return `invalid token`; rotation continues, but those historical premiums may be irrecoverable from the current broker API. This limits old replay coverage, not current live-order safety.

### 10. Dashboard/browser/accessibility

- All four index cards and every 1m/5m/15m selection activated and rendered chart canvases.
- Trade entry/exit timestamps render as one valid time plus one complete date.
- Browser page errors: zero.
- Axe 4.12.1 WCAG 2 A/AA violations: zero.
- One axe check remains incomplete because gradient backgrounds prevent automated color-contrast calculation.
- Two non-fatal chart preload warnings remain in the console; the vendored chart asset loaded HTTP 200 and all charts rendered.

Evidence: `dogfood-output/report.md` and `dogfood-output/screenshots/final-dashboard-1920.png`.

### 11. Host/system management

At the final check:

- host memory use was 78.23% (3.42 GB free of 15.71 GB);
- the listener working set was about 1.0 GB;
- drive C was 93.34% used with 30.79 GB free;
- project logs were about 1.19 GB and `data_store` about 0.34 GB; and
- the post-restart observation window had 0 dashboard timeouts, 0 scans above 10s, 0 live-scan timeouts, 0 ERROR/CRITICAL log records, and one verified archive.

The disk percentage is an operational warning. The project itself accounts for only about 2.45 GB, so deleting unrelated host data was outside scope and was not attempted. Before unattended use, bring the volume below the application's pressure threshold (preferably below 85% used or at least 50 GB free).

## Final live-market certification gate

Before enabling Auto at the next market open:

1. Complete daily Kite and Fyers authentication.
2. Confirm all four current option chains and the complete current-day morning summary.
3. Keep Auto OFF for a 60–90 minute REAL/data-only soak.
4. Require all four dashboard quote ages below 3s for at least 99% of observations, with no false `LIVE` badge.
5. Require zero dashboard-updater timeouts, zero live-scan timeouts and zero scan cycles above 10s during the soak.
6. Confirm broker positions/orders and the internal open-trade ledger reconcile to zero before arming.
7. Free host disk space or explicitly accept the storage-pressure warning.
8. Have the operator manually enable Auto only after those checks pass.

A guarded smallest-lot live order/fill/protective-stop/exit test would require explicit account-mutation authorization and was not performed here.

## Safety statement

This audit made **no live order placement, modification, cancellation or forced account action**. Broker calls used for validation were connectivity, quote, option-chain and historical-data reads only.
