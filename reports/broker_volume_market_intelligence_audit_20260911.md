# Broker, volume, market intelligence and regime audit

11 September 2026 · Current working tree, including the previous audit fixes

**Conclusion: the system still has reproducible data-meaning, timing and integration defects.** It does not currently provide independent, consistently identified 1/5/15-minute constituent-index, futures and options participation analysis. Some fallbacks substitute another kind of evidence; some consumers lose that distinction; some trade-management helpers use entry snapshots while others use fresh intelligence. This can produce both missed entries and unjustified confidence or exits. Loosening gates before fixing these inputs would not resolve the causes.

This is a completed investigation, not a new implementation or profitability claim. Product files were preserved. No broker requests, orders, server operations or production database writes were performed by this audit. Synthetic probes demonstrate mechanisms; they do not establish how often each occurred in recorded live trades.

Supporting reports and reproducible evidence:

- [Broker mapping and normalization](C:/Users/sagnik/Desktop/rp46-3/reports/broker_volume_audit_20260911.md), [six broker probe groups](C:/Users/sagnik/Desktop/rp46-3/reports/broker_volume_audit_20260911.json).
- [Candle and intelligence analysis](C:/Users/sagnik/Desktop/rp46-3/reports/candle_intel_volume_audit_20260911.md), [ten candle/intelligence observations](C:/Users/sagnik/Desktop/rp46-3/reports/candle_intel_volume_audit_20260911.json).
- [Session, regime and separate signal engines](C:/Users/sagnik/Desktop/rp46-3/reports/regime_engine_audit_20260911.md), [regime probe results](C:/Users/sagnik/Desktop/rp46-3/reports/regime_engine_probes_20260911.json).
- [Eleven scanner consumer counterexamples](C:/Users/sagnik/Desktop/rp46-3/reports/scanner_volume_consumers_probe_20260911.json), [executable scanner probe](C:/Users/sagnik/Desktop/rp46-3/reports/scanner_volume_consumers_probe_20260911.py).

## What the three brokers actually supply here

| Broker | Current role in this application | Important limit |
|---|---|---|
| FYERS | Index history OHLCV; spot tick arbitration; primary option chain | Constituent-share index volume is not futures/option volume. Socket volume availability is different from history availability. |
| Kite | Primary spot socket; nearest-expiry futures quantity/OI snapshots; option quote/history and OI overlay | Index socket packets have no volume. Derivative day-cumulative quantity needs timestamped interval conversion. |
| Angel One | Price fallback and a small, complete near-ATM option-chain fallback | This implementation does not make Angel an equivalent index-volume/futures history backup. Its option fallback changes the strike universe and omits per-leg freshness evidence. |

