# Replacing FYERS with Kite and Angel One together

Date: 23 October 2026. Investigation of the current working tree, including pre-existing changes. This report supplements the existing `fyers_to_angelone_investigation_20261023.md` with the combined Kite/Angel One design and the webhook distinction. No application code, credentials, broker settings, or live orders were changed. No authenticated broker probes were run.

## Finding

FYERS is a supplementary broker overall, but it is the preferred provider for index history and option chains. Removing it is not a priority-setting change. Kite and Angel One expose the building blocks for a replacement, but the current application does not connect all those building blocks. Feature parity is not yet established.

Webhooks notify order changes. They do not supply market prices, index volume, candle history, option chains, or VIX. The replacement requires market-data WebSockets, REST quotes, historical APIs, and local aggregation. Keep order updates on a separate responsibility boundary.

Angel One currently provides data fallback, not live execution fallback. `trading/broker.py:1` explicitly assigns live execution to Kite. An Angel One order update cannot confirm, cancel, or close an order held at Zerodha. Adding execution through Angel One would be a separate migration requiring order ownership, account-specific positions, margin, reconciliation, and duplicate-order protection.

## Feature mapping and application gaps

| FYERS responsibility today | Combined replacement | Required application work |
| --- | --- | --- |
| Four live indices: NIFTY, BANKNIFTY, MIDCPNIFTY, SENSEX | Kite stream first; Angel stream for independent comparison/fallback; bounded REST recovery | Extend `data/angelone_market_data.py:238` beyond REST. Preserve exchange timestamps, source freshness, divergence checks and Kite entry authority. |
| Index OHLCV history, preferred at `data/market_data.py:5516` | Kite `get_historical` (`data/kite_market_data.py:852`) for OHLC; Angel history for redundancy; separately reconstructed cash volume | Join identical completed intervals; preserve timezone, trading sessions, cache provenance, backfill and 1/5/15-minute resampling. Existing legacy Angel history returns empty while `smart_api` remains `None` (`data/market_data.py:301`, `:5244`). |
| Synthetic constituent-share index volume | Aggregate actual constituent cash-stock volumes using Kite and Angel data | New effective-dated membership and volume service. Neither direct index feed is an equivalent volume source. |
| Broad active-expiry option chains | Promote existing Kite chain builder (`data/kite_market_data.py:740`) to a complete route; build matching Angel chain for fallback/comparison | Pin execution expiry and the same strike universe. Existing Kite default is 15 strikes each side; FYERS requests `strikecount=30`, whose actual returned coverage must be measured. Existing Angel caller requests only five pairs (`data/market_data.py:6319`). Neither default proves parity. |
| Premiums, OI, option volume, PCR and strike-selection inputs | Exact-contract Kite quotes and Angel FULL/SNAP_QUOTE data | Preserve field units, timestamps, completeness, OI-provider identity and chain archives. Do not combine partial OI universes and call the result an equivalent PCR. |
| OI changes | Existing provider/expiry-aware snapshot store with Kite or Angel observations | Kite chain reports levels with zero placeholders and an explicit provenance marker. Feed levels through snapshot logic. Measured interval change is not FYERS-reported day change; retain this distinction and warmup/reset rules. |
| Exact option premium fallback | Provider-neutral cached/direct lookup backed by Kite and Angel | Replace FYERS-only acceptance in `data/market_data.py:5928` and direct reader at `scanner.py:13245`; preserve fresh contract, strike, side and expiry checks. |
| Supplemental bid/ask touch quotes | Existing Kite depth (`data/kite_market_data.py:608`) plus normalized Angel FULL depth | Replace FYERS fallback within `data/market_data.py:4827`. Retain execution-book safety: an unusable Kite book does not become executable because another broker shows a price. |
| India VIX | Resolve and subscribe a verified VIX instrument in Kite and/or Angel | Current FYERS auxiliary subscription is explicit (`data/market_data.py:77`). Current Angel index target map has only four indices. Verify account-accessible identities; do not hard-code an assumed token. |
| Previous close, pre-open references and price changes | Kite/Angel reference quotes | Preserve previous-session meaning, holidays, pre-open transitions and cached references. |
| Login, reconnect, readiness and dashboard diagnostics | Per-capability Kite/Angel health and session recovery | Update startup/watchdog, dashboard, source labels and diagnostics. `data/market_source_health.py:76` currently calculates total broker-data unavailability from Kite and FYERS only. Angel must participate correctly. |

Volume affects VWAP, relative volume, surges, volume profile, order-flow estimates, scanner evidence and downstream decisions. Source-name guards are also functional dependencies: `scanner.py:10483`, `:10509`, `:10892` and surrounding paths explicitly require `fyers_index`. A replacement must use verified quantity/completeness capabilities rather than pretending Angel or Kite data came from FYERS.

## Recommended responsibility split

1. **Kite:** retain execution ownership, execution instrument master, primary index/contract prices, execution depth, OHLC history, and the existing option-chain builder. Reuse existing code rather than adding another broker abstraction.
2. **Angel One:** add independent streaming, matching option-chain coverage, historical recovery, auxiliary quotes and comparison data through the dedicated read-only adapter. Preserve independent token resolution from exact contract identity.
3. **Volume service:** reconstruct constituent-share volume from either provider using the same versioned membership. Select one coherent provider observation per stock/interval; never sum duplicate Kite and Angel observations. Record coverage, provider, exchange time, received time and session identity.
4. **Order events:** retain Kite `on_order_update` (`data/market_data.py:2303`) and optional verified HTTP postback (`dashboard/server.py:3550`). Both must continue through the conservative, deduplicated handler (`trading/trade_manager.py:2183`) and broker-position reconciliation. Angel's order stream is unnecessary for replacing FYERS data in the present Kite-only execution design.

