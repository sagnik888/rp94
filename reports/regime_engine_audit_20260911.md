# Regime, session context and separate signal-engine audit — 11 September 2026

Read-only investigation of the current working tree, including the September 10 fixes. No product files changed, no production database access, no broker requests, and no live process changes. Synthetic probes execute the current functions; scanner helpers are loaded through AST to avoid scanner initialization. This establishes mechanisms, not their frequency or realized financial impact.

Reproduction: `venv/Scripts/python.exe reports/regime_engine_probes_20260911.py`. Results: `reports/regime_engine_probes_20260911.json`.

## Findings ranked by direct effect

### 1. Missing volume still creates a directional order-flow vote through session context (confirmed, high)

`intelligence/session_context.py:263` replaces an all-zero volume series with unit weights, calls it `bar_pressure`, and computes `cumulative_delta`, `delta_ratio` and a directional bias. `intelligence/intelligence_aggregator.py:714` consumes this bias without checking its `source`, and adds up to ±0.06 with the reason **session cumulative delta bullish/bearish**. The September 10 order-flow abstention fix therefore does not close this second route.

Probe: fourteen unchanged-price bars, all volume zero, close located high in each candle. Output: `source=bar_pressure`, `cumulative_delta=12.6`, `delta_ratio=0.9`, `bias=BULLISH`, followed by a +0.06 top-down adjustment. No traded volume or observed aggressor flow exists in the input.

There is additional correlated use: `intelligence/session_context.py:109` sends that same delta bias into the day classifier, which adds 0.05 confidence when it agrees with a trend (`:322`, `:333`). The rare-burst gate can also reject the opposite side using session delta (`scanner.py:29615`). Thus a price-derived fallback can first strengthen a day label and then vote again as flow. `vwap_source=price_fallback` is correctly disclosed, but downstream day/regime guards do not treat the resulting VWAP statistics as lower-provenance data.

Repair boundary: keep price pressure as an explicitly separate feature. Actual session flow should abstain when volume is unavailable. Even OHLCV-weighted close location is a pressure proxy, not measured buy/sell aggressor volume. Provenance must survive every consumer.

### 2. The live session model receives an indicator warmup slice and labels its truncated range as ten-day context (confirmed, high)

`scanner.py:13295` retains only the final 60 prior-session 5-minute bars in REAL mode. The main pipeline passes this frame to `_build_intelligence_contexts` (`scanner.py:14281`), which sends it directly to the session analyzer (`:13703`). The analyzer then computes `avg_10day_range_points` by grouping whatever prior rows remain (`intelligence/session_context.py:244`). A normal full session has 75 five-minute bars, so the warmup excludes the first 75 minutes of yesterday and usually contains no older session at all.

Probe: complete prior sessions each had a 50-point range, with the extremum near the open; current session range was 10 points. Complete-history analysis: average range 50, ratio 0.2. Actual live-window routing: average range 10, ratio 1.0. The same current tape becomes five times more extended merely because the supplied history was truncated. PDH/PDL change from 140/90 to 105/95.

This is not just a displayed statistic. The day classifier uses range ratio (`session_context.py:312`, `:321`, `:332`). Adaptive profit-lock activation reads it as a quiet-market condition (`scanner.py:17923`). It can therefore alter grading priors and breathing/locking decisions.

A second, independently confirmed bug exists when fuller history is supplied: `session_context.py:61`/`:62` calculate PDH/PDL across **all** previous dates. Probe: actual yesterday high 140, older-day high 180; `pdh=180`.

Repair boundary: supply complete prior-session/daily summaries separately from the indicator slice; PDH/PDL must select only the immediate prior trading session; carry range sample count and completeness rather than implying ten days when fewer are present.

### 3. Regime fallback and freshness differ between exit bundles and trailing labels (confirmed, high)

`_fresh_exact_trade_intel` (`scanner.py:8884`) checks candle freshness, intelligence age and matching timeframe. This is good. But `_get_trade_regime` (`:8865`) returns stored labels with no time/freshness check and borrows the 5-minute label when 1-minute/15-minute labels are missing. `_trade_regime_is_own_timeframe` (`:8845`) also checks only whether a label exists.

