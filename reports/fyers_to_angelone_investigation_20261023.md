# FYERS to Angel One: feature-preservation investigation

Date: 23 October 2026. Scope: the current working tree, including existing uncommitted changes. This is an investigation and migration design, not an implemented migration. No application code, broker configuration, credentials, or live orders were changed. No authenticated broker probes were run.

## Decision

Replacing FYERS with the existing Angel One adapter is not feature-equivalent. Angel One can cover many underlying data capabilities, but the application currently exposes only four index REST quotes and five near-ATM option pairs through that adapter. The largest gap is synthetic constituent-share index volume. A full replacement requires an aggregation service and measured parity, not a credential or priority change.

There is also an unavoidable resilience difference: Kite + Angel One has two independent broker feeds; Kite + FYERS + Angel One has three. Preserving the current number of independent failure domains requires another independent source. A second connection to Angel One does not replace an independent provider.

## What FYERS actually supplies

| Capability | Current implementation and consumers | Replacement requirements |
|---|---|---|
| Secondary live index prices | `data/market_data.py:1192` streams NIFTY, BANKNIFTY, MIDCPNIFTY and SENSEX. `:2874` arbitrates Kite versus FYERS. `:3955` supplies REST cross-check/fallback. | Add an Angel One stream, normalize prices and exchange clocks, and preserve Kite-first arbitration and divergence detection. Existing Angel One REST is a partial substitute only. |
| Index candle history | `data/market_data.py:5366`, particularly `:5516`, prioritizes FYERS for index OHLCV. Cache, backfill and scanner histories depend on this route. | Add a supported Angel One historical adapter with Angel tokens, date-range chunking, timezone normalization, session filtering and completed-bar handling. The existing legacy history method is inert. |
| Synthetic cash index volume | `:1256` onward carries FYERS cumulative volume into index ticks. `:3158` explicitly excludes futures volume from index candles. `:3561` reports cash-volume activity. `:5336` labels historical volume `fyers_index`. | Rebuild the same constituent-share quantity from individual cash-stock data, or retain an equivalent external source. Do not substitute index futures contracts, option volume, ETFs, turnover or zero. |
| Volume-based intelligence | `intelligence/volume_profiler.py:338` onward uses measured volume for availability, VWAP, relative volume and surge evidence. `intelligence/order_flow.py:57` checks volume basis. `scanner.py:10483` onward has FYERS-specific evidence checks. | Preserve quantity, interval coverage, freshness, source changes and provenance. A renamed field or neutral fallback cannot establish feature parity. |
| Broad active-expiry option chain | `data/market_data.py:6054` requests FYERS chain with `strikecount=30`, aligns expiry, and constructs CE/PE premium, OI, volume and OI-change rows. PCR, OI analysis, strike selection and related intelligence consume this frame. Kite overlays OI when needed (`:5965`). | Build the same strike universe from the execution contract master and Angel One FULL quotes. Preserve expiry, symbols, provider-specific tokens, OI units and timestamps. Measure actual returned strike coverage; do not assume request count equals returned pair count. |
| Option OI changes | FYERS `oich` enters chain rows. Provider/expiry-aware snapshot logic starts at `data/market_data.py:6480`. | Preserve the distinction between reported day change and measured interval delta. Warm up snapshots and retain reset/provider/expiry guards. Do not present first-observation zero as measured no-change. |
| Option premium fallback | `scanner.py:13190` calls a direct FYERS chain reader. `data/market_data.py:5928` rejects cached chains unless metadata source is FYERS. | Make fallback accept an exact, fresh, verified Angel One contract/expiry through a provider-neutral interface. Keep execution-book authority with Kite. An Angel chain in the general cache currently does not preserve this path. |
| Supplemental touch quotes | `data/market_data.py:4827`, including `:4910` onward, can fall back to FYERS quotes when a usable Kite book is unavailable and a FYERS symbol can be resolved. | Normalize Angel One FULL depth and timestamps if preserving this optional fallback. Do not override a definitive unusable Kite execution book with a more attractive secondary quote. |
| India VIX | `FYERS_AUX_SYMBOLS` at `data/market_data.py:77`; FYERS websocket subscribes auxiliary VIX without making it a tradable index candle. | Add and independently verify Angel One India VIX identity/token and REST/stream support. Current Angel adapter target map includes only four tradable indices. |
| Previous close, changes and pre-open references | `data/market_data.py:649`, `:4240`, `:5157` and related quote caches use FYERS reference values. | Preserve previous-close semantics, pre-open settlement behavior, holiday behavior and market-session labels. |
| Operational health and recovery | `data/market_source_health.py:10`, `main.py:800`, `engine/scanner_dashboard.py:121`, `dashboard/server.py:3416` and dashboard JavaScript expose FYERS status, login and recovery. Authentication/refresh/reconnect logic resides in `data/market_data.py`. | Add Angel session, stream, REST, history, volume and chain health. Update readiness/watchdog alerts, dashboard controls, source labels and diagnostic scripts. Do not treat successful login as proof of fresh data. |

