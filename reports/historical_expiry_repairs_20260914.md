# Historical expiry and stop repair â€” 14 September 2026

The final runtime comparison exposed a material discrepancy after the earlier entry-clock fixes: the application had no current authenticated Kite master, so its shared expiry manager returned default DTE 3 and obsolete weekday assumptions. The offline harness initialized that manager from a saved master. Both resolved the same archived option prices, but different DTE values could change the estimated option delta and ATR-based stop distance. Earlier expired weekly contracts were also absent from the newer master. Previous corrected replay results are therefore provisional.

## Implemented

- A separate local archive resolver reads explicit option expiry metadata and unambiguous dated weekly archive symbols. Monthly names require actual expiry metadata. It does not load stale contracts into the live execution manager, make broker requests, or invent a weekday when evidence is missing.
- Historical DTE, expiry-day and gamma checks use the replay's IST decision clock. The existing rollover/gamma policy remains unchanged. REAL helpers still delegate to the live manager, including after a mode change.
- An observed option's exact verified contract expiry supplies its stop-mapping DTE. Its actual first eligible one-minute open and the same mapped/capped stop now feed position sizing before simulation. This removes the previous provisional premium/expiry from observed trade risk sizing.
- Historical chain IV inversion and Greeks use the recorded chain's own expiry. Missing expiry stays unavailable, while independent OI/PCR and VIX remain usable. Availability is attached to historical intelligence diagnostics.
- Each observed trade audit includes exact expiry, DTE, estimated delta, the existing 15% IV assumption, underlying stop distance, mapped option stop distance, sizing entry/stop, lots and risk per lot. Observed prices do not imply observed historical Greeks.

The new expiry resolver caches parsed local identity data and refreshes it when its source signature changes. It does not cache intraday gamma decisions across decision clocks.

## Evidence

The focused expiry, observed-stop, causal replay and historical-context selection passed **90 tests**. It covers all four indices, missing data, older weekly archives absent from a later master, monthly metadata, rollover, gamma cutoffs in IST/UTC, empty/populated live-manager isolation, actual contract stop mapping, audit propagation and absent-expiry Greeks.

```text
venv/Scripts/python.exe -W ignore -m pytest tests/test_historical_expiry_context_20260914.py tests/test_observed_option_expiry_stop_20260914.py tests/test_replay_causality_20260913.py tests/test_historical_context_causality_20260913.py -q --disable-warnings --tb=short --junitxml=reports/expiry_focused_20260914.xml
```

The completed source was frozen in `replay_expiry_final_source_20260914`. Scanner SHA256: `886a2cfde3e160e142e5c3d7ec28c4ecfd131fad6e3a3b6f5ede9ed5347ea43c`. Signal processor SHA256: `2a5bb5ce08aa17d293d9c4ce77572ab71159cfd9bff5591bb74879c80d96030a`.

The clean full suite passed **6,256 tests**, with zero failures, errors or skips, in 288.66 seconds (`all_expiry_verified_20260914.xml`). A preceding full run exposed two synthetic-mark fixtures missing the new expiry adapter; those fixtures were corrected and the clean full suite rerun. No production rule or assertion was weakened. The frozen-input replay is recorded separately in `offline_expiry_final_20260914.json` when complete. Final performance must be reconciled with `analyze_paired_engine_replay.py --require-expiry-evidence`; the command checks every observed trade's price path, contract expiry, stop mapping and risk-sizing evidence. No parameter was tuned to improve this sample's P&L.
