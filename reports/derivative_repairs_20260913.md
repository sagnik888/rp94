# Derivative decision and replay repairs — 13 September 2026

This bounded implementation connects eligible derivative evidence to the shared intelligence score. It does not establish profitability. Scanner replay timing, historical-chain wiring and full replay validation are owned by the main repair task.

## Behavior changed

- `intelligence/derivative_context.py` validates instrument, family, timeframe, actual interval coverage, exchange clock, freshness, provider, quantity unit and expiry before using a feature. Futures use one nearest-expiry contract; duplicate providers abstain. Options require matched CE/PE contracts at the same strike, expiry, provider, unit and observation window, with opposite premium moves. Cumulative option quantity and OI sign alone never establish direction.
- The aggregator applies the resulting 0.90–1.10 participation multiplier once to existing conviction. It cannot create or reverse a directional score. Ordinary 1x participation and unavailable evidence preserve the existing score. Zero measured activity is distinct from missing activity. No additional hard entry/exit gate was added; existing consumers of the shared aggregate see the adjusted score.
- OITracker rejects an unknown, day-reported, or poll-to-poll horizon as a requested 1/5/15-minute change. A complete matching cohort of exchange-measured option intervals can replace FYERS day fields. Current OI levels and interval deltas are replaced together from that cohort, preventing cross-provider differencing. Missing legs, providers, strike coverage or independent underlying-price confirmation abstain. OI's scalar score also becomes neutral when its directional signal abstains.
- The background chain refresh resolves up to eleven near-money CE/PE pairs from the existing local Kite contract master at the chain's exact expiry and registers both legs for FULL ticks. It is disabled for history-only providers. Execution-master and background expiry representations normalize to one identity so repeated registration cannot split a contract history.
- Futures, option types, strikes, expiry, provider and unit remain separate from cash-index candle volume. Five-second buckets retain their last actual observation, with its original exchange time and cumulative totals; values are not interpolated. High tick rates therefore cannot evict the entire fifteen-minute measurement window.

## Recorded-history interface

`MarketDataProvider.get_recorded_option_chain(instrument, as_of_epoch, expiry="")` returns `(DataFrame, quality_dict)`. Only an actual recorded same-session response at or before the requested clock, no older than 120 seconds, can be returned. Original identity and quality remain attached. Missing data returns an empty frame and `intelligence_usable=False`.

`get_derivative_participation(instrument, timeframe, as_of_epoch)` retains its public signature and can read recorded same-session observations. The archive is `data_store/derivative_observations/YYYY-MM-DD.jsonl`; tests route it under their isolated database directory. Tick handling does no disk I/O. Existing background futures and chain workers flush pending records under a separate writer lock. Pending records are bounded at 20,000 and an overflow increments `archive_dropped_events`. Nonfinite values serialize as missing. Corrupt/truncated JSON lines do not become observations.

The archive starts with actual recorded observations; it cannot recreate missing historical OI, depth or participation for earlier backtests. FYERS/Angel receipt-timed chain intervals remain descriptive and do not masquerade as exchange-timed minute participation. Broker availability, actual subscriptions and feed density still need live observation after restart.

## Verification

Command: `venv/Scripts/python.exe -m pytest tests/test_derivative_decisions_20260913.py tests/test_remaining_audit_repairs_2026_09_11.py tests/test_intelligence_data_truth.py tests/test_broker_intelligence_audit_fixes_2026_09_11.py -q --disable-warnings --tb=short`

Result: **178 passed**, one pre-existing warning, 5.51 seconds.

New behavioral tests exercise all four indices × all three timeframes × both derivative families against the actual aggregator. They prove eligible participation changes the score without changing cash volume; invalid/mismatched/receipt/stale evidence abstains; options require matched opposing premium moves; neutral conviction stays neutral; provider duplicates cannot multiply votes; FYERS day fields can be superseded by a complete measured exchange cohort; ninety-second changes cannot impersonate five/fifteen-minute OI; persisted observations round-trip without future access; high-rate ticks retain fifteen-minute coverage; and a stalled archive write does not block tick collection.

No broker calls, orders, production restart or profit claim were made by these tests.
