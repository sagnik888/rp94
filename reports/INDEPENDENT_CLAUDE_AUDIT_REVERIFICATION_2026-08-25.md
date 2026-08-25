# Independent reverification of the Claude strategy audit — 25 August 2026

## Executive verdict

**The report's strongest conclusion is supported: the dominant observed loss concentration is an entry-admission problem, not something exit tuning alone can repair.** Its headline can be reproduced exactly: through 21 August, retaining the latest EXIT row per lifecycle produces 327 outcomes, 74 never-green outcomes, -Rs.46,870.75 of loss, 59.135% of the loser pot, a 17.041-second median hold, and the published 22.847% PnL-to-peak ratio.

That is not the whole truth, however. The report silently combines incompatible populations and overstates several causal conclusions:

1. The rtracker population is sourced from session-candidate marks and has no authoritative broker-order/fill join. It must be treated as advisory for this analysis; rtracker alone cannot prove the execution mode or realized live-account performance of every row.
2. The exact 74/327 headline depends on a non-authoritative **latest-exit** dedupe. Three candidate lifecycles emitted 21 EXIT rows, creating 18 excess rows. Earliest-exit retention gives 73 never-green outcomes and -Rs.46,420.25. The approximately 59% concentration survives, but the exact counts do not.
3. The aggregate profit is entirely supplied by 121 legacy, identity-less rows from 8–24 July. The 211 rows with explicit option contracts from 27 July onward lose **Rs.9,791.53**. Combining those eras hides the negative option-era result.
4. The published 22.8% "capture" ratio divides mostly net PnL by gross peak PnL. It is reproducible, but it is not a basis-consistent capture statistic.
5. Several report statements about filters and exits are too absolute or use the wrong metric.
6. The current code contains a previously unreported P1 risk defect: mixed-book routing overwrites the correctly computed ATR-aware option stop with the flat 15% cap.

**Auto-readiness verdict: REQUEST CHANGES / NOT READY for unattended live Auto.** Read-only broker/data connectivity and the closed-market dashboard path pass. Live order placement, fills, protective-order behavior, open-market screen latency, and realized PnL were not exercised. The P1 stop overwrite, contradictory entry guards, unvalidated default-on strategy changes, missing counterfactual telemetry, and confirmed host resource exhaustion prevent a responsible live-auto certification.

No live order was placed, modified, or cancelled during this reverification.

## Review findings

| Priority | Finding | Evidence location |
|---|---|---|
| P1 | Mixed-book option routing destroys the candidate's ATR-aware stop/target and widens risk to 15% | `engine/signal_processor.py:6522`, `scanner.py:25335`, `scanner.py:25912` |
| P1 | The new default-on BEARISH stand-aside blocks both directions and therefore re-blocks the downtrend shorts the new census waiver was designed to admit; replay does not receive the same waiver context | `config/settings.py:296`, `scanner.py:21469`, `engine/signal_processor.py:3323`, `engine/signal_processor.py:5996` |
| P1 | The host exhausted virtual/physical memory repeatedly and the service disappeared twice; the launcher adds diagnostics but no machine-wide singleton, memory ceiling, or supervised restart | `Start_UT1.bat` plus Windows System events 26/2004 |
| P2 | Grade adds no information beyond confidence, and confidence has an ADX=40 discontinuity | `engine/signal_manager.py:171`, `engine/signal_manager.py:327`, `engine/signal_manager.py:372` |
| P2 | Lifecycle EXIT duplication makes terminal-outcome selection ambiguous | `data_store/rtracker/events/*.jsonl` |
| P2 | Historical PnL/peak/path units are mixed and only five latest rows declare their bases | `intelligence/rtracker.py:963`, `intelligence/rtracker.py:1105` |
| P2 | Runner activation/provenance is not inherited onto EXIT events; runner stops collapse into generic STOP_HIT | `scanner.py:18096` |
| P2 | Peak trail is default-on from only five changed lifecycle paths; the new 15-second rupee-guard hold lacks a combined protection-path test | `config/settings.py:845`, `scanner.py:17163` |
| P2 | Several market-intel/risk settings are empirically dead or arithmetic no-ops | `intelligence/regime_detector.py:123`, `scanner.py:9509`, `scanner.py:14767`, `config/settings.py:1715` |

