# RP94 Intraday Index Trading — Hybrid Profitability and Risk Audit
**Date:** 19 November 2026  
**Baseline:** `main@ce792f12ee3d74762a1cd450f12dff5be903a529`  
**Scope:** NIFTY, BANKNIFTY, SENSEX, MIDCPNIFTY; underlying signal -> derivative contract -> entry -> actual fill -> exit -> booked P&L; archived causal replay and current risk code.  
**Method:** Read-only review of repository implementation, saved replay outputs and test cases; targeted isolated change only on this audit branch. **No authenticated broker connectivity, full pytest execution, live fills or full new historical backtest were available.** Historical findings are not new runtime observations.

## Executive decision
**Not certified for increased capital or unattended live AUTO on profitability evidence.** The repository contains substantial safety, history, broker and signal infrastructure, but distinct strategy families, price bases and P&L lanes remain difficult to compare. The most defensible root causes are: (1) weak net payoff after transaction friction; (2) unstable or immature signal timing relative to an option-premium risk horizon; (3) differing lane admission and exit policies; (4) performance accounting/history limitations that once materially distorted reported outcomes; (5) risk-control configuration that can allow minimum lots/scale-ins above an advisory risk amount.

## Evidence hierarchy and observed results
1. **13 Sep 30-day historical replay** (reports/verification_losing_replay_20260913.md): 96 trades, 22 wins, 74 losses, net **-Rs 60,093.48**, PF 0.206. This was a *pre-repair* simulation with 42/96 temporally inconsistent trades; not a current P&L claim.
2. **14 Sep causal source-corrected replay** (reports/integrated_repair_verification_20260913.md): 102 actual-archive option price paths, gross **+Rs 15,427.66**, modeled costs **Rs 17,544.03**, net **-Rs 2,116.37**. Historical options prices do not prove order-book execution; retrospective repair is not a prospective holdout.
3. **27 Sep matched exit-policy holdout** (reports/exit_policy_walk_forward_upgrade_20260927.json): 151 trades across 20 holdout sessions; candidate **-Rs 46,250.26**, PF 0.4198, expectancy -Rs 306.29/trade. Candidate not certified; simplified replay does not reproduce every broker lifecycle.
4. **S1 workflow study** (reports/s1_lane_workflow_study_20260925.json): 616 modeled trades, 56.2% winning but net **-0.091R/trade**; 1m -0.126R, 5m -0.096R, 15m -0.026R. A winning percentage is not a positive return distribution.
5. **30 Sep accounting incident** (reports/uts_profit_reversal_analysis_20260930.md): displayed **-Rs 112,250.60** vs corrected frozen-cohort **+Rs 14,813.23** including estimated charges. This largely resulted from lost verified exit fill accounting, not a new proven trading edge. Cohort has overlapping, endogenous entries and non-tick bars.
6. **UTS risk/quality forensic** (reports/ut_smart_loss_audit_20260930.md): seven filled rows on 29 Sep, six repaint exits; five rows' normal quality verdict denied entry although UTS native lane intentionally bypassed it. Correctness repairs followed, and must not be counted as current defects without new counterexamples.

## Pros (retain)
- Four-index, multi-timeframe architecture, separate independent engines and instrument-specific exit ownership.
- Actual option contract identity, protective orders, partial-fill/reconciliation controls, exchange clock and provenance checks.
- Historical causal price/expiry repairs, observed-option replay abstention and persisted exit accounting tests.
- Closed-candle vs provisional signal distinction; UTS evidence freshness and post-entry peak validation.
- Risk circuit breakers, prospective stop-risk policy, read-only broker audit tools, execution lease and kill controls.
- A/B runner replay and candidate promotion gates now require *positive* holdout expectancy and PF > 1.