FYERS defines index volume as the sum of traded shares of current constituents over each interval and lists all four target indices. This is a participation series for the component stocks. [FYERS definition](https://support.fyers.in/portal/en/kb/articles/can-i-track-volume-on-indices-in-fyers). Its staff stated on 7 January 2026 that index volume is available through History rather than the WebSocket. This is a dated provider statement, not a live certification of today's account feed. [FYERS staff explanation](https://fyers.in/community/t/index-volume-in-tickbytick-feed/13638).

Kite documents day-traded volume for tradable-instrument packets, but its separate index packet contains prices/change/time and no volume or OI. [Kite packet specification](https://kite.trade/docs/connect/v3/websocket/). Angel's official parser likewise distinguishes day volume, OI, exchange timestamp and last-trade timestamp. The presence of a generic volume field does not establish constituent-index volume support. [Angel SDK parser](https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartWebSocketV2.py).

| Index | FYERS index identifier | Kite derivative segment | Angel spot identifier |
|---|---|---|---|
| NIFTY | `NSE:NIFTY50-INDEX` | NFO | NSE / 99926000 |
| BANKNIFTY | `NSE:NIFTYBANK-INDEX` | NFO | NSE / 99926009 |
| MIDCPNIFTY | `NSE:MIDCPNIFTY-INDEX` | NFO | NSE / 99926074 |
| SENSEX | `BSE:SENSEX-INDEX` | BFO | BSE / 99919000 |

These are distinct broker mappings; tokens are not universally interchangeable. The code generally respects that separation. The defects below are mostly shared downstream and therefore apply to all four indices. No live payload comparison across all twelve broker/index combinations was performed.

## What 1/5/15-minute analysis currently means

| Data family | Existing implementation | Gap against the requested behavior |
|---|---|---|
| Index price/constituent participation | FYERS history plus spot candles; 1m resampling to 5m/15m | Useful foundation, but repaired/missing/forming volume and provenance are not handled uniformly. |
| Futures price/volume/OI | Kite cumulative snapshots, normally polled about every 15s; process-local 1m activity baseline | No equivalent persistent independent futures OHLCV/changes pipeline for all three timeframes; restart requires baseline warmup. |
| Options activity | CE/PE day-cumulative volume/OI snapshots; chain TTL at least 90s | Not per-contract 1m/5m/15m increments. Can miss a brief 1m burst and changes contract/strike coverage through fallback. |
| Intelligence labeled 5m or 15m | Regime uses its own frame, but volume/order flow prefer the same 1m frame | A short-term overlay is being presented without its actual measurement timeframe. |
| Live trade management | Mix of fresh exact-frame helpers, cached strings and saved entry context | Different rules can assess different market states for the same trade. |

The current code correctly keeps futures volume out of cash-index candles. The old automatic mixing path should not be described as the normal current behavior. However, keeping streams separate is only the first requirement: the derivative stream also needs valid consumers and accurately labeled measurements.

## Confirmed problems and their downstream effects

### A. Broker identity, clocks and fallback capability

**A1 — Futures can show a false 4x spike without a new observation.** At `:59`, a saved minute-volume observation produced 1.0169x. At the next minute's `:01`, the same observation produced 4.0x and still reported reliable. The getter divides the previous bucket by the new minute's progress without checking bucket identity. The writer also assigns the entire cross-boundary polling delta to the later minute. [Minute-volume logic](C:/Users/sagnik/Desktop/rp46-3/data/market_data.py:3219).

This reaches the pre-candle predictor, where a reliable futures ratio above 2 adds 0.12 confidence when current cash volume is unavailable. [Prediction consumer](C:/Users/sagnik/Desktop/rp46-3/engine/pre_candle_predictor.py:373). The optional pre-candle entry veto defaults off; no matching override was found in `.env`. Therefore the demonstrated direct effect is prediction/telemetry inflation, with entry rejection conditional on enabling that veto. It is not evidence that every current entry filter uses this futures ratio.

**A2 — Chain intelligence can describe the wrong expiry.** Active-expiry selection rolls ahead, while enabled gamma execution can select the current expiry. Separately, the Kite OI overlay requests its default nearest expiry and joins by strike alone into the FYERS chain. The output retains FYERS symbols and metadata. Both failures can give OI/PCR analysis for a different contract from the traded option. [Chain selection and overlay](C:/Users/sagnik/Desktop/rp46-3/data/market_data.py:5654), [execution override](C:/Users/sagnik/Desktop/rp46-3/engine/expiry_manager.py:661). Exact-key configuration inspection found current-expiry gamma trading enabled.

**A3 — A recent API response can conceal old exchange data.** Derivative adapters discard exchange/last-trade timestamps, then receipt time becomes the freshness clock. An offline futures response containing yesterday's timestamps was accepted as available. A successful HTTP request is not proof of a current market observation. Quote freshness and last-trade freshness also need separate treatment for thin options. [Kite activity adapter](C:/Users/sagnik/Desktop/rp46-3/data/kite_market_data.py:704).

**A4 — OI change changes meaning during fallback.** Provider day-change, change since a recent snapshot and overnight change occupy the same fields. A provider switch can reuse the previous provider's baseline without comparing provider identity. Probe: the same OI levels yielded 500 under reported day-change and 100 under local snapshot change. Switching the chain's strike universe also changes aggregate levels without necessarily indicating new participation. [OI differencing](C:/Users/sagnik/Desktop/rp46-3/data/market_data.py:6125).

Recovery still partly models unsupported cash socket volume as a feed failure. Futures refresh is tied to that cash-volume worker, so it is not a continuously independent derivative feature service. The existing health script also describes an older interchangeable-fallback model. These are capability and observability inconsistencies, not a reason to pour futures quantities back into index candles.

### B. Intelligence calculations and signal filtering

**B1 — Old volume can acquire a new, opposite direction.** VolumeProfiler walks back to a measured numerator, but uses the latest close pair to assign bullish/bearish direction. A bullish 500-volume bar followed by three falling zero-volume bars became **BEARISH_VOLUME, available, 4.17x**. This then contributes a bearish intelligence vote. [Numerator selection](C:/Users/sagnik/Desktop/rp46-3/intelligence/volume_profiler.py:237), [direction assignment](C:/Users/sagnik/Desktop/rp46-3/intelligence/volume_profiler.py:435).

**B2 — Missing recent volume still produces strong “flow.”** The prior fix correctly handles a wholly zero-volume session. It does not handle an earlier measured session followed by a dead recent edge: five recent zero-volume bars still produced **BULLISH / 95% buy** because recent close location determines the signal. Session context independently substitutes unit bar weights when volume is absent, then its directional delta adds up to ±0.06 in top-down intelligence. [Order flow](C:/Users/sagnik/Desktop/rp46-3/intelligence/order_flow.py:58), [session fallback](C:/Users/sagnik/Desktop/rp46-3/intelligence/session_context.py:263), [top-down vote](C:/Users/sagnik/Desktop/rp46-3/intelligence/intelligence_aggregator.py:714).

These may be usable price-pressure heuristics if named and validated accordingly. They are not measured buying/selling volume, and counting the same candle geometry again as volume confirmation creates correlated evidence.

**B3 — Own-timeframe and futures labels are misleading.** With 1m volume=100 and 5m volume=9,000, an analysis requested as 5m returned volume=100 and `aggregate.futures_volume=100`. The same 1m preference also supplies volume-derived structure/Aroon/WaveTrend. Output does not identify the selected volume observation's actual timeframe/time. [Input selection](C:/Users/sagnik/Desktop/rp46-3/intelligence/intelligence_aggregator.py:224), [output alias](C:/Users/sagnik/Desktop/rp46-3/intelligence/intelligence_aggregator.py:648).

**B4 — Option OI and option volume are combined inconsistently.** Order flow adds positive OI change to cumulative traded quantity and interprets CE/PE activity as directional. The OI engine can simultaneously call the same call-OI addition bearish. The probe produced bearish OI and bullish option-flow confirmation from this reuse; PCR trend also changed from RISING to STABLE solely through repeated reads of an unchanged chain. [Option activity](C:/Users/sagnik/Desktop/rp46-3/intelligence/order_flow.py:178), [PCR state](C:/Users/sagnik/Desktop/rp46-3/intelligence/pcr_engine.py:134).

Volume counts transactions; OI counts outstanding positions. Each contract has both a buyer and seller. Their sum is not a defined directional flow measure, and CE volume by itself does not identify aggressive call buying versus call selling. [Exchange definitions](https://www.cmegroup.com/trading/about-volume.html). The code inconsistencies are proven; the profitability of any particular replacement directional model is not.

**B5 — Entry grading loses volume provenance and ignores useful measured context.** Candidate `volume_ratio` is stamped from special spike metrics; the basis is not carried into `QualityInputs`. Ordinary candidates can have valid profiler volume while that scalar remains zero. A 15m A candidate with ADX30 and measured 3x context was judged to have no volume confirmation; a 5m candidate with a 1.3 tick-activity scalar was accepted as volume-confirmed in the isolated quality route. [Candidate stamping](C:/Users/sagnik/Desktop/rp46-3/engine/signal_processor.py:7637), [quality adapter](C:/Users/sagnik/Desktop/rp46-3/scanner.py:27220).

This is both an overfiltering mechanism and a false-confirmation mechanism. It does not imply all candidates should pass: other confirmations and actual trade quality still matter. The local special-spike detector already distinguishes tick percentiles; that distinction is lost downstream.

**B6 — Forming-bar and closed-bar ratios use different conventions.** The thrust extractor divides current partial volume by full prior bars without elapsed-time normalization. Equal activity rate after 30 seconds of a five-minute bar reads 0.1x. Other components project partial volume or borrow a completed bar, so their “volume ratios” are not interchangeable. Also, an aware `as_of` against a naive candle index makes slicing throw and silently continue with later data; the probe requested 10:05 but read the 10:30 spike. The latter is a conditional API/replay defect, not proof that every production caller supplies mismatched timezones. [Thrust extractor](C:/Users/sagnik/Desktop/rp46-3/scanner.py:15300).

### C. Tracking, trailing and exit decisions

**C1 — Saved entry context wins over the live cache.** `_trade_like_market_intelligence_context` immediately returns a nonempty attached snapshot. Candidate creation saves that snapshot and session restoration restores it; the session tracking loop passes the saved candidate to the exit helpers. The probe returned entry intelligence +0.8 despite current cache -0.8, with **zero cache reads**. A direct saved regime vector also takes precedence. [Context resolver](C:/Users/sagnik/Desktop/rp46-3/scanner.py:17782), [entry snapshot](C:/Users/sagnik/Desktop/rp46-3/engine/signal_processor.py:7719), [session exit call](C:/Users/sagnik/Desktop/rp46-3/scanner.py:28460).

This affects near-peak threat, choppy profit-lock activation, recovery watch and some loss-band evidence for objects carrying that snapshot. Normal TradeManager trades have no `market_intelligence_context` field and usually take the cache branch. Session candidate exits produce virtual tracking exits/alerts; this loop explicitly does not mutate broker positions. Thus the finding establishes divergence between session/manual tracking and broker-trade management, not frozen intelligence on all actual broker exits. Candidate snapshots also omit OI/PCR even though this helper's downstream readers look for them. Direct saved-vector precedence was demonstrated by attaching that attribute in the probe; its normal live attachment was not established.

**C2 — Entry volume can mask current weakness; another reader looks in the wrong place.** Near-peak threat prefers positive entry `volume_ratio` over current volume. With current ratio 0.4, the weak-volume point disappears when entry ratio=2 is present. This saved-scalar path normally concerns session candidates; ordinary broker Trade has no such field. The helper's last fallback uses `buy_sell_ratio` as if it were a participation ratio. The choppy-lock helper looks for `aggregate.volume`, but actual volume is the sibling `context.volume`; this affects either object type. Probe: the real payload kept the activation threshold at Rs1,000; the incorrectly nested payload lowered it to Rs750. [Choppy lookup](C:/Users/sagnik/Desktop/rp46-3/scanner.py:17998), [near-peak lookup](C:/Users/sagnik/Desktop/rp46-3/scanner.py:18663).

**C3 — Some exit voters ignore availability (defensive contract gap).** The near-peak helper reads signals/ratios without checking per-pillar validity, and calls its evidence domain `fresh_intel` without establishing freshness itself. A stale or unavailable payload retaining directional values can contribute points. In a controlled contract probe, unavailable adverse aggregate/OI confirmed an exit after two observations without price evidence. Normal unavailable OI and order-flow outputs are neutral, and the aggregate does not normally publish `available=False`. The injected combination therefore proves a missing validation boundary, not a normally reachable false broker exit. Repeated confirmation of an unchanged invalid snapshot does not make it valid. [Exit voters](C:/Users/sagnik/Desktop/rp46-3/scanner.py:18609).

Some other exits correctly use `_fresh_exact_trade_intel`. That stronger helper should be reused consistently. The aggregate score and its constituent pillars are also counted separately in near-peak scoring; they are correlated contributions, not independent sources.

### D. Other market engines and regime

**D1 — Session range baselines are computed from the wrong history scope.** Live routing retains only 60 prior-session 5m bars for indicator warmup and passes them to the session analyzer as its history. The purported ten-day average can be a partial yesterday. Probe: true average range50 became10, so today's range ratio changed **0.2x → 1.0x**. PDH140 became105. With fuller history, another bug calculates PDH/PDL across all earlier days instead of yesterday only. These values influence day classification and choppy-lock activation. [Live window](C:/Users/sagnik/Desktop/rp46-3/scanner.py:13295), [session levels](C:/Users/sagnik/Desktop/rp46-3/intelligence/session_context.py:61).

**D2 — Regime strings do not share the exit bundle's freshness contract.** An exact 15m TRENDING_UP label with epoch-1 cache time still counts as own-timeframe. If missing, the helper can borrow 5m RANGING. Those states choose different ATR/one-minute-candle trailing and affect runner privileges. A feed problem can therefore look like a change in market regime. [Regime accessors](C:/Users/sagnik/Desktop/rp46-3/scanner.py:8831). Cached live cross-index breadth likewise lacks a per-peer age check.

**D3 — Quad Prime still double-counts derived state.** Raw UT/EMA/Supertrend are price/ATR-based; volume is surrounding context, not the source of the UT ATR flip. The prior source-count fix deduplicated signal provenance, but the separate state vote still includes held EMA/ST consensus as a fourth source. Two of three primitive sources bearish became a 2:2 tie and FLAT because stale consensus remained bullish. This state feeds MTF confluence. [State vote](C:/Users/sagnik/Desktop/rp46-3/engine/alt_scanners.py:123).

**D4 — Several market labels describe different horizons.** Dashboard “Hourly / Live” uses 36 five-minute bars; the detector needs60 for its full BB/breakout features. It remains PARTIAL, while a full model on the same series can say BREAKOUT_UP. The actual trading hourly anchor uses resampled60m bars, a third model. Session “minute” mood normally uses a five-minute bar; opening range assumes six rows rather than explicit clock boundaries. Rare-burst logic still reads opening bias after checking effective phase. These differences explain some apparent disagreements without requiring a broken UT formula. [Detailed regime evidence](C:/Users/sagnik/Desktop/rp46-3/reports/regime_engine_audit_20260911.md).

## Conditional persistence and cache defects

These were reproduced but must not be overstated as normal current live failover:

- CandleBuilder drops the first cumulative increment at a minute boundary. A controlled +400 total increase stored only200. Production effect depends on actual cumulative-volume input and later history repairs.
- A source-change rebase can have its zero replaced with the old positive number while keeping the new basis. The cash-to-futures example is latent because current routing separates those lanes.
- A previous-day futures-labeled row can veto a valid current-session cash volume window. The homogeneity guard checks the entire supplied frame, beyond the metric's actual baseline.
- Resampling drops tick counts, limiting equivalent tick-activity behavior on5m/15m fallbacks. Unknown provenance can also be inherited as if verified.
- The MTF cache excludes volume and older candle corrections. Volume-only invalidation matters for supported `adaptive_dual`; the currently selected Quad Prime raw trigger is price-only.
- Volume-dependent market-structure outputs can remain active after the volume pillar is vetoed. Early-session structure can fall back to warmup history while still labeled session-only.

## Repair order and acceptance criteria

1. **Preserve data meaning at existing boundaries.** Keep constituent-index participation, exact futures quantity, exact option quantity/OI and tick activity distinct. Carry actual asset/contract, provider, unit, interval, observation time, receipt time, completeness and measurement validity into each consumer. Do not present an unavailable feature as a measured neutral or weak value.
2. **Fix identity and time arithmetic.** Resolve one effective execution expiry; join overlays by full contract identity. Retain exchange clocks, reset incompatible baselines, deduplicate chain observations, and prevent rollover/reconnect/clock advancement from creating a spike. Expose uncertain cross-minute poll allocation rather than treating it as exact.
3. **Make requested timeframes real.** Build separate coherent1m series for index participation, futures and selected options; derive5m/15m interval features from the corresponding lane. Prefer valid completed history where available; disclose live projection and coverage. Keep any intentional1m overlay separately named.
4. **Unify decision consumption.** Entry uses the current measured feature or an explicitly separate calibrated proxy. Live management uses fresh current context; entry snapshots remain attribution. Fix the wrong nested lookup and buy/sell-ratio substitution. Reuse the fresh exact-timeframe helper and invalidate dependent outputs when their inputs fail.
5. **Repair session/regime inputs and redundant votes.** Supply complete prior-session summaries separately from indicator warmup. Expire regime/breadth labels; identify fallback horizons. Count primitive signal states once and disclose panel readiness. Preserve correctly working per-bar regime smoothing.
6. **Then evaluate strategy thresholds.** Replay the same timestamped raw observations before/after, across all four indices and all three timeframes, including broker failure, expiry rollover, zero/stale volume, forming bars and recovery. Measure raw signals, unique rejection reasons, quote-valid entries, false exits, time in trade, MAE/MFE and net P&L with costs on held-out sessions. Fixing semantics is measurable immediately; improving profitability needs this separate comparison.

Regression acceptance should invert the reproduced counterexamples: no clock-only spike; no mismatched expiry merge; no changed PCR state from duplicate reads; no direction assigned from a different bar; no proxy promoted into traded volume; no stale entry snapshot substituted for live analysis; no history-slice-dependent PDH/ten-day range; no additional vote for a derived consensus.

## Verification status

- Six broker probe groups, ten candle/intelligence observations, eleven scanner consumer groups and the independent regime probes completed successfully. The scripts intentionally assert current counterexamples and are audit artifacts, not post-fix regression tests.
- Existing focused broker tests: **104 passed**. Existing focused scanner/quality/exit tests: **76 passed**. The runs overlap in one test file, so these are not180 distinct tests. Their success shows these cases were not covered by existing checks; it does not establish correctness of the demonstrated gaps.
- Current production code was not modified in this pass. Prior uncommitted implementation changes remain present.
- Historical trade-level attribution, live provider capability certification and post-fix profit improvement remain unproven. The defects and repair boundaries above are established strongly enough to implement targeted corrections without speculative threshold loosening or adding another intelligence engine.
