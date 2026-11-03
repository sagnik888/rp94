# Candle and intelligence volume audit — 11 September 2026

Scope: read-only investigation of current code, including the previous audit's working changes. Synthetic, in-memory probes only. No broker requests, production database writes, server control, or product edits. The accompanying Python script contains ten executable probes; all observations reproduced. These are defect demonstrations, not trading-performance backtests.

## Current architecture

- `data/candle_builder.py:128` builds the index OHLC lane from spot ticks and an optional cumulative volume value. `:260` starts each minute's baseline. `:781` aggregates 1-minute OHLCV to 5/15 minutes.
- Current production separates the futures volume fallback from index candle volume; `scanner.py:9763` documents this, while `data/market_data.py:2142` carries only the cash source into index ticks. Therefore the old design's automatic cash/futures mixing must **not** be presented as the current normal path. Legacy data and the generic builder remain relevant conditional paths.
- `intelligence/intelligence_aggregator.py:224` chooses the same 1-minute frame whenever available, even for an analysis requested as 5 or 15 minutes. Volume, volume-derived market structure, Aroon/WaveTrend, and order-flow inputs inherit that choice (`:227`, `:452`). Regime has its own timeframe handling.
- FYERS index volume is synthetic constituent-share participation, rather than turnover in a tradable index contract. Its meaning must remain distinct from exact futures quantity, exact option quantity, and delivered tick count. Broker validation and official references are covered by the parent/broker audit.
- OI and PCR consume the authoritative option chain. The aggregator explicitly withholds stale/fallback/misaligned chain data at `intelligence/intelligence_aggregator.py:199`. OI stays separate by call/put side, which is an improvement over aggregate futures-style OI classification.

## Findings ranked by directness and likely consequence

### 1. A past volume spike can be assigned the latest opposite price direction — current reachable defect

`intelligence/volume_profiler.py:237` walks back as many as four bars to select a measured volume numerator. Its baseline is anchored to that selected bar at `:281`, but the signal direction is taken from the **latest** close pair at `:437`. Neither the measured timestamp, lag nor actual timeframe is returned at `:482`.

Probe: 26 bars of volume 100, a bullish bar with volume 500 and close change +10, followed by three falling bars with no volume. The profiler returns `available=true`, `current_volume=500`, `volume_ratio=4.17`, **`BEARISH_VOLUME`**. This gives a current bearish interpretation to a three-minute-old bullish participation event. The aggregator awards the resulting ±0.20 directional volume vote at `intelligence/intelligence_aggregator.py:236`.

The proposed repair boundary is the volume observation: timestamp/direction/age must belong to the same selected bar, with an explicit distinction between recent context and current evidence. A cheap regression test should freeze the clock and reverse latest price direction while keeping the measured spike unchanged.

### 2. The previous zero-volume order-flow fix stops only wholly empty sessions — current reachable defect

`intelligence/order_flow.py:58` accepts the source when **any** current-session volume exists. The directional signal then comes from an unweighted average of the last five candle close locations (`:69`), even if all five have zero volume. It publishes `available=true` (`:102`).

Probe: first five session bars volume 100, last five volume zero, last closes at 95% of candle range. Result is **BULLISH, 95% buy, available=true**. Thus a history repair outage can still become a strong order-flow reading in the current implementation. The all-zero session now correctly abstains, but that narrower prior correction did not resolve recent-feed absence.

There is also no volume-basis homogeneity check in this analyzer. Conditional mixed-cache probe returns BULLISH from recent price location while reporting ratio 0.05 and cumulative delta -449,955 from cash-share-weighted earlier bars. Current production's separated futures lane reduces that mixing path; it does not repair analyzer semantics.

The narrow test should require recent measurement coverage, expose OHLC candle pressure separately, and validate no directional flow vote from five volume-less recent bars.

### 3. A 5/15-minute intelligence label contains 1-minute volume and a misleading futures field — current reachable design mismatch

`intelligence/intelligence_aggregator.py:224` unconditionally prefers 1-minute candles for volume and order flow. `:647` still labels the aggregate with the requested timeframe, and `:648` exports that volume as **`futures_volume`**, regardless of whether it is constituent-share volume or tick activity.

Probe: supplied 1-minute volume=100, 5-minute volume=9,000, requested timeframe=5min. Output is `aggregate.timeframe=5min`, `volume.current_volume=100`, `aggregate.futures_volume=100`. The volume object has no measurement timeframe/timestamp field. Using a short-term participation overlay can be deliberate; calling the result own-timeframe/futures participation is unsupported. It does not satisfy the user's requested distinct 1/5/15-minute derivative analysis.

`data/candle_builder.py:793` resamples OHLCV and basis only. The probe confirms `tick_count` is dropped from 5-minute frames. Consequently an analyzer fallback to the own-timeframe frame cannot reproduce the 1-minute tick-activity fallback, either. A direct contract test should assert each output's actual asset, units, timeframe, timestamp and source, then test each requested timeframe independently.

### 4. Option OI is reused with opposite directional meanings and incompatible windows — current reachable model inconsistency

`intelligence/order_flow.py:178` reads day-cumulative option volumes, then `:180` adds positive option OI changes to them. `:106` calls a CE/PE activity ratio above 1.15 bullish. This combines traded flow with changes in outstanding positions, and gives no event window or aggressor-side evidence. `engine/pre_candle_predictor.py:430` uses that ratio for a +0.05 directional confirmation, so it is not exclusively display metadata.

