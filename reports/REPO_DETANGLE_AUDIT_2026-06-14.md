# UT1 Repository Detangle Audit - 2026-06-14

## Scope

Reviewed the primary runtime path and recent patch surfaces around:

- `main.py`
- `scanner.py`
- `dashboard/server.py`
- `dashboard/static/js/app.js`
- `data/market_data.py`
- `data/market_calendar.py`
- `engine/recalculation_queue.py`
- `engine/recalculation_worker.py`
- `engine/scanner_recalculation.py`
- `engine/scanner_telemetry.py`
- `engine/option_history_store.py`
- `engine/expiry_manager.py`
- `trading/trade_manager.py`
- `trading/order_retry.py`

The audit separated three categories:

1. Patch logic already integrated into the real engines.
2. Patch/reference artifacts that should not be versioned as active source.
3. Runtime files that make the repository dirty after normal scanner use.

## Executive Summary

The important recent fixes are integrated into the main system path, not sitting as side patches:

- Backtest recalculation now flows through `RecalculationQueue`, `ProcessRecalculationWorker`, and `ScannerRecalculationMixin`.
- Scanner startup still intentionally hydrates one day only, while user-selected larger windows are handled by explicit recalculation.
- Dashboard trade pagination and 2000-row cap are implemented through `/api/trades` and frontend paging logic.
- Broker failover semantics live in `data/market_data.py` and are covered by source-health tests.
- Broker order retry behavior is centralized in `trading/order_retry.py`.
- External chart CDN dependency has been replaced by a local Lightweight Charts bundle.
- Startup launcher hardening is directly integrated into `start.bat`.

The biggest repository hygiene problem was not current source logic. It was Git tracking mutable runtime state:

- 494 candle CSV cache files under `data_store/candles/`
- generated missed/rejected signal reports under `data_store/reports/`
- warm memory under `data_store/warm_memory/`
- `fyers_token.json`
- obsolete root-level scanner snapshots and an obsolete market-data sidecar

Those files are now treated as local runtime/reference artifacts instead of active source.

## Cleanup Applied

Updated `.gitignore` to ignore:

- `fyers_token.json`
- `runtime_*.log`
- `data_store/candles/`
- `data_store/recalculation_jobs/`
- `data_store/reports/`
- `data_store/warm_memory/`
- `scanner_old.py`
- `scanner_frictionless.py`
- `data/market_data.py_fixed_func.py`

Removed the same files from Git tracking with `git rm --cached`, preserving the local copies on disk. This means:

- local backtest/runtime data is still available to the running app;
- Git no longer treats normal scanner activity as source edits;
- GitHub no longer needs to carry token/runtime cache churn;
- old patch reference files remain available locally but are no longer active repo source.

## Patch Integration Status

### Integrated Cleanly

`trading/order_retry.py`

- Order retries are centralized in one helper.
- Tests cover five short retries plus two 10-second retries.
- This is correctly integrated and should stay as a small dedicated module.

`engine/recalculation_queue.py`

- Expensive historical rebuild requests are single-flight and latest-wins.
- This is the correct abstraction boundary for settings-triggered recalculation.

`engine/recalculation_worker.py`

- Historical cache warming is process-isolated.
- The worker uses `MarketDataProvider(start_streams=False)` and writes a manifest as the truth source.
- This patch is integrated and should remain separate from `scanner.py`.

`engine/scanner_recalculation.py`

- Historical rebuild orchestration has been extracted from `scanner.py`.
- This is a good partial detangle and should be expanded over time.

`engine/scanner_telemetry.py`

- Runtime diagnostics, source freshness, dashboard truth, and telemetry were extracted from `scanner.py`.
- This is also a good partial detangle.

`engine/option_history_store.py`

- The Parquet bundle and miss ledger are centralized.
- This is the right location for option-history cache acceleration.

`data/market_data.py`

- Broker-source health, Yahoo fallback, and entry-eligibility semantics are in the real provider path.
- Tests cover broker availability and Yahoo idle behavior.

`dashboard/server.py`