### P1 — ATR-aware option risk is overwritten after selection

The signal processor correctly derives a premium stop from timeframe, ATR, delta, and configured bounds. Later, mixed-book routing calls `_force_candidate_to_options()` even when the candidate is already an option. That function rebuilds stop and target from `options_sl_pct`, currently 15%, rather than preserving the candidate's already-computed levels.

The real 24 August tape proves the mutation:

| Signal | Stabilized stop | Accepted stop |
|---|---:|---:|
| NIFTY 5min, premium Rs.167.85 | Rs.161.136 — 4.00% | Rs.142.6725 — 15.00% |
| SENSEX 5min, premium Rs.476.70 | Rs.455.708 — 4.40% | Rs.405.195 — 15.00% |

A direct unit reproduction with premium 100 and stop 96 similarly returns stop 85. Existing mixed-book tests pass because they assert allocation/contract conversion, not preservation of risk geometry.

Required fix: when a candidate is already a valid option with a fresh premium and valid stop/target, preserve those levels. Recompute only when conversion or remarking is genuinely required, and add an end-to-end assertion from STABILIZED through ACCEPTED/order input for stop, target, initial stop, and R.

### P1 — the two entry fixes contradict each other

The census bug is real: it counts the system's own historical arrows, not price, so a long-heavy morning can label a later short "counter-bias" on a confirmed downtrend day. The new live waiver correctly lets a confident `TREND_DAY_DOWN` short outrank that stale census.

But the current worktree then applies a blanket default-on rule that rejects **both LONG and SHORT** whenever `session.bias == BEARISH`. It therefore neutralizes the census repair for the cited bearish-short scenario. The replay census call also omits `day_type` and `day_type_confidence`, so live and replay decisions differ.

The statistical rationale for the blanket gate is not sufficient:

- the 40-row cohort is selected from the same unverified session-candidate data used to design the rule;
- it mixes legacy and option-schema eras;
- recorded rtracker PnL is already normally net of Rs.80, but the claimed Rs.12,279 benefit subtracts a fresh Rs.130 from every row, double-counting most transaction cost;
- the claimed sign stability by time-third depends on that extra subtraction; and
- it deliberately blocks bearish shorts despite the session label and price trend agreeing with them.

Required fix: ship this stand-aside OFF as an experiment, version the strategy on every signal, and evaluate a preregistered rule on held-out sessions using executable option marks. Pass identical day-type inputs to live and replay, then add a final-disposition test that runs census, session bias, readiness, and stabilization together rather than testing each helper in isolation.

### P2 — exit-policy evidence and combined coverage are still thin

Peak trail was historically off. The effective dirty worktree turns it on and retunes the gate from Rs.600 to Rs.500, despite a nearby comment saying only the flag changed. A normalized replay over 92 unique usable paths shows:

| Policy | Book result | Increment | Changed positions |
|---|---:|---:|---:|
| Trail off | Rs.5,228.23 | — | — |
| 40% giveback above Rs.600 | Rs.6,150.23 | +Rs.922 | 4 |
| 40% giveback above Rs.500 | Rs.6,354.23 | +Rs.1,126 | 5 |

The direction is encouraging and no one of nine sessions is worse at Rs.500, but five affected positions are not enough to make a live default robust. The sample is also conditional on path availability and does not contain post-exit marks.

The new `rupee_loss_band_min_hold_seconds=15` is based on one 2.59-second exit classified as a stale entry mark. The change is safer than the isolated test makes it look: it delays only the secondary rupee-band decision. TradeManager still has an immediate software stop, arms/adopts broker SL-M protection after a confirmed fill, can fall back to server-side GTT/recovery, and auto-flattens a confirmed unprotected position after the configured five-second grace. A focused safety selection passed 19 tests. The remaining problem is coverage and attribution, not a demonstrated naked 15-second window: no one end-to-end REAL-lifecycle test proves those protections coexist during seconds 0–15, and the original quote is not labeled with enough provenance to prove it was stale.

