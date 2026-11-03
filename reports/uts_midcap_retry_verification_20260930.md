# UTS premium tiers and repaint re-entry verification

Date: 2026-09-30. Scope: UT SMART EXIT only.

## Premium stop policy

| Index | Tight setups | Regular 5m/15m | Choppy/volatile 15m maximum |
| --- | ---: | ---: | ---: |
| NIFTY | 5% | 10% | 15% |
| BANKNIFTY | 5% | 10% | 15% |
| SENSEX | 5% | 10% | 15% |
| MIDCPNIFTY / MIDCAPNIFTY | 3.5% | 7% | 10% |

Tight setups include filtered 1m entries, cascade/alignment entries and qualifying strong directional trends. Ordinary 1m entries remain prohibited. The previously authorized rare strong 5m exception remains one controlled widening: 10% to 15% for the three standard indices, or 7% to 10% for MIDCPNIFTY. It requires fresh setup evidence, remaining rupee risk budget, an untouched original stop and verified broker protection before and after modification. A tighter operator limit continues to win.

The shared policy now reaches entry verdicts, queued orders, actual option fills, stop extensions and actual-option replay. MIDCPNIFTY verdicts record their profile so a new 10% maximum cannot be mistaken for an older 10% regular tier. Legacy common-tier verdicts map to the corresponding MIDCPNIFTY tiers. Lot-size configuration is unchanged. Existing recovered exposure is not retrospectively repriced by the entry/fill cap.

For a ₹100 MIDCPNIFTY entry, the three planned initial stop thresholds are ₹96.50, ₹93 and ₹90, subject to tighter geometry and broker tick rounding. These thresholds define planned protection, not guaranteed execution prices.

## Repaint and reappearance workflow

1. Before entry, a sampled absent or opposite arrow discards its pending buffer and releases its exact UTS signal identity. Other source identities and the opposite side remain intact. A returning arrow can enter normal scanning again and must complete a fresh buffer.
2. After entry, the independent worker samples every 10 seconds, with a final candle-close check. A detected repaint latches exit intent. A returning arrow does not cancel that intent while the option quote or execution authority is unavailable.
3. Re-entry eligibility is released only after the trade is actually CLOSED with `UTS_REPAINT`. An acknowledged or pending exit is insufficient. Eligibility expires when the source candle closes; a later regular stop prevents an older repaint exit from reopening eligibility after restart.
4. If the arrow returns while forming, normal signal processing produces a fresh candidate. The candidate must pass all admission, capital, quote and timing checks again. Defaults remain 20 seconds for 5m/15m and 6 seconds for 1m, configurable within 5–7 seconds.
5. Repeated filled attempts receive distinct lifecycle and history identities. Duplicate terminal callbacks cannot erase the next attempt's buffer. Closed-candle entries retain their existing confirmation path.

Normal scans, trailing and premium protection continue independently. Manual mode retains notification/confirmation authority. Sampling cannot detect a flicker that disappears and returns entirely between observations; unavailable data does not prove a repaint.

The full regression run found that SQLite did not store the new exit-intent field. Both the persisted-column whitelist and automatic schema migration now include it. A real isolated database save/load test verifies recovery.

## Verification

- Initial focused reproduction: 38 failures and 36 passes.
- Full default suite: 7,670 passes and one failure identifying the SQLite persistence gap; four integration tests excluded by default.
- After fixing that gap: 572 focused UTS, manual-authority and lifecycle tests passed.
- Persistence and database gate: 25 passed, including the previously failing consistency check.
- Four isolated integration checks: four passed; no live broker calls or orders.
- Across the full run and subsequent checks: **7,682 unique tests have a passing latest result; zero unresolved failures**. The full suite was not repeated after the narrow database fix; the affected persistence and UTS gates were rerun.
- Seven changed Python modules parsed successfully. Source hashes and per-run counts are stored in `uts_midcap_retry_verification_20260930.json`.

The full run retains one existing unrelated pytest warning about a test returning a value. Dashboard port 8001 was not listening during verification. Runtime activation is unverified; start or restart the scanner to load these source changes. No live settings, broker quantities or orders were changed during this verification.

## Main code locations

- `engine/uts_lane.py`: index-specific tiers, verdict profile and extension selection.
- `trading/trade_manager.py`: actual-fill caps, verified broker extension and serialized exit intent.
- `data/sqlite_db.py`: persisted exit intent and automatic column migration.
- `engine/execution_service.py`: exact source identity release on buffer discard.
- `scanner.py`: terminal-only eligibility release and distinct retry lifecycle/history.
- `engine/uts_repaint.py`: independent samples, latched exit intent and manual notification loop.
- `engine/signal_processor.py`: instrument identity for actual-option replay.
- `tests/test_uts_midcap_repaint_retry_2026_09_30.py`: focused regression proof.
