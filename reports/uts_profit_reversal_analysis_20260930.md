# UT SMART EXIT backtest profit reversal audit

Audit started 30 September 2026 and completed after midnight on 1 October, Asia/Kolkata. Reference baseline supplied by the user: the earlier 63-trade, +₹93,531 run.

## Main finding

The current displayed loss is dominated by a dashboard accounting bug. The historical price-path helper already filled previously raised premium stops at the active stop, but `reconcile_legacy_trade_row` replaced every `SL HIT` fill with the row's stored stop. Historical rows stored their initial stop there. Consequently, profitable trailing-stop exits became displayed initial-stop losses. The same adapter also replaced observed gap fills and recomputed P&L with flat costs instead of the producer's recorded charges.

This is separate from the newly found replay bug that ignored option candle highs when updating UTS premium profit protection.

## Controlled comparison

The running application was in HISTORICAL mode, with Auto disabled, and had completed a seven-session replay covering 22–30 September. Its displayed result was **257 trades, −₹112,250.60**. The captured rows were recovered from the read-only dashboard API. No application state, order, production database, or settings were changed to obtain this evidence.

All **257 input hashes matched** local observed option price paths across **124 exact contracts**. For 153 paths, the original `provider_time` and `receipt_at` timestamp dtypes were restored to reproduce pandas hashing. OHLC prices were not changed. The original displayed P&L was reproduced for every trade before including any trade in the comparison.

| Same frozen 257 opportunities | Net P&L |
|---|---:|
| Original adapter: stop fills replaced by initial stop; flat ₹80 costs | −₹112,250.60 |
| Preserve verified fills; original close-only premium lock; same flat costs and display rounding | +₹16,027.65 |
| Preserve verified fills and observe candle highs; same flat costs and display rounding | +₹16,724.90 |
| Corrected raw fills, recorded production cost model, rounding net P&L only | **+₹14,813.23** |

Preserving verified fills accounts for **₹128,278.25** of the improvement on the controlled flat-cost comparison. Observing highs contributes **₹697.25** net. Earlier profit protection can reduce some eventual winners; the high correction is not a rule that every trade improves.

The final cost model uses the frozen observed production assumptions: ₹80 flat option cost plus 0.02% per leg on option premium × quantity. The model reproduced all 257 original recorded charges. Calibration was frozen in the audit process; no live calibration state was changed. The production adapter preserves the recorded P&L and charges instead of substituting a new model.

This is a comparison of the same opportunities. It does not generate new signals, model capital-constrained admission, or include trades added by the repaired final-signal branch. A new full application backtest can therefore have a different count and total.

### Concrete accounting example

The 30 September MIDCPNIFTY 5-minute PE trade entered at ₹220.90. Its initial stop was ₹205.437. Observed premiums subsequently raised the profit stop to approximately ₹240.43. The next minute crossed that raised stop.

The helper used the raised stop, and its recorded ₹91.07 charges also reflected the genuine higher exit. The dashboard instead displayed ₹205.44 and −₹1,935.20. Preserving the genuine exit gives approximately **+₹2,252.53 after recorded charges** for the original close-only path.

## Repairs

1. Preserve verified historical option fill prices, recorded P&L, and recorded charges through legacy row reconciliation. Keep the existing repair behavior for unverified legacy rows and broker-fill rows.
2. Preserve two decimal places for replay P&L in trade serialization and dashboard row serialization. Restore stored charges when deserializing a trade.
3. Use observed option highs to arm or tighten UTS premium locks, after checking the prior resting stop. A newly raised lock cannot assume whether the minute low occurred before or after the high. A subsequent close crossing is filled at that observed close; otherwise the new lock applies to the next minute. Gaps through a prior stop remain filled at the observed open.
4. Restore the final unpaired historical UTS signal. The old branch required both HISTORICAL mode and `is_live`, making it unreachable. The repair is restricted to the UTS historical branch and retains native admission, direction, entry cutoff, actual option history, and sizing checks. Ordinary C-grade 5m/15m signals are not vetoed by generic grading.
5. Store the observed historical option fill time separately from the original chart signal time. A 09:15 15m marker can have a confirmed entry at 09:30; reporting must not claim entry at 09:15.
6. Make nominal option target/RR metadata consistent with the UTS 3R policy. Explicit FUT retains its existing index-proxy replay and dynamic target model; its RR metadata follows that model. These reporting corrections do not alter exit pricing or tuning.

