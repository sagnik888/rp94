# Replay execution repair evidence — 13 September 2026

Owned product file: `engine/signal_processor.py`. Original preserved at `reports/replay_baseline_source_20260913/engine/signal_processor.py` before edits. No app restart, broker orders, live settings changes or network requests were performed by this repair task.

## Implemented

- Historical reconstruction copies raw signals, preserves `source_signal_timestamp`, and uses source bar start plus timeframe duration as `timestamp` / `signal_available_at`. Original MTF signals are unchanged. Historical grade, instrument routing, late entry gate, opposite-arrow boundary and EOD reconstruction therefore receive a knowledge time. Stable historical IDs retain the raw signal identity; stored Trade.signal_time retains the raw source time and entry_time records execution eligibility/fill.
- Historical 15-minute trend agreement requires that the confirming arrow's candle has closed and abstains when only the final session state is available. Historical compression and fallback regime slices exclude unclosed bars. The final unpaired arrow now obeys the entry cutoff too.
- Option replay enters at the first covered one-minute open at/after availability, checks the same candle for resting stops and targets, evaluates close-driven management at the minute close, and never reads beyond the opposite signal / session boundary. UTC inputs normalize to IST without changing their instants.
- Stops win ambiguous both-level OHLC candles. A gap below a long option stop fills at the lower open. Close-only profit guards fill at the observed close, not a newly calculated stop outside the candle. Existing resting stops are evaluated before close-only guards. New trailing stops apply to the next candle.
- An absent or invalid minute before exit makes that price replay unavailable. Data defects after an already reached exit cannot retroactively change the entry/exit. No result is fabricated from an early last available close. The strict fetch-or-skip path cannot replace a failed actual option simulation with a synthetic premium.
- Archived lookup requires the exact strike and a verified nonexpired contract expiry. Explicit dated symbols are parsed; month-only Kite symbols require the local instrument master. Configured expiry rollover reuses the existing expiry resolver. A missing requested strike no longer silently becomes the nearest available strike. Historical broker-resolver fallbacks reject missing expiry/strike identity and snapped strikes.
- Replay traces retain the input hash, contract symbol/expiry/strike and verification status, eligibility/entry times, fill assumptions, guard trace, and final fill. Full historical intelligence supplied by Scanner._get_historical_intel_context_at is attached to post-entry management; it is not replaced by only aggregate/regime scalars.
- Historical estimated premium does not average future same-day VIX. Date-only requests consult the historical resolver at midnight; missing VIX leaves baseline geometry, rather than claiming measured volatility.
- Paired FUT-labelled index-proxy replay uses covered one-minute entry-open prices and handles gaps conservatively. Actual futures contract price replay was not added; EOD/shadow FUT paths remain existing index proxies and are not evidence of real futures execution fidelity.

## Verification

`venv/Scripts/python.exe -m pytest tests/test_replay_causality_20260913.py tests/test_option_history_cache.py tests/test_backtest_can_see_live_exits_2026_08_18.py tests/test_replay_calibration_pipeline_2026_09_09.py tests/test_backtest_sweep_hygiene.py tests/test_option_conversion_keeps_the_compression_cap_2026_08_04.py -q --disable-warnings --tb=short --junitxml=reports/replay_causality_20260913.xml`

**65 passed, 1 warning, 3.23 seconds.** Includes 14 new behavioral cases. Three existing fixture expectations were corrected: incomplete 15-minute bars are unavailable; entry-at-open permits first-bar management and resting stops; intrabar target time records the observation at minute close rather than claiming the bar-open instant.

Real local archive probes:

| Request | Selected contract | Verified expiry | Day bars |
|---|---|---|---:|
| MIDCPNIFTY 14350 PE, 11 Sep | MIDCPNIFTY26SEP14350PE | 29 Sep 2026 | 375 |
| NIFTY 23300 CE, 11 Sep | NIFTY2692223300CE | 22 Sep 2026 | 385 |
| BANKNIFTY 57500 CE, 1 Sep | BANKNIFTY26SEP57500CE | 29 Sep 2026 | 385 |

These probes establish contract resolution, not profitability. The parent task performs a separate frozen-input full replay; its results must be read before making a performance claim.

## Provenance follow-up

All paired, EOD and futures-shadow rows now include a `replay_input.price_basis`: `OBSERVED_OPTION_OHLC`, `SYNTHETIC_OPTION_ESTIMATE` or `INDEX_PRICE_PROXY`. Explicit booleans identify observed option prices, synthetic option prices, actual futures prices and shadow rows. Existing `data_provenance=BACKTEST` remains the compatibility replay-family tag, preserving accounting behavior. Existing observed price hashes and exit guard traces are retained.

The bounded follow-up passed **26 tests, 1 warning, 2.98 seconds** in the new causality, replay hygiene and evidence visibility suites (`reports/replay_provenance_20260913.xml`). Released signal_processor.py SHA256: `F6D7397BC3F49A6484BE010EED37167BC23F44395EFE50D338005A133482DDBD`.

## Final historical guard integration

A direct production-method probe found that attached historical context was accessible through the trade-context fallback, while `_fresh_exact_trade_intel` and `_get_trade_intel` returned `None`. Fifteen-minute chain readers use the direct method. The full context also lacked the top-level timestamp used by `_intel_age_seconds`, causing a fresh historical observation to receive unknown-age discounting.

Both historical management adapters now bind their instance's exact reader to the source Scanner's full historical context at the replay candle clock. Live Scanner class methods and live cache behavior remain unchanged. The historical full-result cache stamps its actual decision `as_of` and timeframe. Regression checks prove direct readers return the OI/PCR/derivative context, freshness is zero seconds at the analysis instant, post-entry chain checks can recognize a genuinely post-entry recorded observation, and an arbitrary future wall-clock argument cannot move the replay input clock.

Historical intelligence now also obtains strike spacing from the index configuration, with the existing four-index defaults, rather than applying NIFTY's 50-point spacing to every index. Twelve index/timeframe assertions verify the values delivered to the aggregator.

**116 focused tests passed, 1 warning, 4.23 seconds**, including the causal VIX fixture update (`reports/replay_context_integration_20260913.xml`). Latest released hashes: signal_processor.py `96569116B904B77092819619F4F87464D4C099C2CDAB1E32CD50712B4770AEF3`; scanner.py `EAA6F3BB693FBAC01812F8B3DC9A38F8BD00E639B84D68AD69161AAA2E8831A3`.