Required fix: keep peak trail experimental until a frozen, basis-normalized fixture and held-out forward window pass. For the loss hold, retain the protection stack but add one combined fill-to-15-second test and record entry/exit quote verification so the stale-mark rationale can be audited directly.

### P1 — process reliability is an Auto blocker

Windows recorded low/out-of-virtual-memory and resource-exhaustion events on 24 August at approximately 10:55, 14:11–14:12, 15:12, and 15:28. At 15:28 the event recorded 68.14/68.41 GB committed and three Python processes using approximately 2.41 GB, 2.01 GB, and 1.54 GB. The session tracker shows the app up until 10:59, absent from 11:00–14:07, up again from 14:08–15:30, and absent after 15:31.

This strongly supports two service disappearances during machine-wide resource exhaustion, but does not prove Python itself raised OOM or that exactly four builds caused it. Retained telemetry lacks command lines, workspace roots, exit codes, and graceful-shutdown markers.

Required fix: enforce one live engine per account/workspace with an OS-level lease; record PID/command/workspace/build/working-set/commit charge; alert before commit exhaustion; cap parallel test/build jobs on this host; and supervise restart into Auto OFF with broker reconciliation. Stderr redirection alone is diagnostic, not remediation.

## Quantitative reproduction

The new read-only script `scripts/audit_rtracker_entry_admission.py` makes the cohort and retention choices explicit.

### Exit rows and dedupe sensitivity

| Population | Outcomes | Never green | Never-green PnL | Share of loser pot | Median never-green hold | Published PnL/peak ratio |
|---|---:|---:|---:|---:|---:|---:|
| Through 21 Aug, latest EXIT retained | 327 | 74 | -Rs.46,870.75 | 59.135% | 17.041s | 22.847% |
| Through 21 Aug, earliest EXIT retained | 327 | 73 | -Rs.46,420.25 | 59.135% | 16.919s | 23.184% |
| Through 24 Aug, latest EXIT retained | 332 | 76 | -Rs.47,199.75 | 59.124% | 19.342s | 22.446% |

Raw telemetry has 350 EXIT rows. Three lifecycle collisions account for all 18 excess rows:

- MIDCPNIFTY 10 Aug: 3 exits for one entry;
- BANKNIFTY 14 Aug: 8 exits for one entry; and
- NIFTY 19 Aug: 10 exits for one entry.

The scanner's outcome-signature behavior makes earliest terminal retention arguably closer to runtime semantics; the external report used latest. The correct practice is to fix duplicate lifecycle emission and preserve a single immutable terminal outcome, not choose whichever exit makes an analysis convenient.

An additional join check links 331 of 332 latest-retained exits to an ACCEPTED admission. The final retained Aug-19 NIFTY collision instead joins only to a REJECTED row, another reason not to treat the current lifecycle table as canonical ground truth.

### The schema-era split changes the interpretation

| Latest-retained cohort | Dates | N | Wins | PnL | Never green | Never-green share | Published PnL/peak ratio |
|---|---|---:|---:|---:|---:|---:|---:|
| Explicit option contract | 27 Jul–24 Aug | 211 | 70 | **-Rs.9,791.53** | 50 | 63.852% | **-11.703% mixed-basis** |
| Legacy identity-less | 8–24 Jul | 121 | 57 | +Rs.54,399.83 | 26 | 48.541% | +47.277% |

Through the report's 21 August cutoff, the explicit-option cohort has 206 outcomes, loses Rs.9,220.03, and has 48 never-green outcomes costing Rs.34,904.25, or 63.918% of that cohort's loser pot. Its published net-PnL/gross-peak ratio is -11.151%. As a transparent sensitivity, adding an assumed Rs.80 round trip back to every final PnL produces an approximately basis-aligned gross ratio of **+8.781%**; 202 of 206 rows reconcile to that Rs.80 gap. This is still not authoritative fill accounting, but it shows how materially the mixed units distort the word "capture."