FYERS does not own live order execution here. `trading/broker.py` states live execution is Zerodha Kite. Angel One is currently a read-only data fallback, not an automatic execution failover.

## What already works through Angel One

- `data/angelone_market_data.py:89`: password/TOTP session creation, bounded transport timeout, sanitized SDK logging and limited authentication retry.
- `:238`: REST spot quotes for the four indices, including OHLC/previous-close fields. The adapter explicitly reports index volume unavailable and exchange timestamp unknown.
- `:191` and `:288`: daily filtered option master and independent Angel token resolution from Kite contract identities.
- `:333`: one FULL request for the prepared option legs. The current caller requests ATM and two strikes on either side, yielding at most five CE/PE pairs.
- `data/market_data.py:6386`: option OI snapshot deltas and active-expiry metadata. At least three usable paired-OI rows can pass current certification, but that does not make five rows equivalent to the broader FYERS chain.
- `:1856`: asynchronous fallback prewarm.

No Angel One websocket starts in this adapter. `MarketDataProvider.smart_api` is explicitly set to `None` (`data/market_data.py:300`). `get_historical_candles_angel` returns empty without it (`:5244`). Restoring that broad legacy field is not a safe shortcut: it also re-enables retired branches with old token, connection and logging assumptions.

Aggregate source health also needs functional changes, not just new labels. `data/market_source_health.py:76` computes total broker-data unavailability from Kite and FYERS only. Angel adapter health is appended separately by `MarketDataProvider.get_source_health`; it does not participate in that aggregate calculation. Retiring FYERS without updating this logic can report an outage despite working Angel quotes.

The local Greeks engine already derives Greeks and can infer IV from real option premiums (`intelligence/intelligence_aggregator.py:81`). Neither current chain normalization requires broker Greeks. Angel One's separate Greeks endpoint is optional; adding it is not a prerequisite merely because FULL quotes omit Greeks.

## External capability evidence

