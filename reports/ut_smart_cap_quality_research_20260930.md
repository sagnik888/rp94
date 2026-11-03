# UT SMART EXIT: premium protection and grading research

Research date: 30 September 2026. This investigation reads production source, completed app trades, archived gross P&L samples, option-history coverage and calibration outcomes. It probes production helpers using detached objects with network disabled. It changes no production source, live setting or order. Twelve production source hashes remain unchanged.

The previous repairs address reproducible execution defects, but two added rules are unvalidated strategy changes: mandatory generic quality admission and requiring the capped premium stop to cover the delta-mapped UT stop. Neither rule has demonstrated better UTS returns. A 1R premium profit lock also leaves gains below its arming threshold unprotected against giveback.

## 1. What happens to the user's price examples

The ATR chart stop and the option premium stop are separate controls. The chart stop describes when the underlying UT idea fails. The premium stop describes the maximum intended loss on the bought option. A tighter capital stop is allowed to exit before the chart idea fails.

The current source caps option stops at the tighter of the operator limit and the shared policy:

| Entry condition | Maximum initial premium loss | Stop for a ₹100 entry | 1R profit-lock arm |
|---|---:|---:|---:|
| Ordinary/strong trending setup | 10% | ₹90 or tighter | ₹110 if risk is exactly ₹10 |
| Ranging/volatile setup | 15% | ₹85 or tighter | ₹115 if risk is exactly ₹15 |
| Weak confidence, C-grade, or fast burst | 5% | ₹95 or tighter | ₹105 if risk is exactly ₹5 |

These are ceilings, not mandatory distances. A tighter natural stop or lower operator limit reduces both allowed risk and the 1R arming distance. Grade currently participates in both entry admission and stop selection; changing grade admission therefore requires keeping stop selection explicit.

Production helper probes start with an ATR-derived ₹50 stop on a ₹100 option. The trade manager raises that stop to ₹90, ₹85 or ₹95 for the corresponding cases. The chart's ₹50 level does not override the tighter premium stop. This proves updated source behavior; it does not prove a running process loaded that source or that the exchange filled its stop.

For the user's ₹100 entry with a ₹90 initial premium stop:

| Observed path | Current premium-lock behavior | Remaining protection |
|---|---|---|
| ₹100 → ₹102 → down | No lock; gain is below 1R | Original ₹90 stop, chart exits and EOD |
| ₹100 → ₹105 → down | No lock; gain is below 1R | Original ₹90 stop, chart exits and EOD |
| ₹100 → ₹108 → down | No lock; gain is below 1R | Original ₹90 stop, chart exits and EOD |
| ₹100 → ₹110 → down | Lock arms; floor becomes ₹107 | Stop can ratchet up to ₹107 |
| ₹100 → ₹120 → down | Peak gain ₹20; retain 70% | Stop can ratchet up to ₹114 |
| ₹100 → ₹90 → ₹110 | Stop condition occurs at ₹90 before the recovery | Recovery requires a separate new entry |
| ₹100 → ₹80/₹70 → ₹110 | The intended ₹90 stop has already been crossed | Holding through that move violates the 10% loss budget |
| ₹100 → ₹70 in one gap | Stop condition is crossed without trading through every price | Exit can occur below ₹90; a stop is not a guaranteed fill price |

With a 5% initial risk, ₹105 arms the same 1R lock, and a ₹108 peak produces a ₹105.60 floor. With 15% initial risk, none of ₹102/₹105/₹108 arms it. Lowering the R threshold alone is therefore not a complete solution across different stop distances.

UTS deliberately returns from its own exit branch before the generic early-loss, breakeven and profit-protection stack. Its trade update explicitly passes `early_loss_cut_decision=False`. Adding the 1R lock did not activate those other guards. [UTS lock](C:/Users/sagnik/Desktop/rp94/rp46-3/engine/uts_lane.py:157), [exit branch](C:/Users/sagnik/Desktop/rp94/rp46-3/engine/exit_cycle.py:222), [option stop cap](C:/Users/sagnik/Desktop/rp94/rp46-3/trading/trade_manager.py:6430).