The report's overall positive PnL and old approximately 47.5% capture narrative are driven by the identity-less era. Those 121 rows do not explicitly carry contract/quantity/lot/basis, so exact normalization and contract replay are not defensible; coarse charge inference is possible but should remain labeled as inference. Strategy revision is not recorded on the historical rtracker exits, so code-version drift cannot be controlled either. Primary decisions should therefore use versioned, explicit-option, rolling out-of-sample cohorts—not the combined headline.

### Grade and confidence

All-date latest-retained values reproduce the report:

| Grade | N | Win rate | Average PnL |
|---|---:|---:|---:|
| A+ | 97 | 34.02% | +Rs.151.03 |
| A | 151 | 41.06% | +Rs.129.23 |
| B+ | 37 | 37.84% | **+Rs.219.75** |
| B | 20 | 45.00% | -Rs.21.69 |
| C | 27 | 33.33% | +Rs.101.76 |

Confidence at or above 0.95 has 28 outcomes, four wins, a 14.286% win rate, -Rs.3,109.50, and 12 losing session dates out of 13. In the explicit-option cohort it is worse: 25 outcomes, two wins, 8% win rate, -Rs.5,365.50, with every one of 12 dates negative.

The mechanism is substantially confirmed:

- grade is exactly `confidence_to_grade(confidence)`, so the two displayed fields are one predictor;
- technical, intelligence, and regime contributions are added before confidence is bucketed;
- intelligence itself already includes regime, creating trend/regime double-counting;
- not every intelligence component is a trend proxy—OI, PCR, flow, volume, and divergence can contribute independent information—so "all three pillars are collinear" is too absolute;
- under a fixed input sweep confidence reaches 0.93245 at ADX 40 and drops to 0.82 at ADX 40.0001 because the exhaustion cap activates strictly above 40; and
- at least 0.95 does not mathematically require full trend alignment: quad-prime agreement can lift 0.93245 to 0.96245 even in RANGING. It is nevertheless dominated by aligned-trend cases in telemetry (62 of 66 high-confidence signal rows).

Conclusion: grading does need redesign as an independently validated ranker, but the replacement should be judged on held-out calibration/discrimination, not merely on making grade numerically separate from confidence.

## Claim-by-claim verdicts

