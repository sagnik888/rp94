# Verification of the September 10–11 fixes

11 September 2026. **Result: partially repaired, not fully fixed.** The previous completion response overstated coverage. The full-suite result of 5,898 passing tests was real, but those tests did not establish every audit acceptance condition. Some compatibility changes made to obtain passing tests weakened the intended contracts.

This pass changed no product code. It inspected the final implementation, reran relevant tests, and added an offline diagnostic script and evidence file. Synthetic observations establish mechanisms, not their frequency in live trades. No live profitability certification is made.

## Verification evidence

- Existing audit/quality/volume/September-10 trade-management regressions: **87 passed**, 174 warnings.
- Broker volume separation, Angel fallback, FYERS telemetry, regime tracking, expiry-day gamma and chain-readiness regressions: **104 passed**, 208 warnings.
- These two selections contain different test files: **191 tests passed this pass**.
- Independent production-method probes: [script](C:/Users/sagnik/Desktop/rp46-3/reports/postfix_verification_20260911.py), [results and source SHA-256 fingerprints](C:/Users/sagnik/Desktop/rp46-3/reports/postfix_verification_20260911.json).
- Probes execute extracted production methods with synthetic collaborators; they do not initialize the app, connect brokers, or write trading databases. Their outputs intentionally record incorrect behavior; successful script execution is not an acceptance pass.
- The entire suite was not rerun this pass: focused tests plus explicit counterexamples already disprove the claim that all gaps are closed.

Commands:

```powershell
python -m pytest tests/test_broker_intelligence_audit_fixes_2026_09_11.py tests/test_signal_trade_management_audit_fixes_2026_09_10.py tests/test_intelligence_data_truth.py tests/test_volume_profiler.py tests/test_order_flow_abstains_2026_08_08.py tests/test_quality_verdict_parity_2026_09_01.py -q --disable-warnings --tb=short
python -m pytest tests/test_index_volume_fallback.py tests/test_futures_volume_never_ticks_into_index_candles_2026_08_14.py tests/test_angelone_latency_futures_volume_2026_09_08.py tests/test_fyers_volume_telemetry_2026_08_28.py tests/test_regime_record_and_trail_2026_08_13.py tests/test_expiry_day_gamma_mode.py tests/test_live_readiness_auth_diagnostics_2026_08_30.py -q --disable-warnings --tb=short
python reports/postfix_verification_20260911.py
```

## Remaining reproducible defects

| Priority | Finding | Observed result | Responsible boundary |
|---|---|---|---|
| High | OI expiry pinning can be bypassed by compatibility fallback | Requested 22SEP2026; legacy adapter returned 15SEP2026; call OI 777 was merged into the next-expiry row | `data/market_data.py::_augment_chain_oi_from_kite` retries without `expiry`, then joins only by strike. Aligning the primary chain does not align the overlay. Conditional on legacy adapter/fallback. |
| High | Current volume value and basis come from different observations | Candidate basis TRADED + current TICK_ACTIVITY 3x => `volume_confirmed=True`; reversing the bases suppresses genuine traded confirmation | `scanner.py::_quality_verdict_for_candidate` prefers candidate basis while reading current context ratio. Can cause both false confirmation and overfiltering. |
| High | Measured zero disappears through truthiness fallback | Current ratio 0 became saved candidate ratio 2 and remained confirmed | Same quality adapter uses `or`, which treats zero as absent. Current ratio, validity and basis must be selected as one observation. |
| High | Stale/missing regime clocks are accepted in REAL mode | A missing 15m clock let epoch-1 5m TRENDING_UP be borrowed; an absent own-frame clock returned ownership=True | `scanner.py::_get_trade_regime`, `_trade_regime_is_own_timeframe`. Requested-frame age is checked instead of the borrowed source age; missing timestamps are explicitly fresh. No replay-only restriction exists for that branch. |
| High | Stale peer states can create market-wide consensus | Four epoch-1 regime labels with no usable cache yielded BULLISH, aligned_count=4 | `scanner.py::_market_breadth_context` falls back to `latest_regimes` without checking each peer's age. |
| High | Session history expansion ignores analysis cutoff | Requested 10:05 with a correctly clipped input; analyzer received full cache through 14:10 | `scanner.py::_build_intelligence_contexts` replaces the supplied slice but never clips the replacement to `as_of`. Conditional replay/API look-ahead; live calls near the newest candle are not proof of occurrence. |
| Medium | Futures minute coverage is incomplete but called reliable | Constant 60 units/min, sampled every 15s, produced baseline45 and projected_ratio1.3333 with reliable=True | Cross-boundary deltas are discarded; projection uses elapsed wall-clock fraction against incomplete historical buckets. The former clock-only 4x bug is fixed, but comparable coverage is not. Getter still labels raw quantity as contracts. |
| Medium | Candle boundary increments remain lost | Cumulative increase400 stored only200 | `data/candle_builder.py::update_from_tick` still rebases on each first new-minute tick. Depends on cumulative-volume input and later history repair; missing allocation/completeness is not exposed. |
| Medium | MTF correction invalidation is bounded too narrowly | Changing volume30 bars back left the hash unchanged | `engine/multi_timeframe.py::_hash_df` fingerprints only20 bars. Recent correction detection is fixed, but older corrections and omitted fields can still leave calculated state unchanged. |
| Medium | An unavailable aggregate can still supply adverse signed intelligence | `{available:False, score_norm:-0.8}` returned -0.8 | `scanner.py::_trade_like_signed_intelligence` checks for a score before validity. Defensive boundary gap; normal aggregate output does not necessarily publish this combination. |