Probe: an exact 15-minute `TRENDING_UP` label with cache timestamp epoch 1 remains returned and counts as own-timeframe. Deleting it returns 5-minute `RANGING` for the 15-minute trade, with own-timeframe false. There is no provenance in the returned string.

The distinction changes real management: regime chooses ATR versus previous-1-minute-candle trailing (`scanner.py:35087`, `:35098`; candidate tracking has the same split at `:28142`, `:28156`), and stabilization uses `_get_trade_regime` (`:19652`). A missing higher-timeframe reading can therefore cause abrupt tightening via borrowed ranging context; an old trend label can persist even after its feed stops. The own-timeframe boolean protects some runner privileges, but does not expire an old exact label.

Repair boundary: one timestamped regime contract, with requested/source timeframe, source candle time, calculation time and validity. Optional fallback must remain visibly borrowed and cannot silently supply the same authority as fresh exact data. Feed loss and a measured ranging regime are different states.

### 4. Quad Prime still counts held EMA/Supertrend consensus as a fourth independent state vote (confirmed, medium)

The active setting is `quad_prime`. Current raw signal generation is a union of UTBot, EMA, Supertrend and their derived consensus (`engine/alt_scanners.py:939`). `_quad_union_consensus_positions` still votes all four (`:123`–`:144`), while `_consensus_positions` holds the previous agreed direction through disagreement (`:167`).

Probe: state starts EMA=UP, ST=UP, UT=UP. Next bar EMA=DOWN, ST=UP, UT=DOWN. Primitive sources are 2:1 bearish, but held consensus remains UP, creating 2:2; output state becomes FLAT `[1, 0]`. That state reaches multi-timeframe confluence (`engine/multi_timeframe.py:437`). This confirms distorted/lagged state evidence; it does not prove an individual entry or exit was wrong.

The prior repair in `scanner.py:26090` correctly deduplicates consensus in **signal source counts**. It does not change the separate state-voting algorithm. Raw UT/EMA/ST signals are fundamentally price/ATR based; expecting broker volume to change those raw triggers would misdescribe the current design. Volume enters the surrounding intelligence/filtering layers.

Repair boundary: consensus may remain a named strategy event or held state, but it must not count as independent corroboration alongside its own ingredients.

### 5. Dashboard “Hourly / Live” regime is a permanently partial 36×5-minute model, different from the trading regime and 60-minute anchor (confirmed, medium)

`scanner.py:14036` builds the dashboard hour pocket from 36 five-minute bars and uses it as the displayed primary live regime. `RegimeDetector.detect` needs 60 bars for full confidence, Bollinger width and its 60-bar breakout test (`intelligence/regime_detector.py:92`, `:228`, `:334`). Consequently that dashboard pocket is always PARTIAL at sufficient history, always lacks the 60-bar breakout feature, and caps confidence at 60. It represents approximately three hours of 5-minute data, not one-hour bars.

Probe with one identical trending series: full 80-bar 5-minute model returns `BREAKOUT_UP`, FULL, confidence 70, BB width 15.72; dashboard 36-bar tail returns `TRENDING_UP`, PARTIAL, confidence 60, BB width zero, breakout NONE. `_detect_tf_regime_snapshot` drops the readiness flags from its returned UI object (`scanner.py:13889`). A user cannot readily distinguish partial-model disagreement from a market change.

Separately, the trading top-down anchor really resamples 5-minute candles to clock-aligned 60-minute bins (`scanner.py:13543`), including the unfinished final bin. It is therefore a third horizon with different boundaries and warmup. The hour pocket is deliberately isolated from shared trading detector history, which is correct, but its UI naming and readiness hide the distinction.

Repair boundary: expose timeframe, analysis lookback, readiness and completed/forming status separately. Share feature definitions where intended, or label intentionally different models accurately.

### 6. Session mood horizons are all derived from a 5-minute frame; nominal time horizons can overstate actual coverage (confirmed contract issue)

`scanner.py:9008` uses the 5-minute frame to build session context even when requesting 1-minute or 15-minute intelligence. The top-down prior can validly be shared, but its `minute` mood is one 5-minute bar, not an independent 1-minute observation. `_mood_horizons` derives every horizon from whichever single input frame it receives (`session_context.py:357`).