| External report claim | Verdict | Independent finding |
|---|---|---|
| 74/327 never-green; -Rs.46,871; 59%; 17s | **Confirmed with scope qualifier** | Exact only through 21 Aug with latest-exit retention. Earliest retention gives 73; current latest lifecycle snapshot gives 76/332. |
| Entry admission causes most damage | **Confirmed descriptively** | Approximately 59–64% of the loser pot is concentrated in never-green lifecycle marks. Causality and realized fill PnL remain unproved. |
| Capture is 22.8%, not 47.5% | **Reproducible but mislabeled** | Numerator is mostly net and denominator gross. The 47.3% legacy cohort versus -11.7% option cohort explains the conflict better than one combined number. |
| Grading needs replacing | **Supported** | Grade is only a confidence bucket and ordering is poor, particularly in the explicit-option cohort. |
| A+ has the lowest win rate | **Literally false, directionally concerning** | A+ is 34.02%, but C is lower at 33.33%. A+ remains worse than A, B+, and B and is especially poor in the option era. |
| All three pillars are collinear trend proxies | **Qualified** | Regime/trend evidence is double-counted, but intel contains non-trend inputs too. |
| Highest grade is a late-trend detector | **Supported, not exclusively** | ADX/trend alignment dominates high confidence, but quad-prime agreement provides another route. |
| Repaint 21 and readiness 15 killed 53 shorts | **Counts confirmed, interpretation refuted** | These are 53 lifecycle-stage REJECTED rows, only 35 unique short signal tuples, with overlaps: repaint/readiness 11, repaint/filtered 6, readiness/filtered 3. They are not additive unique missed trades. |
| Refused cohort was A+ at 0.96 | **Refuted as cohort description** | Repaint rows were 14 C, 3 B, 4 A+; readiness rows were 10 C, 3 B, 2 A+. A+ 0.96 describes only a late subset. |
| Counter-census was wrong | **Confirmed** | It counted self-emitted arrows and contradicted a high-confidence downtrend day. Current fix is incomplete/neutralized by the blanket BEARISH stand-aside. |
| Five ATR definitions; two not true range | **Directionally confirmed, count imprecise** | At least six implementation groups collapse to roughly three formula families; two subsystem groups use H-L range instead of true range. |
| VOLATILE ATR limb can never fire | **Confirmed empirically, not mathematically** | Threshold floors at 0.5; 67,378 archived finite samples max at 0.31 and zero reach it. A future extreme could exceed 0.5. |
| Chop widening, high-vol lock, RR max are no-ops | **Confirmed under current config** | 15→max(15,15), 0.85→min(0.85,0.85), and RR max 12 above the reachable approximately 5.02 ceiling; 1m/5m are capped at 2.5 anyway. |
| ATR-aware 5min option stop is always 4% | **Refuted** | Of 45 Aug-24 candidates, 24 were 4%; 21 ranged above 4% to 8.574%. The later forced 15% overwrite is the actual bug. |
| Peak trail was off and cap is off | **Historically confirmed** | HEAD is off/Rs.600; dirty worktree is on/Rs.500. Giveback cap remains off. |
| SMART lock arms at 5% of position cost | **Refuted** | Non-scalp base is 2.5%, scalp 5%, both bounded by R-based arms. At premium 100 × NIFTY lot 65: Rs.162.50 versus Rs.325. |
| Two-thirds never arm any profit guard | **Unsupported** | Current raw retrospective says 42.3% reach SMART arm; breakeven is another guard and 209/350 raw EXIT rows have peaks above its Rs.150 arm. Raw rows include duplicate lifecycles. |
| Zero trails ever fired | **Only narrowly true** | Zero `PEAK_TRAIL`; five `PEAK_GIVEBACK_CAP`; eight `SMART_PROFIT_LOCK`. Generic runner incidence cannot be recovered because provenance is lost and runner stops serialize as STOP_HIT. |
| Winners take Rs.287 heat before working | **Metric mislabeled** | `max_drawdown` is peak-to-later-trough, not pre-green MAE. On 31 pathed winners, median actual pre-first-green adverse PnL is Rs.0; only four first went negative. |
| 61% of losers were closed inside winner noise | **Descriptively similar, causal inference unsupported as stated** | Around 60–63% fall below Rs.270–287 on raw rows. Tick paths stop at exit, but exact-contract 1-minute archives can bound many later outcomes; that analysis still needs explicit intrabar/fill assumptions. |
| `pnl_path` ends at exit | **Confirmed, but not a total data dead-end** | Exit construction explicitly copies/freezes it. Tick-accurate later exits are unavailable; 71/98 pathed lifecycles nevertheless have continuous valid exact-contract 1-minute OHLC through 15 minutes after exit. |
| `pnl_path` begins 12 Aug | **Almost** | Four one-sample paths exist on 11 Aug; useful multi-sample paths begin 12 Aug. Latest-deduped corpus has 98 paths, 92 with at least three samples. |
| No rejected-signal outcomes | **Economically correct, literally too broad** | Code scores rejected clusters on later underlying candles (63/66 on Aug 24, 36 positive at 30m), but has no selected-option bid/ask/fill/charges/rupee counterfactual. |
| Two OOM deaths from four builds | **Strong correlation; exact attribution unproved** | Resource exhaustion and two disappearances are evidenced. Three multi-GB Python processes are identified; exactly four builds and direct kill cause are not retained. |

## Filter evidence on 24 August