Kite's existing chain builder can substantially reduce new work, but requires completeness validation: it can emit a row when only one side has a quote (`data/kite_market_data.py:822`). Normalize missing sides explicitly and gate consumers on adequate paired coverage. Protect the shared API budget so chain/history expansion cannot starve execution reconciliation or exits.

## Volume preservation contract

For index I and interval t, compute the sum of exchange-reported cash-stock share volume for every constituent effective during that interval. Use the correct NSE cash instruments for NSE indices and BSE cash instruments for SENSEX. Membership is not supplied by a generic broker instrument master; obtain and version authoritative index membership separately.

Live cumulative volume differences require session baselines, exchange-clock alignment and reconnect recovery. Late startup cannot turn a day's cumulative total into the first minute's volume. Missing packets are unknown, not zero. Streaming snapshots can miss exact minute boundaries; use historical stock candles to reconcile completed intervals before claiming equivalence. A fully observed stock with no trades contributes genuine zero.

Backfill constituent candles with membership effective on each historical date. Preserve the existing FYERS cache and its provenance. Do not replace its volume with zero, futures contracts, option contracts, ETF shares, turnover, or tick counts. Keep futures participation as its existing separate feature. Cross-provider volume baselines require measured equivalence or a documented warmup.

Matching the published formula does not prove matching bars: constituent membership, venue, corrections, packet sampling and publication timing all matter. Exact historical and live parity remains an acceptance condition.

## Reversible migration and acceptance

1. Add a supplementary-provider selector and capability-based interfaces while FYERS remains available for comparison and rollback.
2. Wire Kite chains/history as complete routes. Expand Angel streaming, history, chain batching, depth and VIX. Share endpoint budgets across workers; verify current account limits before enabling traffic.
3. Implement and validate the volume service; replace source-name guards without weakening evidence or freshness requirements.
4. Compare all four indices across opening minutes, normal sessions, reconnects, a restart, pre-open, close, holidays and expiry changes. Measure OHLC coverage, volume completeness/differences, chain universe, OI units/deltas, PCR, selected contracts, VIX and fallback premiums.
5. Replay identical captured inputs through both implementations. Compare indicators, entry gates, selected contracts, trade management and exits. Define tolerances before cutover; explain every material difference.
6. Fault-test stream, REST, history, session and instrument-master failures independently. Verify no invented freshness, no zero-filled missing volume, bounded workers and continued exit management. Retain Kite-only entry authority unless a separate policy change is intended.
7. Switch only after parity gates pass. Retain FYERS rollback until a monitored validation period completes; then remove its SDK, login routes, settings and operational references.

There is an unavoidable resilience change: three independent broker sources become two. Extra WebSockets or webhooks do not restore the third provider failure domain. Strict preservation of that resilience requires another independent source or retaining FYERS. Do not describe a two-provider migration as preserving literally every existing property.

## External verification

- [Kite WebSocket protocol](https://kite.trade/docs/connect/v3/websocket/): market quotes and order events share a connection. Tradable packets include volume/OI/depth; index packets omit volume. The documented capacity is 3,000 instruments per connection and three connections per API key.
- [Kite postbacks](https://kite.trade/docs/connect/v3/postbacks/): HTTP callbacks carry order-status changes for orders placed with the app API key. User-wide WebSocket order updates cover orders placed elsewhere on Kite as well. HTTP callbacks require checksum validation.
- [Kite quotes and instrument master](https://kite.trade/docs/connect/v3/market-quotes/): full quotes support up to 500 instruments per request; the existing chain builder conservatively chunks 250. Daily instrument snapshots are not live prices; derivative tokens can be reused.
- [Kite historical API](https://kite.trade/docs/connect/v3/historical/): supports OHLCV/OI intervals. Continuous history applies to daily NFO/MCX futures; it is not a substitute for expired option intraday archives. Preserve local archives and contract identities.
- [FYERS volume definition](https://support.fyers.in/portal/en/kb/articles/how-is-the-volume-for-indices-calculated-in-fyers): synthetic volume sums constituent share volumes for each interval using membership effective on that date.
- [Angel market-data SDK](https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartWebSocketV2.py): exposes exchange timestamps, daily traded volume, SNAP_QUOTE OI and depth. These fields do not establish native synthetic index volume or account entitlement.
- [Angel order-update SDK](https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartWebSocketOrderUpdate.py): a separate authenticated order stream exists. It is not the market-data stream and does not report Kite account orders. The inspected implementation disables TLS certificate verification; do not copy that transport setting if this optional feature is later added.
- [Angel documentation](https://smartapi.angelone.in/docs): search-indexed official content states 50 symbols per FULL market-data request at one request per second. Direct access returned an authorization page. Confirm quotas and exchange/account coverage through accessible official documentation before implementation. For N paired strikes, budget at least `ceil(2*N/50)` requests, across all indices and workers.

## Verification executed for this investigation

```text
venv/Scripts/python.exe -m pytest -q
  tests/test_angelone_latency_futures_volume_2026_09_08.py
  tests/test_broker_source_failover.py
  tests/test_index_volume_fallback.py
  tests/test_india_vix_fyers_websocket_2026_09_24.py
  tests/test_fyers_volume_telemetry_2026_08_28.py
  tests/test_kite_market_data.py
  tests/test_kite_postback_and_tagging.py

123 passed in 8.04s
```

These focused tests validate existing paths, not an implemented replacement. Live account connectivity, entitlement, volume equivalence, expanded quotas and market-session parity remain unverified. This investigation adds only this report and preserves pre-existing workspace changes.
