# Remaining audit repairs — 12 September 2026

This implements the remaining reproducible defects from `postfix_verification_20260911.md`, with additional checks of the connected session and exit paths. The earlier verification report remains an unchanged record of the pre-repair findings.

## Implemented changes

| Boundary | Repaired behavior |
|---|---|
| Broker OI overlay | Requires the requested and returned expiry to match. An adapter without expiry support cannot apply an unpinned overlay. Overlay OI uses one provider, resets incompatible reported changes, and retains separate price/volume and OI provider identities. |
| Execution expiry | Chain fetches and execution share the effective expiry resolver, including gamma-window selection. Provider and scanner chain caches recheck explicitly recorded expiry at rollover. |
| Quote freshness | Futures fallback requires a current exchange clock. Receipt time is measured after the request, so a quote updated during the request is accepted. Missing, old and future clocks cannot refresh the fallback. |
| Current quality evidence | Current volume ratio, availability and basis are selected together. A measured zero survives. Tick activity cannot masquerade as traded volume. REAL candidates use the fresh exact-timeframe resolver and cannot recycle saved entry context. |
| Regime and breadth | REAL regime reads require source timestamps. Borrowed labels use the source timeframe's age. Stale peer labels do not create market-wide consensus. |
| Session history | Full-cache expansion is clipped to the requested analysis cutoff. Opening range follows the exchange clock; UTC and IST inputs agree. Prior daily-range references require complete sessions and disclose the number available. Rare-burst analysis uses effective session bias. |
| Futures minute estimates | Baselines compare observed volume rates with like coverage. Boundary increments are explicitly unallocated. A steady sampled rate gives a projected ratio of 1.0 instead of 1.3333. Incomplete coverage is disclosed and insufficient observations cannot be called reliable. |
| Candle accounting | Cross-boundary cumulative increments are retained as unallocated volume. The reproduced 400-unit increase is conserved as allocated plus unallocated quantity, without claiming exact minute allocation. Live partial bars disclose incompleteness. |
| Cache corrections | MTF cache identity covers the applicable input frame and relevant OHLCV/provenance fields, including corrections more than 20 bars back. |
| OI and PCR | Reported day OI changes cannot vote as short-timeframe changes. PCR history separates provider, expiry, covered strike universe and near-money scope, and deduplicates repeated observation IDs. |
| Flow and session votes | Cumulative CE/PE volume does not manufacture a directional flow vote. Invalid candle quantities are excluded; usable independent depth remains usable. Session VWAP/delta exclude proxy, mixed and explicitly incomplete quantities. |
| Stale/invalid exit evidence | Unavailable aggregate scores and unavailable corroborating primitives abstain. Volume measurements more than two bars behind the input edge cannot be reused as current surges. Their measurement timestamp and quantity remain diagnostic. |
| Exit confirmation | REAL peak/loss threat confirmation requires changed market observations as well as elapsed time. Repeated polling of the same observation does not earn confirmation. Unavailable confirmation state does not authorize an exit. |

## Separate derivative intervals

`data/derivative_participation.py` maintains independent contract/provider/clock/unit histories and produces 1-, 5- and 15-minute volume, OI and price changes, coverage, and comparable relative-volume rates. It resets on counter rollback or a new session, separates provider and contract changes, rejects future observations in as-of queries, and abstains when sampling cannot resolve the requested interval.

Futures snapshots and selected-option Kite FULL ticks feed exchange-timed histories. FYERS/Angel chain snapshots remain receipt-timed observations. Both are exposed through the intelligence bundle and current entry/trade contexts; quantities are never poured into index candle volume. Receipt-timed option observations remain descriptive, and sparse 90-second chain samples cannot produce eligible 1-minute features. Selected-option ticks can produce them after enough actual samples arrive.

This is an in-memory interval service with warmup after restart. It does not supply missing broker history or create a complete exchange-wide option universe. The new derivative interval fields are contextual analysis, not additional uncalibrated directional votes or replacement index-volume gates.

## Verification

**Final result: 6,025 passed, 4 deselected, 0 failures (588.86 seconds).** The run produced 12,072 warnings. Results are recorded in `remaining_fixes_verified_20260912.xml` and its matching log. The existing pytest configuration excludes four integration tests that use live APIs or slow historical backtests; this change adds no exclusions.

Command: `python -m pytest -q --disable-warnings --tb=short --junitxml=reports/remaining_fixes_verified_20260912.xml`.

The relevant source-file hashes are saved in `remaining_audit_repairs_20260912_sources.json`. `git diff --check` also passed.

Before the final run, the affected session/broker/new regression selection passed 101 tests. Exit confirmation and September-10 trade-management checks passed 120 tests. Dead-edge volume and new audit regression checks passed 115 tests at that stage. These selections overlap and must not be added together.

The final new audit regression file contains 86 passing cases. A separate production-method probe confirmed that a matching 22 September OI overlay copies all three requested strike pairs, retains Kite OI identity and leaves the input chain unchanged. Independent review also verified that committing a consumed contract tick cannot change confirmation identity; only a new market observation can advance it.

Old policy fixtures were updated to provide explicit fresh regime/volume observations. Older tests that treated three-bar-old volume as current were revised to verify that the measured quantity is retained while current scoring abstains. Separate new tests exercise missing/stale clocks, zero ratios, provider changes and incomplete coverage; production validity rules were not relaxed to preserve old expectations.

These are code and synthetic integration proofs. No new broker-connected entry-to-exit replay or profitability improvement has been established. The prior unprofitable holdout remains unverified by this repair pass. The code is implemented locally; this work does not assert that a running trading process has reloaded it.