The raw 53 short REJECTED event rows break down as 21 stabilization-repaint, 15 live-readiness, and 17 filtered-candidate rows. They represent only 35 unique short signal tuples because one signal can be logged at several lifecycle stages. Fifteen later stabilized, none were ultimately accepted, and 11 appeared first as repaint rejects and later as readiness rejects.

Across the full archive there are 67,627 REJECTED event rows. None systematically carries executable option outcomes: 11 have a nonzero exit/PnL/path field, which is incidental/malformed overlap rather than a shadow outcome book. Separate missed/rejected reports do contain substantial underlying-index scoring: over the same 28 exit dates, 4,250 unique clusters were formed and 3,649 (85.86%) received a 30-minute/EOD score. That can estimate directional opportunity in index points, but cannot substitute for option premium, spread, fill, slippage, charges, or the selected contract. Therefore the economic marginal benefit/cost of each guard remains unmeasurable.

The defensible conclusion is not "leave every guard alone" or "remove them." It is: avoid broad default-on changes until each guard is evaluated on unique signal lifecycles with a common executable shadow outcome and versioned decision trace.

## Trade-management findings

The report correctly identifies telemetry limits but overstates what existing fields mean:

- `max_drawdown` tracks giveback from a running peak; it is not entry MAE or "heat before working";
- normal runner trailing uses 15% of original target distance, not a universal 15% peak-giveback allowance;
- the cascade runner locks 85% of open gain, and its high-volatility adjustment is currently a no-op because both configured values are 0.85;
- `pnl_path` is deliberately frozen at exit, so it alone can replay only earlier/tighter exits;
- 85 of 98 latest-deduped paths end at gross path PnL exactly Rs.80 above final net PnL, exposing the basis mismatch; and
- only the five 24-Aug rows explicitly declare `pnl_basis`, `pnl_path_basis`, and `round_trip_cost`.

All 98 pathed lifecycles are in the explicit-option cohort, giving 46.45% path coverage there (98/211), not 29.52% of the mixed-era population as the only useful denominator.

There is a useful recovery path the external report missed. Exact-contract one-minute archive files exist on the trade date for 73 of the 98 pathed lifecycles. Their latest timestamps extend at least 15/30/60/120 minutes after exit for 72/70/69/57 rows. After additionally requiring valid OHLC, a first bar within 90 seconds, and no gap above 90 seconds through the horizon, the continuous counts are **71/68/66/51**. These bars can bound some wider-stop/later-exit price paths under explicitly declared OHLC ordering and fill assumptions. They cannot reproduce tick order inside the minute, quotes/spreads, or a guaranteed executable fill, so they complement rather than replace a post-exit executable quote path.

Before tuning exit policy, telemetry should persist:

1. executable option bid/ask/last and quote quality from signal time through at least 30 minutes/EOD for accepted and rejected shadow trades;
2. the same executable quote path after an actual exit (one-minute OHLC already provides lower-resolution bounds for many rows);
3. `pre_green_mae`, `post_green_drawdown`, `peak_giveback_pct`, and basis/charges as separate fields;
4. runner activation time, peak, computed trail, stop provenance, and a distinct `RUNNER_TRAIL_STOP` exit reason; and
5. strategy/config revision and executable contract identity on every lifecycle event.

## Live-system and broker evidence boundary

The read-only `scripts/full_system_audit.py` run completed successfully in the current closed-market environment:

- Kite and Fyers authenticated;
- four Kite index tokens loaded;
- approximately 1,800 recent 5-minute candles per index were fetched through 24 Aug 15:25;
- Fyers volume alignment was valid for NIFTY, BANKNIFTY, SENSEX, and MIDCPNIFTY;
- all four Fyers option chains returned 61 strikes and active expiries;
- the four cached Kite websocket spot quotes were present at approximately 1.78 seconds receipt age; and
- all were correctly labeled closed-market/context-only rather than live.