## Audit reconciliation

| Original finding | Current status |
|---|---|
| A1: futures clock rollover | Clock-only spike fixed; coverage/projection incomplete as above. |
| A2: execution expiry / OI overlay | Standard path improved; legacy retry reopens mismatch. Token resolution is conditional on an existing master. Returned overlay identity is not verified. |
| A3: exchange clocks | Kite timestamps retained and the minute-writer rejects old observations. `get_index_activity` still accepts adapter `available`; `_refresh_index_volume_worker` can stamp the separate stale fallback with current receipt time even when it refused the minute observation. Health/fallback validity is only partly repaired. |
| A4: OI provider/horizon | Provider-switch baseline reset added. Horizon metadata added, but `intelligence/oi_tracker.py` does not consume it when interpreting changes. Overlay OI is still subsequently labeled provider FYERS. Strike-universe comparability remains unresolved. |
| B1: measured-volume direction | Repaired and covered: the selected measured bar supplies direction and measurement metadata. Availability/freshness of an older selected measurement still needs consumer validation. |
| B2: zero-volume flow | Recent dead-edge abstention and no-volume session delta repaired and covered. This does not certify all mixed-source flow/structure outputs. |
| B3: timeframe/asset labeling | Aggregator uses requested candle frame and replaces false `futures_volume` alias with measured-volume metadata. This is not implementation of independent futures/options interval histories. |
| B4: option OI-volume/PCR | OI is no longer added to day volume; repeated identical observation IDs are deduplicated. CE/PE cumulative activity still boosts directional flow; Angel fallback lacks the FYERS expiry/observation attrs; PCR history keys omit provider/strike universe. Only part of the audit is closed. |
| B5: quality provenance | New fields exist, but current value/basis conflict and zero fallback remain, as reproduced above. |
| B6: partial bars/as-of | Thrust timezone slicing now fails closed and normalizes clocks. Projection uses a25% progress floor, so very early equal-rate ratios remain biased. A separate session-context cutoff defect now exists. |
| C1: entry snapshot precedence | REAL-mode resolver now prefers fresh exact intelligence and returns empty when missing. The fresh-helper mock test proves precedence, not the entire live freshness chain. Historical/unspecified modes still reuse attached state by design. |
| C2: wrong volume lookup | Sibling volume lookup and removal of buy/sell-ratio substitution are implemented. REAL-mode entry scalar preference removed. Recorded fallback remains outside REAL. |
| C3: invalid exit voters | Flow/pillar signals now check explicit unavailability; aggregate signed-score path still bypasses that check. Repeated confirmation does not prove a new source observation. |
| D1: session history | Previous-day extrema now use immediate prior session. Reading broader cache improves scope but does not prove complete ten-session coverage; the replacement cache ignores `as_of`. |
| D2: regime/breadth freshness | Still incomplete; exact missing clocks and borrowed source age bypasses reproduced. |
| D3: duplicate consensus state | Primitive-only Quad state vote repaired and covered. |
| D4: horizon labels | Panel expanded to60 five-minute bars. Opening range still assumes first6 rows; rare-burst still reads opening `bias` after phase checks instead of `effective_bias`. |
| Conditional candle/cache issues | Source-rebase restoration and tick-count resampling improved; broad homogeneity check narrowed. Boundary loss, older correction invalidation and dependent structure validity remain incomplete. |

## What is still missing from the requested complete system

There is still no demonstrated complete, independent index-participation, futures and selected-option1m series feeding coherent5m/15m features across all four indices. The current futures service is a process-local one-minute snapshot estimator; option activity remains largely cumulative chain snapshots. Meaningful per-interval volume/OI changes require stable contract identity, broker clocks, comparable coverage, and explicit missing-data handling throughout the consumers. Changing aggregator input frames does not supply these absent derivative histories.

The September10 fixes for observed option peaks, retention ordering, primitive provenance, effective phase and advisory MFE policy retain passing focused regressions. Their existence does not remove the remaining September11 defects above.

No new chronological strategy replay, broker capability certification, or full-stack entry-to-exit profitability evaluation was performed here. The previous recorded holdout remained unprofitable and uncertified. The present evidence verifies several code repairs while **rejecting the claim that every audit gap is closed or that trading performance improvement has been established**.