1. [FYERS index-volume calculation](https://support.fyers.in/portal/en/kb/articles/how-is-the-volume-for-indices-calculated-in-fyers): sums traded shares across index constituents for the same interval. Membership changes apply from their effective dates. This is neither futures volume nor turnover. Published availability begins 1 July 2025.
2. [Angel One SmartAPI documentation](https://smartapi.angelone.in/docs): documents FULL quotes, historical candles and streaming. Indexed documentation states 50 symbols per market-data request at 1 request/second; historical candles at 3 requests/second, 150/minute and 5,000/hour. Websocket documentation lists three concurrent connections and 1,000 subscriptions per session. Direct document access returned an authorization page during this investigation, so these limits require confirmation against account-accessible documentation before implementation.
3. [Official Angel One websocket SDK](https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartWebSocketV2.py): exposes exchange timestamps, traded volume and SNAP_QUOTE open interest. This proves protocol fields exist, not that every index or exchange supplies each field.
4. [Official Angel One REST SDK](https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartConnect.py): exposes historical candles, OI history, FULL market data and option Greeks.

The repository's legacy historical limiter defaults to 180/minute (`trading/api_rate_limiter.py:179`), which differs from the currently indexed 150/minute limit. Reconcile before enabling new history traffic. Existing Angel option quote calls also need one shared per-client market-data budget across indices and workers; retry sleeps are not a global limiter.

## Design for preserving constituent volume

This is a proposed solution, not proven live equivalence:

1. Maintain effective-dated constituent membership for each index. Resolve NSE cash constituents for NSE indices and the correct BSE cash instruments for SENSEX. Deduplicate subscriptions across overlapping indices.
2. Subscribe constituents through Angel One. Keep cumulative traded shares, exchange timestamps, receipt timestamps and day/session identity per stock.
3. Calculate each interval's constituent share volume and sum only matching intervals. Preserve genuine zero trades; distinguish a missing stock packet from zero trades. A reconnect or late startup needs historical recovery, not interpolation or a fabricated full minute.
4. Backfill constituents through the historical API and aggregate the same 1-minute intervals before resampling to 5 and 15 minutes. Historical membership must match the historical day. Budget recovery work so it cannot starve live quotes or chain refresh.
5. Preserve source and quantity separately. Use an explicit constituent-shares basis with provider, membership version, interval completeness and timestamps. Replace checks hard-coded to `fyers_index` with tested capability/quantity checks. Never relabel Angel data as FYERS merely to pass guards.
6. Keep existing FYERS cache data intact and retain its provenance. Do not overwrite real cached volume with empty Angel index-volume fields. Allow cross-provider baselines only after measured equivalence; otherwise warm up a homogeneous baseline.

Exact FYERS parity still depends on matching constituent lists, exchange volume semantics, corrections and delivery timing. Public API field availability alone does not establish this. Native Angel index-volume aggregation equivalent to FYERS was not established by this investigation.

## Migration sequence and acceptance gates

1. Add a dedicated read-only Angel market-data adapter for streaming, history, depth and auxiliary instruments. Reuse the current safe login and token-resolution code. Keep Kite as execution authority and retain all entry/exit safety gates.
2. Add a selectable supplementary provider. Keep FYERS available for comparison and rollback while implementing Angel coverage. Update arbitration, health and cached premium interfaces before changing defaults.
3. Expand Angel option chains to match the existing FYERS strike universe and active expiry. Batch at most the confirmed broker limit; for N strike pairs, schedule at least `ceil(2*N/50)` FULL requests. Include all indices in the same quota budget. Keep OI snapshots, archives, freshness and exact-contract premium fallback working.
4. Implement constituent volume as a separate aggregation component and connect it to history, candle construction and volume intelligence only when completeness and parity gates pass.
5. Compare both providers during market sessions. Cover opening minutes, reconnects, a process restart, pre-open, market close, a holiday, an expiry roll and all four indices. Record price/clock divergence, bar coverage, per-interval volume differences, chain breadth, OI units, PCR, strike choices, VIX, premium fallback and derived intelligence. Select tolerances explicitly; do not claim exact tick equality across vendor sampling.
6. Replay identical captured inputs through current and candidate routes. Compare 1/5/15-minute indicators, entry eligibility, selected contracts, trade management decisions and exit availability. Every difference must be attributable to a documented feed difference or a defect before cutover.
7. Fault-test individual stream, REST, history, token and instrument-master failures. Verify bounded workers, cancellation/recovery, source-switch resets, no fake freshness, no false zero volume and continued exits. Prove no Angel method can submit, modify or cancel orders through this data adapter.
8. Switch the supplementary default only after those gates pass. Roll back through the provider selector if they fail. Retire FYERS SDK, auth endpoints, UI and alerts only after all direct dependencies have a verified replacement.

Blocking cutover conditions: unverified constituent volume, narrower chain coverage, missing VIX, missing premium fallback, unverified rate budgets, or incorrect source health. Replacing volume with futures data and recalibrating the strategy would change behavior; it is not a feature-preserving replacement.

## Verification performed

Executed existing focused tests:

```text
python -m pytest -q tests/test_angelone_latency_futures_volume_2026_09_08.py tests/test_broker_source_failover.py tests/test_index_volume_fallback.py tests/test_india_vix_fyers_websocket_2026_09_24.py tests/test_fyers_volume_telemetry_2026_08_28.py
77 passed, 1 warning in 23.63s
```

The warning concerns pandas concatenation with empty/all-NA entries in `data/candle_builder.py:531`. These tests support the current adapter, fallback and volume-separation behavior. They do not prove live Angel One entitlements, account connectivity, current exchange coverage, data parity or post-migration behavior. The existing `scripts/verify_angelone_readonly.py` is obsolete: it imports a removed `SmartWebSocketV2` export, calls `connect(force_reauth=True)` although the current signature takes no argument, and expects the intentionally inert `smart_api`. Do not use it as migration acceptance evidence without repair.

The working tree already contains substantial unrelated modifications. This investigation adds only this report; it does not revert or alter those changes.
