# UTS risk and regime integration audit — 1 October 2026

This audit uses synthetic, ordered contract observations and production admission, fill-cap and exit-monitor code. It verifies policy behavior; it does not estimate future trading profitability.

## Confirmed findings

1. Missing or malformed volume could falsely reject UTS entries. The native setup reader converted unavailable ratios into zero while retaining a measured-volume flag. Historical/recovery cases with `None`, NaN, infinity, negative values or malformed strings then failed the joint-weakness rule when ADX and RSI were also weak. A live intelligence context marked available but missing every ratio field had the same problem. Six regression tests failed before the root agent's fix and pass afterwards. Explicitly measured zero volume remains valid evidence and continues to reject jointly weak setups.
2. The session ledger did not mirror MIDCPNIFTY's approved stop extension. Its condition expected an initial stop at 90% of entry for every index. MIDCPNIFTY starts at 93% for ordinary 5-minute trades. With entry 100, original stop 93, acknowledged broker stop 90 and latest premium 92, the broker trade correctly remains open but the session ledger incorrectly emits `STOP_HIT`. Both supported MIDCPNIFTY spellings reproduce this failure. The three standard-index controls pass. Root agent owns the minimal index-specific ratio correction.

## Policy matrix

The new test module covers 240 combinations: four indices, three timeframes, both underlying directions and ten market/setup cases. Cases are ordinary, normal trend, strong trend, breakout, choppy, ranging, volatile, cascade, multi-index alignment and spike. Directional regimes are tested up and down. One-minute candidates carry a filtered special flag; ordinary one-minute rejection remains covered by existing native admission tests.

Each combination exercises native admission with C display grade, actual fill stop capping and the real exit monitor. It verifies two ordered premium paths: a small gain followed by an initial-stop breach and rebound; and a premium peak followed by a profit-lock breach and rebound between monitor passes. The first observed valid stop wins, and later prices cannot erase the breach or inflate the stored peak after exit.

The matrix confirms standard premium tiers of 5%, 10%, 15% and MIDCPNIFTY tiers of 3.5%, 7%, 10%. It verifies both bought CE and PE behavior, strong-trend and cascade/alignment tight stops, and wider 15-minute choppy/ranging/volatile caps. No stop policy was retuned by this audit.

## Verification

- Initial new matrix plus volume regressions: 247 tests, 241 passed and six reproduced failures. Evidence: `uts_risk_regime_audit_pre_20261001.xml`.
- After root fixes, focused native admission, premium caps, broker extension, repaint retry and new regime matrix: 477 passed, zero failures. Evidence: `uts_risk_regime_audit_final_20261001.xml`.
- Initial MIDCPNIFTY session extension test: five tests, three passed and two reproduced failures. Evidence: `uts_midcap_session_extension_pre_20261001.xml`. The root audit records final verification after its patch.

No live orders, settings, production database or running scanner process were changed by this audit subtask. Production fixes belong to the root audit; this subtask added regression tests and this report.