The user's stop profiles remain unchanged: NIFTY/BANKNIFTY/SENSEX 5%/10%/15%, MIDCPNIFTY 3.5%/7%/10%, including the previously authorized cascade/alignment and controlled widening rules.

## Why the result still differs from +₹93,531

The earlier result was documented as 15 sessions ending 25 September, 63 trades, one lot, actual archived option prices, and the tuned UTS settings. The current run covers seven sessions ending 30 September and uses the uploaded Pine defaults. The archived original JSON and a complete frozen input/config manifest were unavailable, so exact attribution of the entire difference to the earlier result is not established.

On exactly the same current cached index candles, the earlier tuned profile produces **66 raw arrows**, versus **323** for the uploaded Pine defaults. The earlier 5m UT sensitivity/ATR/Heikin Ashi settings were 4.5/21/on; earlier 15m settings were 2/14/on. Current defaults are 1/10/off. Smart Exit arm/retrace/hard-retrace/confirmations changed from 2.5/0.70/2.5/4 to 0.80/0.45/1.10/2. These are strategy changes requested for Pine parity, not merely repairs to implementation bugs.

No tuning, generic grade veto, or restoration of the earlier profile was applied to manufacture profit. The corrected frozen cohort is positive overall, but NIFTY, BANKNIFTY, and SENSEX remain individually negative; MIDCPNIFTY supplies the aggregate gain. Per-index and per-timeframe results are in the companion cohort JSON.

| Index, same frozen opportunities | Corrected net with modeled charges |
|---|---:|
| NIFTY | −₹9,436.73 |
| BANKNIFTY | −₹4,025.66 |
| SENSEX | −₹535.32 |
| MIDCPNIFTY | +₹28,810.94 |

## Limits and activation

The historical replay uses confirmed candle eligibility and observed minute option candles. It cannot reconstruct the live 20-second forming-signal buffer, repeated appearances/disappearances, or post-entry ten-second repaint checks without recorded tick events. Turning the live intrabar toggle on does not supply that missing historical evidence.

Source repairs need a system restart and a new Backtest run before the existing application can show the corrected result. The currently captured process imported the earlier code. Automatic restart was previously rejected by approval review with the reason `blocked by policy`; this audit did not retry process termination.

The amounts above are modeled historical replay outcomes, not broker-executed gains or a guarantee of future profitability.

## Verification

The integration gate passed **814 tests**, with no failures, errors, or skips. The final adjustment restricted option 3R metadata to options and kept FUT metadata aligned with its existing dynamic target model; its nearest **44-test follow-up gate also passed**. These test counts overlap and must not be added together.

The production source hashes remained unchanged during the integration run. The observed option exit helper is byte-for-byte unchanged by the subsequent entry-time, final-signal, and final FUT metadata corrections. Ten changed or relevant Python files passed syntax parsing. Independent review checked stop ordering, native admission, date boundaries, sizing, observed prices, source/entry timestamps, and repeated-scan deduplication.

The offline cohort reproduction was rerun successfully: all 257 input hashes, original displayed results, and original recorded charges match; no contracts or price paths are unavailable. `uts_profit_reversal_verification_20260930.json` records the final source hashes, test XML references, frozen result, and activation limitation.

## Evidence

- `uts_profit_reversal_runtime_20260930.json`: selected runtime configuration and captured state.
- `uts_profit_reversal_trades_20260930.json`: all 257 captured closed rows.
- `uts_profit_lock_cohort_20260930.json`: exact baseline reproduction, separated correction effects, costs, contract/input hashes, and per-trade outcomes.
- `uts_profit_lock_cohort_20260930.py`: reusable offline reproduction with a temporary isolated database, blocked network, and frozen cost assumptions.
- `uts_profit_reversal_parameter_comparison_20260930.json`: old versus current raw arrow counts on identical candles.
- `ut_smart_exit_replay_20260926.py`: documented earlier +₹93,531 / 63-trade run and its offline production replay workflow.
- Focused regression and integration results are recorded in the final verification JSON/XML beside this report.