## Key weaknesses, current versus historical
| ID | Severity | Status | Evidence | Correction / verification gate |
| --- | --- | --- | --- | --- |
| E1 Net expectancy | P0 | Historical failing | Causal replay net -2,116 after Rs 17,544 modeled costs; S1 -0.091R | Broker ledger-cost reconciliation; 60-90 chronological sessions and untouched holdout by index/TF/engine |
| E2 Exit-policy edge | P0 | Historical failing | 27 Sep holdout PF 0.4198, -Rs 306/trade | No promotion until positive net after costs, adequate sample and drawdown |
| E3 Different entry gates | P1 | Confirmed intentional | engine/uts_lane.py entry_verdict sets grade_filter_enabled=False; 5m has native volume/ADX/RSI/PCR | A/B native-grade + quality filter on exact event cohort; do not presume filtering always helps |
| E4 Untuned parameter drift | P1 | Confirmed change | config/settings.py UT Smart defaults 1/10/HA off versus prior tuned 2/14/HA on 15m | Freeze strategy parameter hashes; compare separately, never treat Pine parity as profit |
| E5 Intrabar repaint risk | P1 | Intentional with repaired bugs | UTS 15s wait, 10s repaint check; changes during 5/15m forming bars | Event-level tick replay; compare closed versus provisional on same contracts and eligible times |
| E6 Horizon conflict | P1 | Plausible, test required | prior post-entry report: 1m trail and scalp-lock on longer horizons | Paired shadow exit ablations, immutable SCALP/5MIN/15MIN policy at fill |
| E7 Cost model | P1 | Modeling risk | engine/trade_accounting.py: Rs 80 flat option charge + 0.30% per-leg slippage default; can calibrate | Reconcile brokerage, exchange, GST, STT, stamp and actual bid/ask, source/venue/date, split both legs |
| E8 Calibration | P1 | Confirmed disabled default | config/settings.py ut_confidence_calibration_enabled/live_apply_enabled both False | Proper chronological, segmented outcome calibration. A+ never labeled probability otherwise |
| E9 Pre-entry risk control | P0 safety | **Confirmed optional** | config/settings.py; trading/trade_manager.py: `entry_risk_budget_enabled` guarded path defaults disabled. `max_trade_loss_abs=0` disables rupee hard cap | Make cap activation an explicit audited operator decision; pre-entry & scale-in tests (no silent live change) |
| E10 Corrupt quantity risk | P1 safety | **Fixed on this branch** | engine/entry_risk_policy.py coerced absent/zero/fractional lots via max(1,int(x or 1)) | Reject invalid exact quantity; cover damaged open positions and candidate inputs |
| E11 Futures multiplier risk | P1 safety | **Fixed on this branch** | evaluator only read `multiplier`, while Trade persists `instrument_multiplier` | Read both names, reject explicit invalid multiplier; test independent archived trade |
| E12 Historical lookahead and fill issues | P0 integrity | Repaired in principal replay | reports/replay_path_repairs_20260913.md and integrated verification | Recheck deployed source fingerprint and exact replay input/contract hash |
| E13 Live volume/OI coverage | P1 | Historically impaired; current live state unknown | reports/LIVE_AUTO_TRADING_READINESS_2026-07-30.md and volume_lifecycle_check_20260915.md | Validate coverage, source/expiry, exchange-time freshness, measured zero vs unavailable |
| E14 Operational reproducibility | P1 | Missing external proof | No GitHub Actions runs; sources include thousands of historical regression tests | Run whole suite and fresh simulated order lifecycle on Windows 11 with hash evidence |

**Important nuance on E9:** the Rs 2,000 `risk_per_trade_rs` setting is not guaranteed to be a hard loss cap. Equal-risk lot sizing can choose `allow_one` when minimum lot risk exceeds target; position scaling also exists. Per-trade absolute cap defaults disabled. The separate daily circuit breaker is not equivalent to a prospective admission proof.

## Concrete isolated software correction
On branch `audit/2026-11-19-risk-integrity`:
- `engine/entry_risk_policy.py`: finite positive input requirement; exact positive integer lot counts and lot sizes; no conversion of invalid zero/missing/fractional quantities into one-lot exposures; futures open positions accept either `multiplier` or persisted `instrument_multiplier`; invalid slippage rates abstain.
- `tests/test_entry_risk_quantity_integrity_2026_11_19.py`: pure regression cases for the above, including candidate rejection and refusal to admit new trades when open-position risk is unknown.
- **NOT DONE:** no full pytest run, broker validation, deployment, dashboard changes or automatic risk-policy activation. Static checks confirmed files and key guard fragments exist; do not describe these tests as passed until executed.

## Minimum validation matrix before live money
1. Export a *read-only* broker fill/order/contract-note ledger (include executed quantity, signed direction, contract and expiry, separate buy/sell costs, order status and timestamps).
2. Freeze an isolated snapshot of `.env` **without secrets**, strategy hashes and a non-trading SQL copy. Confirm current actual engine, intrabar mode, exit owner, risk gate, source freshness, subscription and leg selection.
3. Produce exact fill-level portfolio equity per day, and per engine × index × TF × direction × regime × entry route × exit reason × intrabar/closed, with and without brokerage, STT, exchange and slippage.
4. Match identical candidate events and contracts for minimal raw strategy / shipped entry gates / one gate ablated; record reject opportunity using causal later path, never treat index MFE as realized option gains.
5. Paired unchanged-entry exit experiments on same observed option path: current, own-timeframe trail, independent runner, own premium-lock, verified catastrophe fallback. Compare path-ordered MFE, MAE and realized P&L.
6. Run separate 60–90 session walk-forward and at least 20 untouched holdout sessions per sufficiently sampled candidate; require >0 net expectancy, PF >1, bounded DD, adequate coverage and confidence intervals; require minimum supervised forward paper sessions afterward.
7. Execute full tests on Windows 11, broker read-only consistency and simulated partial-fill/cancel/reconnect/stop rearm/expiry/holiday/EOD scenarios. A code-passing result does not certify profitability.

## Research and 2026 India costs
Current Zerodha tariffs and NSE/SEBI derivatives levies materially affect short premium trades. Zerodha publishes its brokerage/levies at https://zerodha.com/charges ; NSE publishes STT/stamp information at https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies . SEBI's retail API algo framework has implementation dates/requirements; verify the broker's approved setup instead of assuming live API eligibility from a working login (https://www.sebi.gov.in/legal/circulars/sep-2025/extension-of-timeline-for-implementation-of-sebi-circular-dated-february-04-2025-on-safer-participation-of-retail-investors-in-algorithmic-trading-_96979.html).

## Final position
**No strategy should be represented as profit-fixed by a safety patch or favorable in-sample run.** Existing production risk controls remain as-is, and the isolated branch only corrects deterministic risk-input accounting. Broker statements plus a fresh end-to-end replay are required to establish whether current account losses are from actual entries, exits, fees, mis-accounting or a changed active strategy.