This verifies the read-only data/intelligence path at that moment. It does **not** verify open-market exchange-to-screen delay, broker order placement, acknowledgement ambiguity, fills, partial fills, protective-order modification, scale-in, square-off, or restart reconciliation against a real open position.

Database evidence reinforces that distinction:

- `session_signals`: 566 rows, sum of `executed` = 0;
- `trades`: 196 mixed historical/session/test rows; 192 have `status=CLOSED`, while 181 still have `execution_status=OPEN`; none has an exit order ID; and
- `order_fills`: eight rows, all matching the known `T-NIFTY`/`T-BANKNIFTY`/`T-SENSEX`/`T-MIDCPNIFTY` fixture identities.

Those tables are useful internal/advisory ledgers, but they do not prove a live broker round trip. The current architecture is Kite as order authority and Fyers as secondary data/option-chain source; it is not a two-order-broker execution engine.

For the dashboard spot badges, the prior polling isolation is structurally present and the closed-market local endpoint is fast/fresh. Because the market was closed during this run, the required open-market claim remains unverified: exchange timestamp to rendered badge age below three seconds at the desired percentile while the scanner is under full load.

## Required order of work

1. Fix the P1 mixed-book stop/target overwrite and add end-to-end preservation coverage.
2. Turn the blanket BEARISH stand-aside and peak-trail policy into experiments, not live defaults; restore live/replay parity and add combined protection coverage for the rupee-loss hold.
3. Stop duplicate EXIT emission and assign one immutable lifecycle ID plus one terminal outcome.
4. Add versioned, basis-explicit executable shadow outcomes for accepted and rejected signals, continuing after real exit.
5. Rebuild/calibrate ranking on independent features using rolling held-out option-era sessions. Optimize admission first, but report calibration, discrimination, frequency, PnL after costs, and tail loss—not win rate alone.
6. Add machine-wide process governance and prove restart/reconciliation with Auto OFF after forced termination.
7. Run a full open-market, Auto-OFF soak over all four indices and three timeframes; measure quote exchange age, receipt age, screen age, scan latency, memory/commit growth, and broker/data feed divergence.
8. Only after those pass, perform an explicitly authorized smallest-lot canary covering entry, partial/complete fill, protective stop, broker reconciliation, and exit. A read-only audit cannot certify this final step.

## Reproducibility and tests run

Commands/evidence used:

```text
python scripts/audit_rtracker_entry_admission.py --through 2026-08-21
python scripts/full_system_audit.py
python -m pytest -q <13 focused audit/management tests>
python -m pytest -q <logic/reachability selection>
Windows System/Application event log queries for Event IDs 26 and 2004
read-only SQLite queries using mode=ro
```

Results:

- **Full suite: 4,996 passed, 12 skipped, 4 deselected, 0 failed in 108.78 seconds**;
- 109 focused current-worktree tests passed;
- 84 logic/reachability tests passed, 3 skipped;
- 96 management/runtime tests passed;
- 19 focused protection-stack tests passed;
- 5 new audit-script tests passed;
- read-only dual-broker/data audit passed in closed-market mode.

The script deliberately labels the PnL/peak ratio `published_mixed_basis_capture_pct`, exposes collision details, reports earliest and latest retention, and separates explicit-option from legacy identity-less cohorts. It does not mutate telemetry unless `--json-out` is explicitly supplied.

## Bottom line

Claude found the right center of gravity—bad admission dominates the measured losses—but its exact headline is more fragile and its live-auto recommendations less safe than presented. The highest-confidence rank is not trustworthy, the option-era cohort is already negative, and the telemetry cannot economically price rejected opportunities or tick-accurately replay later exits (although continuous minute-bar bounds exist for most pathed rows). At the same time, current code can widen a 5-minute option stop to 15%, while current worktree changes can block the very bearish shorts a census repair admits and alter live exit behavior on very small in-sample evidence.

Treat the system as a **research/advisory engine with passing read-only connectivity**, not as certified unattended Auto, until the three P1 findings and the open-market/live-order gates above are closed.
