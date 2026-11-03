# Broker volume and derivative data audit — 2026-09-11

Read-only investigation of the current working tree. No product code, broker state, trading server, or production database was changed. The probes extract selected production methods with AST and feed them in-memory fake responses; they do not import the application or call a broker. Reproduction source: `reports/broker_volume_audit_20260911.py`; results: `reports/broker_volume_audit_20260911.json`.

## Data routing for the four configured indices

| Index | Fyers spot/history symbol | Kite spot symbol; derivatives segment | Angel spot exchange/name/token |
|---|---|---|---|
| NIFTY | NSE:NIFTY50-INDEX | NSE:NIFTY 50; NFO | NSE / NIFTY / 99926000 |
| BANKNIFTY | NSE:NIFTYBANK-INDEX | NSE:NIFTY BANK; NFO | NSE / BANKNIFTY / 99926009 |
| MIDCPNIFTY | NSE:MIDCPNIFTY-INDEX | NSE:NIFTY MID SELECT; NFO | NSE / MIDCPNIFTY / 99926074 |
| SENSEX | BSE:SENSEX-INDEX | BSE:SENSEX; BFO | BSE / SENSEX / 99919000 |

Mappings: `data/market_data.py:45`, `:57`, `:1889`, `:2281`; `data/angelone_market_data.py:23`; `data/kite_market_data.py:44`. Kite spot tokens are resolved from its instrument master; Angel numeric tokens are not reused for Kite. Angel option contracts translate the execution contract identity to an independently resolved daily Angel instrument-master token (`data/angelone_market_data.py:288`, `:345`). This is correct separation.

| Feature | Current primary path | Current fallback / limitation |
|---|---|---|
| Spot tick prices | Kite FULL websocket, arbitrated with Fyers | REST/display fallback includes Angel price-only quotes; price and exchange clocks are tracked for websocket arbitration |
| Cash index 1m/5m/15m OHLCV | Fyers History API (`data/market_data.py:5215`) | Legacy Angel history branch is inert because `smart_api=None`; local CSV then Yahoo price history. No Kite index-history fallback is wired in this method |
| Current forming cash volume | Fyers current 1m history row cached separately (`:1814`, `:5250`) | Valid only for the current minute; separately the system still expects cumulative index volume from WS/quotes |
| Futures volume/OI | Kite nearest unexpired FUT master row + REST full quote (`data/kite_market_data.py:673`) | No Fyers or Angel futures route; no persistent futures 1m/5m/15m OHLCV path in the orchestrator. A process-local 20-sample 1m baseline is made from cumulative snapshots |
| Option chain | Fyers active-expiry chain, normally a broad strike window | Missing OI can be overlaid from Kite by strike; Fyers failure invokes Angel up to five complete near-ATM CE/PE pairs |
| Option volume representation | Cumulative day volume per quoted CE/PE, same `call_volume`/`put_volume` columns from every adapter | No per-contract 1m/5m/15m volume-delta series is built by the option-chain path; consumers use the snapshots |
| Actual option history | Kite contract historical API (`data/market_data.py:5134`) | Existing local option CSV is returned before a broker request unless forced; unlike index history this is not Fyers-primary |

Cash volume, futures volume and options volume are economically different quantities. The upstream code correctly prevents futures cumulative volume from entering cash-index candle volume (`data/market_data.py:3040`, `:3114`). It does not, however, provide three equivalent interchangeable volume feeds. Futures raw broker quantities are labeled `contracts` without lot-size conversion (`data/kite_market_data.py:721`); vendor quantity semantics should be certified before exposing absolute values as lots/contracts. Same-contract dimensionless ratios do not depend on a fixed unit scale.

## Ranked concrete findings

### 1. P1 — The Fyers chain can analyze a different expiry from the execution contract in enabled gamma mode

