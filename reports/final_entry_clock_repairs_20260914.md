# Final historical entry-clock verification — 14 September 2026

The last review found that moving replay decisions to candle-close availability was insufficient while several entry helpers still selected candles and cached signals by their opening timestamps. The repaired historical helpers use completed candles and source-signal availability consistently. REAL mode retains its existing intrabar semantics.

## Implemented boundaries

- Thrust, power-move, VWAP-break, structure-break, directional-reclaim and short-span market-shock evidence exclude unfinished historical candles. UTC and IST cutoffs agree. An invalid cutoff cannot silently retain future rows.
- Cached relief and cross-index signal votes become eligible only after their source timeframe closes. Already converted replay signals are not shifted a second time. The micro-relief caller now passes the same availability clock as its selected signal.
- An explicitly empty depth context stays empty. Historical spike analysis never fetches live depth, and missing historical shock prices never fall back to today's last-traded price.
- Historical market-location gating has an existing explicit bypass. This work does not claim its later candle-selection branch is exercised by historical trades or that the bypass has been removed.

## Verification

The frozen product source used for the full suite and final replay contains 101 Python files. Independent comparison found zero product-source differences. `scanner.py` SHA256: `c61da6a58176b4b83c199757a178e47990c252ee91588e92046f66cb4eb82ddb`.

- Full suite, including the four marked integration cases: **6,214 passed**, zero failures, errors or skips. Log elapsed time: 263.22 seconds. Evidence: `all_final_20260914.xml`.
- Final entry-causality file: **44 passed**, 2.67 seconds. One cross-index test was added after the full suite collected tests; this separate run covers it. The other 43 overlap the full suite, so these counts must not be added together. Evidence: `final_entry_causality_20260914.xml`.

```text
venv/Scripts/python.exe -W ignore -m pytest tests -o addopts=--import-mode=importlib -q --disable-warnings --tb=short --junitxml=reports/all_final_20260914.xml
venv/Scripts/python.exe -W ignore -m pytest tests/test_historical_entry_gates_causality_20260914.py -q --disable-warnings --tb=short --junitxml=reports/final_entry_causality_20260914.xml
```

An independent frozen-data probe found 35 raw one-minute signals that satisfy the burst rule with their actual ADX and no intelligence boost: NIFTY 12, BANKNIFTY 6, SENSEX 4 and MIDCPNIFTY 13. This establishes that the repaired burst branch is reachable; it is not a count of executed trades or proof of profitability. Full grading, other gates and exact-contract history checks still apply. Evidence: `one_min_burst_evidence_20260914.json`.

The final runtime was restarted in HISTORICAL mode with Auto off and the 30-day selection restored. Its fingerprint matches the tested source, with `reload_required=false` and zero open trades at verification. Evidence: `runtime_verified_build_20260914.json`. Completed performance results belong to the separately reconciled final replay report; a running dashboard recalculation is not a completed performance test.