- `/api/trades` pagination, stale historical-cache rejection, and diagnostics truth checks are in the backend API path.
- This is integrated, though the file is now large enough to deserve future extraction.

### Integrated But Still Tangled

`scanner.py`

- Still the largest source file by far.
- Current size is roughly 8.7k to 9.5k lines depending on line-ending counting.
- It still owns scanning, historical loops, signal construction, dashboard payload assembly, trade execution handoff, candidate accounting, and compatibility guards.
- The recent mixin extraction was valuable but incomplete.

Recommended next extractions:

- Move dashboard payload shaping from `scanner.py` into `engine/scanner_dashboard.py`.
- Move historical candidate/trade row reconstruction into a dedicated historical-result builder.
- Move entry/exit candidate normalization into `engine/signal_processor.py` or a smaller sibling module.
- Move compatibility/legacy row guards into one clearly named adapter module.

`dashboard/static/js/app.js`

- Roughly 3k lines.
- Trade paging, chart streaming, settings, state rendering, activity panel, and diagnostics are still in one browser script.
- Pagination is integrated and working, but frontend maintainability would improve if trade table, charts, and settings became separate modules.

`engine/signal_processor.py`

- Roughly 2.5k lines.
- It owns signal filtering, grading, trade generation, regime gates, option logic, and historical/live branching.
- It is functional, but it is the second most important candidate for gradual extraction after `scanner.py`.

`trading/trade_manager.py`

- Around 1.7k lines.
- Execution state improvements are integrated, but order-state reconciliation, dashboard payloads, exit retry handling, and accounting are still close together.
- Future cleanup should split broker execution state from dashboard serialization.

`data/market_data.py`

- Around 1.5k lines.
- Broker connection, history fetch, source failover, Yahoo emergency fallback, option history, and market calendar handling are all close together.
- It is still coherent enough to leave in place for now, but future extraction should separate live-source health from historical data loading.

## Repository Hygiene Findings

### Fixed In This Pass

Tracked mutable runtime files were the direct cause of repeated dirty status:

- `data_store/candles/**`
- `data_store/reports/**`
- `data_store/warm_memory/**`
- `fyers_token.json`

These are now ignored and untracked.

Obsolete patch/reference files were also removed from active Git tracking:

- `scanner_old.py`
- `scanner_frictionless.py`
- `data/market_data.py_fixed_func.py`

### Left Intact Deliberately

`restore_point_26/`

- A partial set of restore-point files is still tracked in Git, while the local restore folder contains more ignored files.
- This is tangled from a pure repository-design view.
- It was left intact because the restore point is an explicit user workflow and has been used as a verified recovery baseline.

Recommended future cleanup:

- Move restore snapshots outside the app repo, or
- package restore points as dated zip artifacts, or
- keep only `RESTORE_POINT_26_INFO.txt` and `RESTORE_POINT_26_MANIFEST.json` in Git while storing the full restore folder locally.

## Risk Notes

- Removing runtime caches from Git tracking does not delete local files.
- A fresh clone will not receive cached market candles from Git. That is correct for production hygiene, but first-run backtests may need to fetch or rebuild local history.
- `fyers_token.json` should never be committed because it is credential/runtime state.
- The large staged deletion count is expected because historical candle CSVs were previously versioned.

## Recommended Next Refactor Order

1. Split `scanner.py` dashboard payload and historical result shaping first.
2. Split `dashboard/static/js/app.js` into trade table, chart, settings, and diagnostics modules.
3. Split `engine/signal_processor.py` into filters, grading, and instrument-selection submodules.
4. Split `trading/trade_manager.py` execution reconciliation from dashboard serialization.
5. Decide the long-term restore-point policy so local restore snapshots and Git history do not fight each other.

## Verification Checklist

To consider this cleanup complete:

- `git status --short` should show only intentional source/report changes before commit.
- `pytest -q` should pass.
- `/api/status` should continue to report the scanner running and fresh.
- `/api/trades` should continue to serve paginated historical rows.
- Local runtime files under `data_store/` should still exist on disk after the Git cleanup.

