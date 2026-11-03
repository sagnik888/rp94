# UTS intrabar entry and post-entry repaint verification

The existing entry timing switch now controls UT Smart Exit. Intra-candle mode includes closed-candle signals and optional buffered forming signals. Closed-candle mode only admits confirmed signals. For UTS, the advanced forming-entry switch and the main timing switch are synchronized in the dashboard and configure handler. The main switch is authoritative when both fields arrive together.

## Entry timing

- A forming 5-minute or 15-minute signal waits 20 seconds by default.
- A forming 1-minute signal waits 6 seconds by default, configurable from 5 to 7 seconds. The settings model and dashboard command validator enforce that range.
- The buffer starts when the signal is detected, not at the candle's opening timestamp. Entry still depends on the subsequent scan and current admission controls.
- Closed signals skip the forming-signal wait.
- UTS cannot use the generic momentum override to enter after its own flip disappears. A vanished signal is discarded; a returning signal must complete a new wait. A different source candle also starts a new wait.
- When a buffered signal becomes confirmed, its pending copy is removed before execution so it cannot also mature during the same pass.
- Timing changes invalidate existing buffers and refresh results. Saved settings restore the new per-timeframe timing field.

## Post-entry repaint checks

`engine/uts_repaint.py` runs independently of regular scans, stop updates and trade tracking. It samples acknowledged open UTS positions every 10 seconds by default, starting from fill time. Indicator calculations run on a worker thread against a copied candle frame; their private results cannot create entries or replace live or chart caches. The scheduler polls once per second, so actual checks also depend on scheduling and calculation latency.

The worker keeps checking the trade's original signal even if the operator switches future entries to candle-close mode. It performs a confirmation check at candle close without waiting for the next scheduled interval. Confirmation is persisted and ends repaint checks for that trade. Missing, erroneous, stale or mismatched results cannot authorize a repaint exit. An absent flip requires a verified contract mark and passes through the existing exit gateway, which retains Auto/Manual authority, spread handling and broker protection. Pending entries and pending exits are excluded. The generic post-entry tracker is bypassed for UTS.

Regular scanning, trailing, premium profit protection and trade tracking remain active. The existing 5% special, 10% regular and 15% choppy premium stop rules remain unchanged. The filtered 1-minute entry policy and closed-candle reversal policy remain unchanged.

## Verification

- Initial regression proof reproduced the timing and toggle problems: 10 failures and 2 passes.
- The broad default suite ran 7,584 tests: 7,583 passed and one outdated structural assertion failed. That assertion prohibited even reading/removing a pending buffer in the closed fast path. It was corrected to prohibit adding a pending candidate; the new behavioral test proves duplicate prevention at candle close.
- The final focused follow-up passed all 42 tests, including that corrected assertion and 33 new integration tests. These exercise actual execution buffer coordination, settings configuration, dashboard JavaScript, environment restoration, private indicator calculation and post-entry sampling.
- All four normally excluded synthetic integration tests passed separately.
- Across the broad run and final follow-ups, all 7,591 unique current tests have passing results. The broad run was not repeated after the test-only assertion correction and three additional tests.
- JavaScript syntax validation passed. Eight production source hashes remained identical throughout broad verification. One pre-existing pytest warning concerns an unrelated test returning a value.

Machine-readable results: `uts_intrabar_verification_20260930.json`. Detailed results: `uts_intrabar_full_20260930.xml`, `uts_intrabar_followup_20260930.xml`, and `uts_intrabar_optional_integration_20260930.xml`.

## Activation

The local dashboard was unavailable at the final runtime check. Verification used isolated test state and synthetic broker/data boundaries; no live orders or runtime settings changes were made. Start or restart the system to load these source changes, then enable **Intra-candle Signals** for UTS. The advanced UTS timing fields expose the 20-second and 5–7-second entry buffers separately from the 10-second post-entry repaint interval.
