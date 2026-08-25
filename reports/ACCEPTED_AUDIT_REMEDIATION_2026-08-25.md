# Accepted audit remediation — 25 August 2026

## Outcome

The two audits agree on the most important descriptive finding: recent explicit-option results are not profitable, and most observed loss is concentrated in entries that never became positive. The safe, directly provable defects around that conclusion have been repaired. The evidence still does **not** prove that the admission/ranking problem is solved, that future trades will be profitable, or that unattended live Auto is certified.

No live order was placed, modified, or cancelled during this remediation.

## One-by-one disposition

| Audit item | Decision | Remediation / reason |
|---|---|---|
| Mixed-book routing replaces ATR-aware option risk with 15% | **Accepted — fixed** | A verified, coherent existing option stop/target/initial-stop tuple is preserved. Invalid, stale, remapped, or over-cap geometry is rebuilt. The chosen branch is persisted as `risk_geometry_source`. |
| Census counts stale self-signals and live/replay differ | **Accepted — fixed** | Historical/replay census receives the same day-type and confidence context as live. Confirmed trend-day context can outrank the stale census. |
| Blanket bearish stand-aside blocks the bearish shorts the census repair should admit | **Accepted — fixed conservatively** | The policy is default OFF and remains an explicit experiment. It was not retuned on the same small cohort that proposed it. |
| Peak trail was promoted from an inadequately small sample | **Accepted — fixed conservatively** | Peak trail is default OFF again; its conservative Rs.600 gate remains available for controlled evaluation. |
| Fifteen-second rupee hold could conflict with hard protection | **Accepted — tested** | Combined lifecycle coverage proves the secondary hold does not delay the hard software stop or broker protection. |
| Duplicate/ambiguous EXIT outcomes | **Accepted — fixed for new events** | Lifecycle identity is frozen before the first tracker write and carried into the managed Trade. A session exit request is recorded only as non-terminal `EXIT_DECISION`; the deterministic immutable `EXIT` is emitted from the TradeManager callback after final fill/reconciliation accounting. A lifecycle-ownership bridge covers the open-row-removal-to-callback interval, preventing an advisory race from taking the canonical terminal id. Legacy duplicate rows are retained as historical evidence, not rewritten. |
| Mixed PnL/path bases and lost runner provenance | **Accepted — fixed for new events** | EXIT rows declare PnL basis, path basis, round-trip cost, runner activation/stop provenance, and the runner-stop unit basis. Runner state is persisted through SQLite/restart. |
| Strategy/quote/risk provenance missing | **Accepted — fixed for new events** | Tracker rows now carry strategy revision/direction mode/data provenance, decision/entry/explicit-exit quote provenance, execution touch evidence, and ATR-preserve-versus-rebuild provenance. Missing exit evidence remains `null`; entry evidence is never relabeled as exit evidence. |
| Host OOM/resource exhaustion can kill management as well as entries | **Accepted — materially mitigated** | REAL Auto uses fresh machine-wide RAM and Windows `GetPerformanceInfo` commit readings, validates sample freshness, fails closed for new exposure, latches with hysteresis, and rechecks at the actual queued broker call/retry. Auto itself stays on so exits/reconciliation keep running. Scale-ins—including recovered-origin scale-ins—use the same forced final admission boundary. Immediate and delayed post-ACK guard refusals rejoin the original lifecycle and retain their precise source/reason in rejected-signal telemetry. Dashboard alert thresholds use the configured guard values. |
| One live engine/order authority per account | **Accepted — existing protection verified** | The Kite order-authority lease already serializes account execution. Resource telemetry/entry inhibition was added. A supervised restart drill remains outstanding. |
| Grade is just bucketed confidence; ADX discontinuity; top confidence underperforms | **Accepted diagnosis — no speculative live rewrite** | The current ranker is not promoted as calibrated. A replacement was deliberately not invented from the same mixed-basis, in-sample telemetry. It requires versioned executable option outcomes and rolling held-out validation first. |
| Rejected signals lack executable option outcomes; paths stop at exit | **Accepted — telemetry improved, full shadow book outstanding** | Contract, strategy, quote, basis, lifecycle, and terminal provenance are now durable. A continuous executable bid/ask shadow path for every accepted/rejected signal through post-exit/EOD remains to be built and collected prospectively. Existing one-minute OHLC remains bounded counterfactual evidence only. |
| Several settings are unreachable/no-ops | **Accepted as observability issue** | Inert-guard diagnostics identify settings that cannot bind. Thresholds were not changed merely to make them fire; that would alter live selection without outcome evidence. |