Fyers chain alignment selects `expiry_manager.expiries[index]['active_weekly'/'active_monthly']` (`data/market_data.py:5743`). Those active expiries are deliberately rolled ahead at T-1/expiry (`engine/expiry_manager.py:293`). Execution token resolution independently overrides that active expiry to the current expiry during the gamma window (`engine/expiry_manager.py:661`). The chain metadata can still claim `expiry_aligned=True`. Safe exact-key inspection of the existing configuration found `EXPIRY_DAY_TRADE_CURRENT_WEEK='True'`; this is an enabled path, not only a dormant default feature.

Probe: active chain expiry `22SEP2026`, gamma-current expiry `15SEP2026`; the actual `get_token` method returns the `15SEP2026` contract while the chain's selection expression remains `22SEP2026`. This affects chain OI, PCR, premiums/Greeks and volume input to entry intelligence whenever the dates diverge. It also makes a Fyers→Angel fallback capable of changing the analyzed expiry, because Angel resolves execution's actual contract first.

Required invariant: resolve one effective execution expiry (including intraday gamma policy), pass it to all chain/overlay providers and invalidate caches when it changes.

### 2. P1 — Futures volume can jump from 1x to 4x merely when the clock crosses a minute

`get_index_futures_minute_volume_context` reads `current_volume` but never verifies that the state belongs to the present minute (`data/market_data.py:3242`). It divides this old numerator by the new wall-clock minute's progress (`:3259`) and still declares it reliable based only on sample count and <=30-second receipt age.

Production-method probe: seven observations at each minute's `:45`, each +100 cumulative units. At `:59`: current volume100, baseline100, projected ratio1.0169, reliable=True, age14s. At the next minute's `:01`, without any new observation: volume100, baseline100, projected ratio4.0, reliable=True, age16s. This is a fabricated spike. It applies equally to all four indices and is surfaced to scanner feature/health consumers (`scanner.py:9454`, `:9661`).

Related allocation error: the writer puts the whole delta between two samples into the later minute even if the interval straddles a boundary (`data/market_data.py:3204`). Probe `:59=10000`, next `:01=11000`: previous minute records0, new minute records1000. With the normal 15s polling cadence (`:2988`) the exact minute allocation is unknowable, but current code presents it as exact.

Required invariant: timestamp every bucket; never project an old bucket as current. Use exchange-timestamped tick increments or historical completed futures candles; treat cross-boundary polling allocation as uncertain rather than a precise spike measure.

### 3. P1 — Kite OI overlay can merge near-expiry OI into next-expiry Fyers contracts

`_augment_chain_oi_from_kite` calls `source.get_option_chain(index, spot=..., strikes_each_side=20)` without an expiry and joins only by strike (`data/market_data.py:5658`). Kite defaults to the earliest live expiry (`data/kite_market_data.py:748`), whereas Fyers is explicitly aligned to the active rolled expiry. On T-1/expiry those commonly differ. The resulting row retains Fyers's symbol/token and active-expiry metadata but contains another contract's OI and optionally mid-price. The snapshot method is then called with `provider='fyers'` (`data/market_data.py:5884`).

Probe: Fyers next-expiry row with missing OI was filled with near-expiry callOI100/putOI200. The fake Kite call recorded no expiry argument; the output still held the next-expiry symbol and attrs. This is a real cross-contract join defect, not evidence that the current live feed has already taken that fallback.

Required invariant: join by underlying, expiry, strike and side; preserve per-field provider identity and reject incompatible overlays.

### 4. P2 — Derivative activity freshness is based on successful receipt, not exchange observation

Kite futures activity returns `available=True` for any dict and drops `timestamp`/`last_trade_time` (`data/kite_market_data.py:704`, `:715`). The worker then stamps the snapshot with `time.time()` (`data/market_data.py:3121`). A response containing yesterday's values can become a fresh current-minute futures observation. Probe with both timestamps one day old is accepted and emitted without any freshness clock.

The option-chain adapters similarly discard per-leg exchange timestamps: Kite rows at `data/kite_market_data.py:813`, Angel FULL rows at `data/angelone_market_data.py:424`, Fyers column reduction at `data/market_data.py:5869`. Angel's complete-pair check validates identity and positive LTP, but neither quote time nor market depth. Chain metadata stamps HTTP fetch time; it does not prove the individual options traded/updated then.

