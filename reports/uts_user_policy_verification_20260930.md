# UT SMART EXIT: revised admission and capital stops

Implemented in source on 30 September 2026. The running trading process was not restarted. No live broker orders were sent or changed during this work. The application must load this code before the new rules apply.

## What changes in everyday terms

The scanner can still use its wide ATR stop to decide when the index setup is invalid. The option position now has a separate, smaller capital stop. A wide chart stop cannot make a new option trade risk 50% of its premium.

For an option bought at ₹100:

| Entry type | Selected maximum premium risk | Initial capital stop |
| --- | ---: | ---: |
| Filtered 1-minute burst, spike or reversal | 5% | ₹95 |
| Cascade or multi-index alignment, any timeframe | 5% | ₹95 |
| Strong, directionally aligned trend, any timeframe | 5% | ₹95 |
| Ordinary 5-minute or 15-minute trade | 10% | ₹90 |
| Choppy/ranging/volatile 15-minute trade | 15% | ₹85 |
| Choppy/ranging/volatile 5-minute trade | Initially 10% | Initially ₹90 |

The lower operator risk ceiling and an already tighter stop still win. Cascade/alignment takes precedence over the 15-minute choppy rule. A plain 1-minute signal still needs the existing special-entry qualification; assigning a 5% stop does not admit it automatically. These percentages describe stop levels, not guaranteed realised losses after costs, gaps or execution delay.

“Strong trend” uses the existing direction-aligned trending/breakout regime plus ADX at least 35 and RSI at least 55 for LONG, or at most 45 for SHORT. Multi-index alignment uses the existing same-direction index vote counter with at least two indices. It does not add a grade requirement.

## The single 5-minute exception

The user explicitly authorised one controlled extension while a fresh setup remains strong. An ordinary 5-minute position may change its stop from ₹90 to ₹85 while its premium is above ₹90 and at or below ₹93. This is limited to a current choppy/ranging/volatile regime.

It requires a matching successful UTS result observed within 30 seconds, a newly closed 5-minute bar, the same UT direction, ADX at least 25, directional RSI support, and measured volume at least 1.2×. REAL intelligence must also be no older than 30 seconds. Tick activity proxies and missing evidence cannot justify this extension.

The projected daily and option loss budgets must fund the additional risk, costs and slippage allowance. The per-trade absolute loss cap and operator ceiling must also permit it. The position must be tracked in the trade book.

The original broker stop remains active while the request is queued. Before modification, the system rechecks price, stop touches, quote verification, risk budget and the broker order's trigger-pending status. The queued request expires after two seconds. Local stop widening is recorded only after the broker modification succeeds. An ambiguous/refused modification enters recovery and consumes the one modification attempt.

There is no extension after a stop touch, after profit protection has raised the stop, for recovered exposure, or for a 5% cascade/alignment trade. The original 10% initial risk remains the reference for profit-lock activation after a successful extension. Extension state persists through SQLite restore.

Broker API acceptance is not a promise of execution. The official [Kite order documentation](https://kite.trade/docs/connect/v3/orders/) explains the distinction between an API response and the final order state; existing reconciliation remains responsible for tracking execution.

## Grade filtering

UTS no longer uses grade/confidence to veto entry, rank native candidates, or choose the capital stop. Operator grade preference is bypassed for UTS. Live admission, delayed manual admission and product-routing rechecks use the UTS native verdict.

The native verdict rejects an extremely weak directional setup only when all three conditions hold together: measured volume below 0.5×, ADX greater than zero but below 12, and RSI lacking directional support (LONG RSI at or below 50; SHORT RSI at or above 50). Missing or proxy volume is not treated as measured low volume. Existing quote, contract, session, candle, capital and execution safety checks remain.

The earlier requirement that an option stop cover the whole delta-mapped index ATR stop has been removed from UTS admission and option replay. A tighter capital stop may exit before chart invalidation. An incomplete actual-option replay path still cannot produce a claimed outcome.

These thresholds implement the requested policy. They have not been shown to guarantee higher trading expectancy. Earlier available research was too small to establish a reliable grade-based edge.

## Small gains and later rebounds

Profit protection still arms at one initial risk unit, rather than on every small rise. With entry ₹100 and initial stop ₹90, it arms at ₹110. A peak of ₹120 raises the floor to ₹114. With a 5% initial stop at ₹95, it arms at ₹105; a peak of ₹108 produces a floor of ₹105.60.

A normal trade that rises only to ₹102/₹105/₹108 and then falls has not armed the 1R lock. Its selected capital stop still limits the intended loss. The rare approved extension can allow a strong 5-minute setup more room, but cannot know whether a later rebound will occur. Once the active stop is touched, the system exits instead of waiting for a rebound to ₹110.

## Verification

**618 tests passed, zero failures, zero skips**, in the current focused and adjacent regression run. Coverage includes UTS candle/repaint repairs, native admission, all stop branches, cascade/alignment priority, actual-fill stop enforcement, option replay, the controlled extension and its refusal cases, SQLite restore, S1 behavior, final entry policy, manual exit authority, trade execution failures, broker protection and projected risk.

Broker behavior was tested with isolated fake brokers and databases. These tests did not trade real capital. No full-repository or live profitability claim is made.

- Machine-readable test result: `reports/uts_user_policy_verification_20260930.xml`
- Regression cases: `tests/test_uts_user_cap_policy_2026_09_30.py`
- Source/verification manifest: `reports/uts_user_policy_verification_20260930.json`

Existing recovered positions are not given a retroactive new stop by the entry boundary. Review their actual broker protection separately when activating the updated application.