Opening range is fixed at the first six rows, and day-type warmup is fixed at six rows (`session_context.py:67`, `:308`). It only means a 30-minute opening range when input really is 5-minute data. Direct probes return opening status INSIDE at 09:20 with 1-minute input and at 10:30 with 15-minute input. In the current production caller it is normally the 5-minute case; this is not evidence that live production alternates all three inputs today.

The quarter mood on a 15-minute input actually uses two bars because `min_bars=2`, but reports `lookback_minutes=15`; the probe confirms this. Early session inputs also report the nominal 15/60-minute horizon even when fewer rows exist. `_infer_bar_minutes` uses only the last interval, so one missing candle changes the inferred timeframe rather than reporting a gap.

Repair boundary: accept an explicit timeframe and as-of time; select opening range by session clock boundaries, distinguish requested versus observed coverage, and build 1-minute/5-minute/15-minute features from their respective coherent source streams.

### 7. Volume-only updates do not invalidate the separate engine's cache (confirmed latent issue)

`engine/multi_timeframe.py:165` hashes row count, final timestamp and close/high/low, but excludes volume and older-row corrections. Synthetic final volume change from zero to 999999 leaves the hash unchanged. This is harmless for the current Quad Prime raw price-only signal logic, but `adaptive_dual` is a supported selectable engine and its thrust/context calculations consume volume (`engine/alt_scanners.py:456`, `:829`). Under that engine, volume can materially change without a recomputation until price or another key changes.

Repair boundary: cache on a candle revision or the actual feature inputs of the selected engine. Do not require spurious price changes to notice updated volume.

## Current architecture and mechanisms that are working

| Path | Current inputs/meaning | Qualification |
|---|---|---|
| UT / EMA / ST / Quad Prime | Each of 1m, 5m, 15m index OHLC; UT ATR stop; alternate price trend triggers | Raw triggers are not measured volume-flow signals |
| Own-timeframe regime | ADX, ATR%, 60-bar BB width/extremes, SMA structure, RSI, MACD, ER; session/breadth adjustment | It does not directly read volume, futures OHLC or option volume (`regime_detector.py:57`) |
| Session context | Current 5m session candles and spot; price/volume-weighted VWAP, bar-pressure delta, opening/day and rolling moods | Several provenance and range-history issues above |
| Trading hourly anchor | Resampled clock-aligned 60m candles, fresh detector warmed over last configured smoothing bars | Different model from dashboard hour pocket |
| Cross-index breadth | Four indices' cached 5m regimes; directional bias needs 3/4 | As-of replay uses historical 5m slices; live cached reads have no age check (`scanner.py:13619`) |
| Dashboard live/hour | Separate 36-bar 5m detector | Isolated state avoids corrupting trading smoothing; permanently partial feature set |
| Fresh exact exit intelligence | Matching timeframe, candle freshness and bounded intelligence age | Better contract than the independent regime-string helper |

Per-bar regime smoothing now replaces repeated evaluations of the same bar instead of accumulating ten identical votes (`regime_detector.py:305`). Different timeframe keys are separate. Warming a throwaway detector over the final window is deterministic (`scanner.py:13566`). With configured window 5, smoothing spans different clock durations on 1m, 5m and 15m; that is a tuning/model choice, not intrinsically a bug.

The previous effective-phase repairs remain in grading, top-down adjustment, regime early-trend promotion and range guards. An exception remains in the rare-burst gate: it reads opening `session_ctx['bias']` (`scanner.py:29606`) even after evaluating effective phase just above, so a later reversal can pass phase logic and still fail stale opening bias. This is a bounded remaining consumer inconsistency.

Current observed settings: Quad Prime, smoothing enabled/window 5, ER veto disabled, range-day ADX override does not require ER. The latter two are explicitly safety-off settings in `config/settings.py:1195` and `:1203`; neither should be silently enabled from an audit. Thresholds, fixed volatility floors, five-bar smoothing lag, and heuristic conviction probabilities require out-of-sample measurement. They are model limitations, not proven coding defects here.

## Scope limits

Broker transport/normalization, per-bar volume provenance, futures/option volume units, quote age, cumulative-to-increment conversion and their downstream spike/trade consumers are being audited separately. This report does not assert that a synthesized positive bar-pressure vote caused any particular historical loss. It demonstrates that the current code permits it and shows the exact computation.