## Claims intentionally not implemented

These statements were either contradicted by the independent reproduction or too weak to justify changing live selection:

- no blanket rejection of confidence at or above 0.95;
- no broad removal of repaint/readiness guards;
- no claim that all intelligence pillars are collinear trend proxies;
- no assumption that A+ literally has the lowest win rate (C was slightly lower in the reproduced all-era table);
- no claim that the five-minute option stop is always 4%; the observed overwrite to 15% was the real defect;
- no exit loosening from the mislabeled Rs.287 “pre-green heat” statistic;
- no VOLATILE/ATR, RR-max, chop, or profit-lock threshold retuning solely to make currently inert branches activate; and
- no profitability claim from tests, historical marks, or connectivity checks.

## Reproduced evidence after remediation

The read-only audit still reproduces the historical dataset rather than pretending code changes rewrite history:

- through 21 August with latest EXIT retention: 327 outcomes, 74 never-green, -Rs.46,870.75, 59.135% of the loser pot;
- earliest EXIT retention: 73 never-green and -Rs.46,420.25;
- explicit-option cohort through 24 August: 211 outcomes, -Rs.9,791.53; never-green entries account for 63.852% of losses;
- confidence >=0.95: 25 explicit-option outcomes, two wins, -Rs.5,365.50; and
- 71 of 98 pathed lifecycles have continuous exact-contract one-minute OHLC through 15 minutes after exit, but not tick-order or executable bid/ask proof.

These numbers confirm the entry-admission diagnosis. They do not identify a validated replacement ranker.

## Verification

- complete repository suite after the final safety-review fixes: **5,034 passed, 12 skipped, 4 deselected, 0 failed** in 111.30 seconds;
- final targeted submission-guard/lifecycle/final-fill selection: **31 passed**;
- broader broker-reconciliation/persistence/unattended-safety selection: **125 passed**;
- independent final safety re-review: **APPROVE**, no remaining actionable P0-P2 findings, **56 focused checks passed**;
- final broker/resource/lifecycle focused selection: **65 passed**;
- lifecycle/exit/unattended-safety selection: **115 passed, 2 skipped**;
- the read-only historical audit script completed and reproduced the published cohort/sensitivity numbers; and
- production modules passed `py_compile` and the worktree passed `git diff --check` (line-ending notices only).

The final native Windows resource sample was valid: 16,088 MB physical RAM with 68.0% used, and a 34,520 MB machine commit limit with 47.1% used / 18,270 MB available.

The final read-only connectivity run at 05:41 IST was **not fully green**: Fyers authentication, websocket ticks, four-index history/volume alignment, and all four 61-strike option chains passed, but Kite rejected the cached daily token (`Incorrect api_key or access_token`). Therefore order-authority connectivity is currently unavailable until the operator completes the daily Kite login. The engine correctly has no basis for live Auto certification in that state.

## Readiness verdict

**REQUEST CHANGES / NOT YET CERTIFIED for unattended live Auto.**

The repaired engine is safer and its future records are substantially more auditable. The central economic question remains open: there is not yet a held-out, basis-consistent, executable-option result showing that the new admission process selects better trades after costs.

Required proof before certification:

1. collect versioned executable selected-option bid/ask shadow outcomes for accepted and rejected lifecycles, continuing at least 30 minutes and after real exit;
2. train/design the replacement admission ranker only on prior sessions and evaluate rolling held-out calibration, PnL after actual costs, drawdown, tail loss, and trade frequency;
3. run an open-market Auto-OFF soak across four indices and three timeframes, measuring exchange/receipt/screen age, scanner latency, feed divergence, RAM, and commit growth;
4. force-stop/restart with a reconciled position and prove recovery starts with new exposure inhibited while exits/protection remain active; and
5. only with explicit authorization, run the smallest-lot live canary through entry acknowledgement, partial/complete fill, protective stop, reconciliation, and exit.

The architecture remains Kite as order authority and Fyers as the secondary data/option-chain broker. It is not dual-broker order execution.
