# UT SMART EXIT repair verification — 30 September 2026

UTS correctness and workflow repairs are implemented. The default full suite passes **7,333 tests across 522 files**. After the final legacy-recovery correction, **584 persistence and entry-timing tests pass**. The running REAL Auto process has not been restarted and still needs to load the updated code.

## Repairs

| Failure | Corrected behavior |
|---|---|
| Closed final candle disappeared until the next candle arrived | Candle open time plus timeframe determines closure. An explicit replay clock is supported. Main and shadow caches recompute at closure even when OHLC is unchanged. |
| Error, stale or unrelated results produced repaint exits | Repaint requires fresh evidence from the matching instrument/timeframe, with the source candle covered and evaluated in its forming or closed state. Unknown results defer the decision. Elapsed time alone never confirms an intrabar trade. |
| General intrabar toggle admitted provisional UTS entries | UTS requires confirmed candles by default. Provisional entries require both the separate UTS opt-in and the general intrabar toggle. |
| Recorded quality rejection remained advisory | Canonical quality and operator grade preferences bind UTS entries, including delayed manual admission and product-routing rechecks. Entry-quality checks do not delay UTS exits. S1 retains its existing selection policy. |
| Premium stop could not cover the UT index stop | The final UTS option-entry invariant rejects capped premium risk smaller than absolute delta times the underlying stop distance, allowing one premium tick of tolerance. The same invariant runs at submission and actual fill. Existing recovery exposure remains exempt. Premium-loss caps are not widened. |
| Smart Exit waited for index ATR run-up while option gains disappeared | A shared option profit lock arms at 1R of original premium risk and retains 70% of the observed peak gain. It can protect gains before the chart exit arms. Stops only ratchet tighter; gap fills can still exceed a protection level. |
| Signal-candle extremes created profit before entry | Chart peak/trough begin at the entry close. Earlier highs/lows from that candle are not owned by the trade. |
| Restart lost confirmation or entry timing | SQLite persists `entered_intracandle` and `uts_confirmed`; the existing premium-peak field persists the lock. Session rows retain the same state. Legacy UTS rows with missing timing infer forming/closed entry from actual fill time relative to source-bar closure when those timestamps are available. Unknown timing is not treated as positive confirmation. |
| Replay used fixed targets, estimated premiums or different exits | UTS replay uses observed option OHLC and the same premium lock. The nominal RR target remains metadata, with no resting take-profit. Stops are re-anchored to the observed option entry rather than the provisional premium estimate. Chart exits execute only after their bars close. Missing or incomplete option paths and incompatible stops are rejected instead of credited with synthetic P&L. |

## Actual loss case

The archived NIFTY 22700 CE fill on 30 September had an entry premium of ₹203.80 and a stop of ₹183.55: **₹20.25 premium risk**. Its underlying stop distance was 103.1351 points and recorded delta was 0.5375, mapping to **₹55.44 premium risk**. The new final policy rejects that exposure. The probe reconstructs nominal targets only to isolate this stop invariant; it does not recreate an entire entry verdict or claim an avoided profit/loss. Existing-position recovery is still allowed.

## Verification

- Full repository-default suite: **7,333 passed**, no failed shards. Repository defaults exclude tests marked `integration`; no live broker tests were run.
- UTS/S1/option-stop focused suite: **228 passed**.
- Exit authority and manual/quote/mixed-book checks: **172 passed** after limiting entry-quality checks to entries.
- UTS and recovery checks after the final legacy-row correction: **238 passed**.
- All 29 test files containing persistence/entry-timing call sites: **584 passed** after that final correction.
- New UTS regression file contains **16 tests**, including a closed final candle without a successor, both cache boundaries, stale/error evidence, binding quality, capped stop coverage, option-lock timing, replay abstention, nominal-target behavior and SQLite recovery.
- Modified Python files parse successfully; `node --check dashboard/static/js/dashboard-settings.js` passes. Dashboard field mapping tests cover the new controls.

The first broad run identified nine older assertions expecting premium stops wider than the existing 5/10/15% policy and one incomplete scanner facade. Those tests now express the existing policy and supply the facade dependency; production premium caps were not relaxed. Source hashes prove the relevant stop-policy functions were unchanged by this repair. An intermediate run was stopped when replay exposed the estimated-premium conversion defect. The verified full-run artifact supersedes those intermediate outputs.

## Observed-price replay and practical limits

The isolated production replay uses a **15-day configured lookback**, copied candle/option stores, a separate database and blocked network access. It makes **zero network attempts**. The available data admits **zero trades**: 42 paired candidates lack a complete observed path or compatible stop, 58 lack observed option prices, and four exceed the entry cutoff. These are candidate diagnostics, not counts of filled trades or complete market coverage.

Zero admitted trades establish abstention behavior, **not profitable performance**. Current UT settings and archived ATM contracts have not demonstrated a safe executable portfolio under the present premium caps. Previous fixed-delta studies remain research evidence and must not be presented as validation of this corrected option workflow. Profitability requires a separate evaluation with sufficient observed contracts and untouched sessions; no settings were promoted on synthetic gains.

One-minute OHLC cannot prove intrabar tick ordering. Replay gives an already-resting stop priority, fills gaps at the adverse open, observes premium peaks at completed closes, and applies a newly tightened stop to the next minute. Live tick results can differ. Fees and broker slippage still affect net results.

## Activation

Read-only runtime inspection found **REAL mode, Auto enabled, UT SMART EXIT selected**, and the new UTS setting absent from the running configuration. The running app has not loaded the fixes. **Restart with the updated source is required** to activate the default confirmed-candle mode, quality gate, stop-coverage check and new profit lock. This task made no broker-order changes, settings API writes or live-process restart.

The source checkout already contained substantial unrelated changes. They were preserved; the task-specific patch compares source with the pre-repair snapshot rather than Git HEAD. That patch covers backed-up source and test files; additional changed dashboard/UTS fixture files are identified by the final source hash manifest.

## Evidence

- [Verification JSON](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_fix_verification_20260930.json)
- [Full-suite summary](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_fix_full_suite_verified_20260930/summary.json)
- [Final persistence tests](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_fix_persistence_verified_20260930.xml)
- [Observed-price production replay](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_fixed_replay_20260930_verified.json)
- [Actual-fill probe](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_fix_probes_20260930.json)
- [Task-specific patch](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_fix_changes_20260930.patch)
- [Source hashes](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_fix_source_sha256_20260930.json)

```powershell
.\venv\Scripts\python.exe -B reports/ut_smart_fix_probes_20260930.py
.\venv\Scripts\python.exe -B -m pytest -q tests/test_ut_smart_loss_repairs_2026_09_30.py
```

The replay harness requires a new report filename and refuses to delete prior outputs. Its output and workspace must remain inside this repository's `reports` directory. Archived pre-fix audit artifacts are preserved as historical evidence.