Broker protection has a separate execution requirement. The exchange must accept protection for the actual quantity; an order identifier alone does not establish execution. Stop-limit orders can remain open when a fast move passes their limit price. Gaps and slippage can exceed the intended loss percentage. [Kite order status](https://kite.trade/docs/connect/v3/orders/), [Zerodha stop execution](https://support.zerodha.com/category/trading-and-markets/charts-and-orders/order/articles/what-are-stop-loss-orders-and-how-to-use-them).

## 2. Why the bounce cannot be guaranteed

Two paths can be identical until the stop: ₹100 → ₹95 → ₹90. One can then rise to ₹110; another can continue to ₹50. At ₹90, the system has the same observed history in both cases. It cannot retain only the future winner with certainty.

The capital-budget choice is to realize the capped loss, then evaluate a fresh recovery setup. That preserves a per-attempt loss budget but can create repeated small losses and extra costs. It needs a limit on attempts and a session loss limit. Increasing position size after a loss or widening a stop would defeat this protection.

An existing stop-out re-entry helper can bypass the post-exit cooldown for an eligible new candidate. Its source default is off. Its defaults allow one extra attempt per index, within 30 minutes, with an aligned trend and measured ADX at least 25. It does not automatically create a new UT signal when the option bounces, and this investigation does not enable it. Its generic trend requirement also needs UTS-specific evaluation for reversals and ranging markets. [Existing re-entry helper](C:/Users/sagnik/Desktop/rp94/rp46-3/scanner.py:3215).

## 3. Grading is not a probability guarantee

Grade is a display bucket of confidence: A+ ≥85%, A ≥70%, B+ ≥55%, B ≥40%, otherwise C. The confidence score combines weighted ADX, multiple-timeframe agreement, intelligence, regime and candle-wick adjustments, then applies a logistic transformation. A logistic transformation bounds a score; it does not establish a 90% chance of winning. [Grade bands](C:/Users/sagnik/Desktop/rp94/rp46-3/engine/signal_quality_policy.py:27), [score construction](C:/Users/sagnik/Desktop/rp94/rp46-3/engine/signal_manager.py:219).

Recorded UTS verdicts explicitly label confidence as `heuristic_score`. Live calibration is off in those entry snapshots. The outcome CSV contains only 29 records; the largest index/timeframe/regime cell contains three. The calibrator's source defaults require 200 global outcomes and 50 per exact segment. Even meeting those thresholds would not establish certainty. The CSV also lacks scanner/strategy identity, so an eventual pooled curve would not separately establish UTS calibration. [Calibration](C:/Users/sagnik/Desktop/rp94/rp46-3/intelligence/confidence_calibration.py:154).

The generic quality rule is broader than the user's intended weak-setup check in chop. Outside its explicit exceptions, it applies the B+ floor in trending markets too. In range/volatility it also requires ADX, 15m agreement and another confirmation. UTS has one source engine; it cannot supply the multiple-source confirmation leg merely because its own UT flip is valid. It can supply other legs, but those must be measured and present.

Detached probes confirm a 30% score is rejected even in TRENDING_UP with ADX 30 and 15m agreement. An 80% score with those same inputs passes in TRENDING_UP but fails in RANGING/VOLATILE if no second confirmation is present. This is policy behavior, not a measured predictive advantage. [Quality rule](C:/Users/sagnik/Desktop/rp94/rp46-3/engine/quality_verdict.py:209).

A lagging trend label can penalize the first valid reversal: the regime pillar subtracts 0.25 for a counter-trend signal. The quality rule contains stale-regime and full-core reversal exceptions, but those exceptions do not prove general UT suitability. The archived terminal fills have empty grading-pillar breakdowns, so the specific cause of every historical low score cannot be reconstructed.

### Recorded UTS outcomes

Ten completed app-recorded UTS fills are available across 28–30 September. Ghost and session mirrors are excluded. Archived terminal fills and current completed real trade rows are deduplicated.

| Recorded quality verdict | Filled trades | Winners | Recorded net P&L |
|---|---:|---:|---:|
| Allowed | 3 | 1 | −₹1,510.00 |
| Rejected | 7 | 1 | −₹4,972.50 |

On 29 September alone, the five rejected fills total −₹5,392.50 and the two allowed fills total −₹110. This arithmetic supports investigating the filter; it does not establish a causal benefit. Six of that day's seven exits used repaint logic that had reproducible defects, and every fill entered during a forming candle. A score can correlate with a loss caused by another mechanism.

The direct counterexample is 30 September MIDCPNIFTY: C-grade, about 30% confidence, `allowed=False`, recorded net **+₹448**. The same day's A+ NIFTY fill was allowed and recorded **−₹1,400**. Neither quality rejection nor A+ predicts an individual outcome with certainty.

This sample cannot support a claim that rejected UTS trades will usually perform worse under corrected exits. Ten fills span only three dates and multiple correlated indices. Filled trades alone also omit the outcomes of setups that were never executed. These are app accounting figures; broker contract notes were not reconciled.

## 4. Reconsider the stop-coverage rejection rule

The previous patch rejects fresh UTS option exposure when capped premium risk is below the underlying UT stop distance multiplied by absolute delta. For the NIFTY example, that compares ₹20.25 of allowed premium risk with approximately ₹55.44 of delta-mapped chart risk.

That check expresses a strategy preference: do not enter unless the option can tolerate the approximate chart invalidation distance. It is not a necessary consequence of preserving a 10% capital cap. A trader can deliberately accept that an option stop will exit before chart invalidation and evaluate a later re-entry.

Delta is a local price sensitivity, not a fixed conversion over an entire stop distance. Gamma changes delta; implied volatility and time also affect the premium. Consequently, the ₹55.44 estimate is not a guaranteed required loss budget. [NSE option Greeks, section 6.4](https://archives.nseindia.com/content/ncfm/SMRepository/DERIVATIVES_EN.pdf).

The existing observed-price replay admitted zero trades. Its 42 combined diagnostics mix incompatible stops and incomplete paths, so they cannot establish how many trades the coverage rule alone removed. Rejecting that known losing fill is not sufficient evidence that rejecting all such geometries improves UTS. The coverage rule should be evaluated independently as a candidate admission policy; capital caps must remain enforced whichever policy wins.

## 5. What would address small gains and early failures

The next UTS design to evaluate has four separate decisions:

1. **Capital protection from entry:** keep the explicit premium cap and verified protection for actual broker quantity. Account for gaps, stop-limit non-execution and reconciliation.
2. **Small-gain protection:** evaluate a premium lock that can arm below 1R when retained gain exceeds estimated fees, spread and slippage. Observe executable contract prices. Do not rely on a 15m bar or a wide index ATR arm.
3. **Early failure:** evaluate fresh UT invalidation/reclaim evidence, failure to advance and adverse movement over elapsed time. Distinguish missing data from price failure. A generic grade or a brief red tick alone is not enough evidence of a fake move.
4. **Recovery:** keep the first loss capped. Evaluate a fresh, confirmed re-entry with a new risk budget and bounded attempts, rather than waiting below the stop for a bounce.

The early lock requires a cost floor. For illustration only, retaining 70% after a ₹100 → ₹102 rise gives ₹101.40. With 65 units, that is ₹91 gross. Against the app's ₹80 round-trip cost estimate, only ₹11 remains before additional slippage. Automatically locking every 2% gain can therefore add churn with negligible net benefit. This is arithmetic, not a proposed production threshold.

For UTS entry research, compare its native confirmed-flip policy plus execution/data safeguards with generic grading as a shadow diagnostic. Then test individual UT-specific false-breakout checks, especially in chop. Generic grading should not be assumed to be a binding predictive veto without UTS-specific evidence. Preserve explicit operator preferences separately from claims about expected returns.

Do not promote any of these candidates on the ten fills. Compare a small, predeclared set of variants on the same point-in-time opportunities, then on later untouched sessions. Evaluate net expectancy, drawdown, tail losses, cost-adjusted gain capture, profitable signals removed and stopped trades that later recover. Split results by index, 5m/15m, regime and expiry conditions; account for correlated trades by session. Repeatedly tuning thresholds on the same data can manufacture a profitable-looking result. [Backtest overfitting research](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).

## 6. Evidence available and missing

The research ran 15 premium-lock cases, five ATR-stop cap cases and nine quality-rule cases through production helpers. Source fingerprints stayed unchanged. Six archived fills have retained gross P&L paths, totaling 630 samples; these are thinned diagnostics and end at actual exit. They cannot prove executable fills or subsequent recovery.

Only one of ten fills has its exact contract CSV in the inspected candle stores; none of the eight unique traded contracts appears in the option-history bundle. No continuous exact-contract path was found for the other nine fills. Historical rejected trades also lack complete alternate entry/fill paths. This blocks a reliable comparison of early locks, recovery and generic grading on actual option execution.

Recorded peaks have an additional limitation: MIDCPNIFTY's final gross gain exceeds its stored peak. Terminal fills can occur after the last sampled update. Treat those fields as recorded observations, not complete MFE proof.

The earlier full test suite establishes implementation consistency. It does not establish profitable selection, calibrated grades, or a stop/re-entry policy that retains future winners. This research deliberately makes no production changes.

Evidence artifacts: [reproducible collector](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_cap_quality_research_20260930.py), [measurements and helper probes](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_cap_quality_research_20260930.json), [prior observed-option replay](C:/Users/sagnik/Desktop/rp94/rp46-3/reports/ut_smart_fixed_replay_20260930_verified.json).