At the same time `intelligence/oi_tracker.py:148` treats call OI addition as bearish resistance. Probe with equal CE/PE traded volume and only positive call OI additions produces **OI=BEARISH but option-flow confirmation=BULLISH (ratio 1.5)** from the same input. This proves inconsistent reuse; it does not prove which directional heuristic is profitable.

OITracker sums change fields directly (`:113`) and reports `oi_change_source` only as telemetry (`:132`), without enforcing common provider baseline/measurement window. The broker audit's day-change versus roughly 90-second change and provider-switch findings therefore reach this model unchanged. Prefer separate observations and only combine independently defined, comparable features. Test provider/window changes without a genuine market change.

### 5. PCR trend depends on repeated analysis calls rather than new market observations — current reachable defect

`intelligence/pcr_engine.py:137` keys history only by index and appends on every `analyze()` call. No snapshot ID, time, session or expiry identifies an observation. Both requested timeframe analyses can consume the same chain, so the trend horizon changes with scan frequency and repeated requests.

Probe: PCR observations 1,1,1,1.5,1.5,1.5 yield RISING; three additional reads of the identical final chain yield STABLE. No new option-chain information was required. The fix boundary should deduplicate by actual source snapshot and reset/partition by contract universe/session; test duplicate reads explicitly.

### 6. Full input-window homogeneity can disable today's valid cash volume — conditional legacy-cache defect

`intelligence/volume_profiler.py:390` checks **all** input rows although its rolling denominator uses today's rows (`:260`). Scanner's live window deliberately prepends previous-session warmup (`scanner.py:13295`).

Probe: one previous-day `kite_futures` row, followed by 29 current-day `fyers_index` rows. The latest 20 bars are homogeneous with 4.17x participation, but the entire-frame profiler returns unavailable and 1.0x. Analyzing those latest 20 alone returns available and 4.17x. This can suppress valid current metrics until old labels disappear. It is conditional on old/mixed provenance remaining in the cache; current injection of futures into cash candles was not demonstrated.

Homogeneity must be evaluated for each metric's actual numerator/baseline window. VWAP and market structure also need their own compatible source/units validation: the profiler still computes them at `:267` and `:457` after a volume veto, and the aggregator accepts market-structure signals at `intelligence/intelligence_aggregator.py:245` independently of volume availability. Blanket volume abstention does not invalidate those dependent outputs.

### 7. Cumulative volume loses the first increment at every minute boundary — builder defect, live impact bounded by data source

`data/candle_builder.py:260` initializes a new minute's baseline to the first arriving cumulative total. The increment since the previous minute's final observed total is assigned to neither minute.

Probe: cumulative totals 1000@09:15:00, 1100@09:15:50, 1200@09:16:00, 1300@09:16:50, 1400@09:17:00. Known increase=400; stored minute volumes=[100,100,0], total=200. The effect can be large with sparse observations. Reliable interval observations/REST repairs can correct the affected bars, so this is not proof of a 50% production deficit. Cash index WS volume often being absent further limits this path's actual use.

Test cumulative conservation after the initial baseline, including a minute rollover, sparse updates, reconnect and daily counter reset.

### 8. Source-switch rebasing is undone by zero-volume preservation — proven latent builder defect

`data/candle_builder.py:231` correctly resets a same-minute volume on source change. But `_restore_zeroed_volume` (`:486`) restores the old positive numeric value during merge, without its old basis. `_restore_candle_telemetry` (`:633`) then sees the restored positive value plus new basis as incoming measured volume.

Probe: cash cumulative 1,000,000→1,100,000, followed by first futures cumulative 1,000 in the same minute. Internal new-source volume=0 but stored candle is **100,000 with basis `kite_futures`**. The current separated index/futures production architecture makes this latent rather than a demonstrated normal live failover. Test value/provenance atomicity across any source transition and history repair.

## Other important limitations

- Tick activity is correctly labeled a proxy (`volume_profiler.py:128`, `:518`), but `intelligence_aggregator.py:236` gives it the same available-volume vote as share participation. A tick-count-weighted index VWAP also travels under the ordinary `vwap` field (`volume_profiler.py:264`). These are heuristic price/feed activity features, not measured contract or constituent turnover. Their separate calibration is not demonstrated here.
- Volume availability has bar-count and stub checks, but no returned observation age. It judges the active day from the last dataframe row rather than a supplied decision clock. Scanner freshness guards may prevent stale invocation; the analyzer itself cannot make that contract explicit.
- A source/basis missing from historical rows is treated as homogeneous (`volume_profiler.py:61`). This avoids blocking unknown rows but is not proof of comparability. Resampling also stamps a sole known basis onto unlabeled buckets (`candle_builder.py:833`). Provenance inheritance should not be mistaken for independently verified measurement.
- `market_structure_profile` defaults to session-only but falls back to the complete warmup frame while the current session has fewer than 12 bars (`intelligence/price_action.py:257`). It still reports `session_only=true` (`:323`). Early-session value-area context can therefore include yesterday. Its output needs honest scope labeling; whether to retain that context is a strategy choice.

## Verification record and limits

- Run: `venv/Scripts/python.exe reports/candle_intel_volume_audit_20260911.py`
- Result: ten synthetic probes reproduced, process exit 0. Measurements are in `reports/candle_intel_volume_audit_20260911.json`.
- Source files were read only. The report/probe/JSON are the only artifacts created by this subtask.
- This audit establishes deterministic mechanisms and semantic mismatches. It does not attribute a particular live trade's P&L to them, certify a volume threshold, or assert that more features/looser gates improve profitability.
