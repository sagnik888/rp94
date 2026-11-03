# Signal and trade-management fixes — 10 September 2026

This implementation repairs every concrete code defect established by the preceding audit. It does not claim that raw UT or the current full strategy is profitable; the available chronological replay remains negative and is now explicitly prevented from certification.

| Audit finding | Implemented behavior |
|---|---|
| Synthetic option peaks drove real exits | Option `peak_pnl` now accepts only verified premium marks and observed contract ticks. The index/delta reconstruction is retained separately as `projected_peak_pnl`. Futures retain contract-candle extremes. Peak provenance is recorded. |
| Breathing/defer decisions were overridden | The final smart-retention backstop is ineligible during a breathing-room clamp or trend-profit-lock defer in the same decision cycle. |
| Correlated evidence and misleading confidence | RANGING/VOLATILE/UNKNOWN no longer multiply all independent intelligence by 0.15 after the regime already contributed no directional vote and the quality policy separately accounts for regime risk. Consensus expands to EMA and Supertrend primitives without becoming a third vote. Quality verdicts label confidence as `heuristic_score` unless calibration collection and live application are both enabled. |
| Consumers used the opening phase | Live grading, weak-market mode, top-down intelligence, regime conviction, ER promotion, range guards, and trade-budget phase parsing now prefer the effective intraday phase and its phase confidence. |
| Zero-volume price proxy voted as order flow | OHLCV with no measured volume and no usable depth now returns `available=False`/`source=unavailable`. |
| Generic historical MFE overfiltered entries | The directional MFE-floor result remains in the verdict as `expected_move_floor_advisory`; it no longer vetoes otherwise fresh, verified, supported 1.5R geometry. Forecast availability, freshness, provenance, geometry, stop cap, and RR remain required. |
| Strategy/timing differed from a UT-only description | No untested strategy switch or timing threshold was forced. The running engine remains configuration-owned; source provenance and heuristic-confidence semantics now make the combined path explicit. |
| A losing exit policy could be relatively “better” | Certification now also requires positive holdout expectancy and profit factor above 1.0. |
| Market location used a bar-start price and old sessions | Candidates carry a decision timestamp. The live location gate uses observations through that clock and scopes day high/low to the latest session. |
| Competing raw signals were all consumed before selection | Raw collection no longer consumes every eligible identity. Only the selected row is consumed, leaving alternatives available on the following scan when the selected row is rejected. |

## Verification

- Focused audit regression set: **200 passed**.
- Broader UT/Quad Prime, entry-policy, intelligence, option/futures and runner-management set: **320 passed**.
- Full repository suite: **5,877 passed, 4 deselected, 0 failed** in 269.06 seconds.
- Final affected-path rerun after the phase-confidence correction: **81 passed, 0 failed**.
- Python compilation and `git diff --check`: passed.

The executable-peak regression uses the production incident values: prior observed peak ₹6, current option P&L −₹318, and delta-projected peak ₹623.1168. The resolved executable peak remains ₹6. The tests also prove that an exact option-contract tick can raise the peak, while a futures contract-candle extreme remains eligible.

The refreshed chronological exit replay contains 36 replayable sessions and 245 matched trades. On holdout, baseline net is −₹57,233.43 with expectancy −₹369.25 and profit factor 0.3862; the candidate is −₹55,743.54 with expectancy −₹359.64 and profit factor 0.3772. It is less negative on expectancy but still unprofitable, so the new positive-expectancy and profit-factor gates correctly keep `candidate_certified=false` and `lot_increase_allowed=false`.

These results verify the repaired decision mechanics and compatibility of the implementation. They do not establish a positive trading edge. That requires new shadow/live observations produced after these fixes and a replay that executes the complete entry and exit stack against exact option contracts.
