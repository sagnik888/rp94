# Volume, PCR and OI through the trade lifecycle — 15 September 2026

**Conclusion: the main data-to-decision paths are connected and respond to changed evidence, but live operation is not currently demonstrated, and PCR does not yet provide independently timed 1/5/15-minute momentum.** This is a read-only verification; no product code, trading mode, settings or broker orders were changed.

## What is verified

| Stage | Actual behavior | Evidence |
|---|---|---|
| Signal detection | UT/ATR generates the base price signal; measured volume can provide spike/thrust confirmation and special entry eligibility. Missing volume and tick activity are not equivalent to traded volume. | Current-volume quality and MTF cache-mutation regression tests. |
| Index volume | Requested timeframe owns its volume analysis. Source, measurement timestamp, timeframe, availability and ratios are retained. Cash constituent participation stays separate from futures and option quantities. | Aggregator tests cover four indices × three timeframes × both derivative families without changing index volume. |
| Futures/options changes | Separate contract/provider histories produce 1/5/15-minute quantity, OI and price changes when the requested interval has sufficient exchange-timed observations. Options require matched CE/PE identities; day totals and sparse receipt-timed snapshots abstain from precise interval claims. | `test_derivative_decisions_20260913.py`, including counter resets, provider/expiry changes, window coverage and matched legs. |
| OI interpretation | Comparable interval changes can influence directional evidence with price confirmation. A day-reported OI change cannot impersonate one-minute OI. Complete measured replacement cohorts carry levels and deltas together. | Full-window 1/5/15-minute OI tests; neutral underlying price prevents manufactured direction. |
| PCR | Broad and near-money ratios update from distinct chain observations. Provider, expiry, strike universe and near-money population separate histories. Repeated reads of the same snapshot do not create a new trend observation. | PCR regression tests and independent changing-ratio probe. |
| Entry filtering | Current volume evidence feeds quality checks. Available volume/flow/OI/PCR affect shared conviction; eligible derivative participation modifies existing conviction once within 0.9–1.1. It does not independently create or reverse trade direction. | Actual aggregator and candidate-quality tests; `scanner.py` current-intelligence adapters. |
| Tracking and trailing | Trade management resolves fresh intelligence for the trade's own timeframe. Live entry snapshots cannot override current market observations. Volume, OI/PCR/flow, regime and price evidence feed threat, reversal, loss and profit-lock policies. | `Scanner._fresh_exact_trade_intel`, `_trade_like_market_intelligence_context`, `_peak_threat_exit_decision`, `_choppy_profit_lock_bypass_peak_threshold`, and loss/reversal helpers. |
| Exit decision | Adverse votes are interpreted relative to long/short exposure. Exits retain price/structure, P&L and confirmation requirements; a volume or PCR change alone need not cause an exit. Repeated polling of unchanged evidence cannot earn another confirmation. | Exit-confirmation and unavailable-evidence regression tests; independent PCR-to-position-vote probe. |

These are controlled code/integration proofs. They do not establish actual broker arrival timing, fill quality, profitable decisions or a full live broker order lifecycle.

## Remaining PCR limitation

`PCREngine.analyze` compares recent groups of distinct snapshot ratios. It does not select a separate elapsed-time 1-, 5- or 15-minute PCR-change window. Its directional signal is interpreted from the current primary PCR; the `trend` label is separate. The traced aggregator and position-vote consumers use the ratio/classification, rather than a standalone PCR trend-change/acceleration signal.

An independent probe changed PCR from 0.6 through 1.5. The engine changed its classification from BEARISH to STRONG_BULLISH and its tracked trend became RISING. Long-adverse votes changed from one to zero; short-adverse votes changed from zero to one. Re-reading the last snapshot left history at six observations. Changing only a supplied trend label from RISING to FALLING left position votes unchanged. This documents current behavior, not a recommendation to add another gate.

OI-change and derivative-participation windows therefore must not be described as proof of equivalent timed PCR momentum. Option participation also covers the subscribed matched contracts, not the entire exchange option universe. Candle-derived buy/sell pressure is an estimate, not proof of actual aggressor-side order flow.

## Running application at 00:58:49 IST

- HISTORICAL mode; Auto off; 30-day selection; quad_prime scanner. Runtime fingerprint matched the installed source with `reload_required=false`.
- Kite: no live session or available historical REST connection.
- FYERS: disconnected; application diagnostics reported manual authentication required.
- Angel: adapter connected/configured, but index volume unavailable and option fallback reported `option_contract_not_found`.
- Displayed index volume came from historical candles dated 11 September, not a current market session. Available historical volume is not fresh live volume.
- Displayed PCR/OI were unavailable and derivative conviction multiplier was neutral. Missing chain data was not treated as bearish evidence. It was outside market hours.

Consequently **the current app is not making live trades from fresh volume/PCR/OI streams**. A market-hours verification with authenticated feeds, actual interval coverage and timestamped decision traces is still needed. The earlier complete replay also lacked historical derivative/chain observations, so its P&L does not validate the live derivative feature contribution.

## Tests and artifacts

**188 passed**, 5.27 seconds; zero failures in the focused selection:

```text
venv/Scripts/python.exe -W ignore -m pytest tests/test_derivative_decisions_20260913.py tests/test_remaining_audit_repairs_2026_09_11.py tests/test_broker_intelligence_audit_fixes_2026_09_11.py tests/test_intelligence_data_truth.py tests/test_signal_trade_management_audit_fixes_2026_09_10.py -q --disable-warnings --tb=short --junitxml=reports/volume_lifecycle_check_20260915.xml
```

Evidence: `volume_lifecycle_check_20260915.xml`, `volume_lifecycle_runtime_20260915.json`, `volume_lifecycle_state_20260915.json`, `volume_lifecycle_probes_20260915.json`. Probe persistence was isolated in a temporary database. No live orders were placed.
