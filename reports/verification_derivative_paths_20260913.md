# Verification of derivative data paths — 13 September 2026

Read-only investigation of the current implementation. Production code was not changed. The accompanying Python probe calls actual analyzer and quality methods without initializing brokers, sending network requests, placing orders, or writing production state.

## Result

The earlier repairs improve data separation and prevent several false signals, but do **not** complete the user's requested derivative-aware entry-to-exit behavior. The new 1/5/15-minute contract features are exposed as telemetry without decision consumers. This does not establish that this omission caused the reported 30-day loss; that requires identifying and replaying the actual run.

## Findings

### P1 — New derivative intervals do not influence trading decisions

`scanner.py:184` installs the derivative source. `intelligence/intelligence_aggregator.py:221-232` fetches it and copies the result into the bundle. `scanner.py:18014` and `scanner.py:29850` copy that field into trade and candidate contexts. A production repository search found no code consuming this field inside aggregate scoring, regime classification, entry quality, tracking/trailing, or exits.

The production-method counterfactual probe confirms this in all twelve index/timeframe combinations: with identical index candles, changing eligible exchange-timed futures/options observations from price +100, OI +10,000 and 10x participation to price -100, OI -10,000 and 0.1x participation leaves aggregate output, regime output and entry quality unchanged. This establishes disconnection; it does not prescribe a directional trading rule for options volume.

The prior test named `test_aggregator_keeps_derivatives_separate_and_forwards_them_to_live_consumers` (`tests/test_remaining_audit_repairs_2026_09_11.py:227`) asserts data forwarding at lines 237/242/243, not a downstream decision that uses the fields. Its pass therefore cannot prove the original behavioral acceptance condition.

There is a separate, older **1-minute futures rate** path: `scanner.py:9698` reads it, `scanner.py:9790` forwards it to the pre-candle predictor, and `engine/pre_candle_predictor.py:373-378` adds predictor confidence for elevated participation. The optional entry veto reads that prediction at `engine/signal_processor.py:6827`, guarded by `pre_candle_flip_veto_enabled` at line 6850 (default False in `config/settings.py:2868`). This is not the new multi-timeframe service and is not an options interval/trade-exit consumer.

### P1 — The 30-day backtest does not replay these derivative inputs

`scanner.py:31260` reconstructs historical intelligence; its analyzer call explicitly supplies `options_chain=None` at line 31356. The derivative source is a live in-memory store (`data/derivative_participation.py:29`), reset at session/counter boundaries (line 35), with no broker-history loader in the service. Historical as-of filtering correctly excludes later observations, but cannot recreate earlier sessions.

A probe with a populated live futures history queried one day earlier returns `available=False`, `reason=stale_observation`. Historical options OI, PCR, chain Greeks and the new derivative intervals therefore cannot be validated by this current candle-only historical scoring route. A loss on this route is a real concern about that simulated strategy; it is not an end-to-end test of all the live broker-data repairs.

### P2 — FYERS short-term OI changes are withheld even when snapshots could measure them

`data/market_data.py:6305` always prefers nonzero broker-reported OI change. Line 6332 gives that change a day horizon (86400 seconds). `intelligence/oi_tracker.py:108` rejects horizons longer than the requested timeframe, so normal nonzero FYERS reported day changes cannot contribute OI direction on 1, 5 or 15 minutes. The stored local snapshots do not replace the reported field in this case, and the separate interval service has no OITracker consumer.

The production probe takes two FYERS snapshots 90 seconds apart with a real +1000 put-OI change per strike. The output remains `fyers_reported`, horizon 86400, and `change_comparable=False` / `NEUTRAL` for all three requested timeframes. Rejecting the day change is correct; completing a separate eligible rolling-change consumer remains unfinished. Conversely, when the provider-reported changes are zero, the fallback is a poll-to-poll delta: a 90-second delta may be accepted by both the 5-minute and 15-minute OI analyzers. It is not the corresponding full-window OI change.

## Current broker roles

| Data lane | Current route | Limit relevant to acceptance |
|---|---|---|
| Index candle volume | FYERS index history, explicitly labeled at `data/market_data.py:5116-5128`; carried cumulative FYERS quantity on index ticks at line 3041 | Futures quantity is correctly kept out of this candle column. Live incomplete volume may abstain until history repairs it. |
| Futures observations | Kite near-month quote at `data/kite_market_data.py:673`, selected by actual expiry; provider reads it at `data/market_data.py:3421` | This is a Kite-only futures lane, with 15-second refresh throttle at line 2989, not interchangeable three-provider futures failover. |
| FYERS options | Primary option chain, optional exact-expiry Kite OI overlay; snapshot registration at `data/market_data.py:6354` | Chain quantities are receipt-timed; minimum cache lifetime is 90 seconds (`5839`), too sparse for eligible 1-minute features. |
| Angel options | Five nearest complete CE/PE pairs when primary fails, beginning `data/market_data.py:6074` | Receipt-timed and narrower coverage, not a full equivalent chain or exchange-timed 1-minute series. |
| Kite selected options | Registered exact contracts and FULL ticks at `data/market_data.py:3191`; called by tick handler at line 2136 | Can build interval data after real samples arrive, but current decisions do not consume the new interval payload. It is not a complete option-universe feed. |

The standalone regime detector is driven by index price indicators; derivative intervals were not added to its inputs. This may be an intentional component design, but the aggregate market-intelligence layer also does not use the new intervals, so the promised broader participation-aware behavior has not been demonstrated.

## Reproduction

Run `python reports/verification_derivative_paths_20260913.py` from the workspace root. Results are in `verification_derivative_paths_20260913.json`. All twelve counterfactuals and the FYERS OI/historical-store probes completed successfully. No full suite, live broker test or profitability claim is part of this verification.