Required invariant: separate receipt, exchange quote time and last-trade time; enforce appropriate freshness per field. A stale last trade does not by itself invalidate a fresh two-way book, but the current adapters do not retain enough evidence to distinguish them.

### 5. P2 — OI-change time horizons switch silently with provider/fallback

`_apply_option_oi_snapshot_deltas_locked` prefers nonzero Fyers-reported change; otherwise it differences the current OI against the latest local snapshot, and can use the prior session's snapshot (`data/market_data.py:6137`). Both values occupy `call_oi_change`/`put_oi_change`. The stored provider is never checked before using that baseline; snapshot key is only index|expiry (`:6128`). Thus the same field can mean provider day-change, ~90s poll change, or overnight change. Provenance strings identify origin but do not normalize horizon or guarantee cross-provider unit agreement.

Probe with current OI1100, prior OI1000, provider day-change500: the Fyers path emits500; fallback path emits100, and accepts the previous Fyers baseline for Angel. This proves inconsistent time horizons, not that these brokers necessarily use incompatible OI units. `intelligence/order_flow.py:180` adds positive OI change to cumulative option traded volume, combining open-interest change with whole-day turnover as a single activity score. The intelligence audit should assess consumer interpretation separately.

Required invariant: maintain distinct session OI change and aligned rolling interval changes; compare compatible contract/provider quantities, retaining explicit horizon and elapsed time.

### 6. Structural gap — Recovery routes are not capability-equivalent

The parent investigation verified FYERS documentation: index volume is constituent stock share volume by interval and FYERS staff state index volume is unavailable in websocket feeds. Sources: [FYERS index volume definition](https://support.fyers.in/portal/en/kb/articles/can-i-track-volume-on-indices-in-fyers), [FYERS staff websocket explanation](https://fyers.in/community/t/index-volume-in-tickbytick-feed/13638). The runtime nevertheless waits for cumulative cash-index volume from the Fyers websocket/quote probe, then calls it stale and repeatedly polls futures (`data/market_data.py:3040`, `:3388`). Completed Fyers history volume and price feed health are partly distinguished downstream; absence of an unsupported stream field should still not be modeled as broker failure.

Kite futures updates are triggered through the cash-volume refresh worker, not as an independent derivative series. When cumulative cash volume is fresh the early return at `data/market_data.py:3065` suppresses those refreshes. When cash volume is unsupported, the futures fallback remains a short process-local 1m approximation. It cannot supply accurate independent 5m/15m futures volume/move analysis or instant reliable state after restart.

Option-chain TTL is at least90s (`data/market_data.py:5693`), failure cooldown at least180s and can return stale cached chains (`:5709`). Those snapshots are useful for slower structural OI/PCR, but do not form an accurate 1m option volume change/spike feed. Angel fallback also changes the observed strike universe to five near-ATM pairs from a broader Fyers chain. Consumer confidence must account for that reduction rather than assume equal information content.

## Verification and limits

- Offline reproduction script completed all six probe groups and assertion checks. Source/results are saved beside this report.
- Existing upstream tests: **104 passed**, one FYERS SDK `pkg_resources` deprecation warning, 10.14 seconds. Files: `test_index_volume_fallback.py`, `test_futures_volume_never_ticks_into_index_candles_2026_08_14.py`, `test_angelone_latency_futures_volume_2026_09_08.py`, `test_index_volume_refresh_is_per_index_2026_08_05.py`, `test_fyers_volume_telemetry_2026_08_28.py`, `test_kite_market_data.py`.
- The suite validates existing intended paths; it does not cover the demonstrated boundary, expiry-mixing or old-exchange-response cases. Passing tests therefore do not disprove the findings.
- No live broker call or historical P&L replay was made. This establishes input/contract/time correctness defects and architecture gaps; it does not quantify their realized trade losses. The parent audit owns downstream filtering, tracking, trailing and regime consequences.
