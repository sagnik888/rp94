# S1 system integration verification — 24 September 2026

## Result

**479 tests passed, zero failures or skips**, plus saved-candle replay checks across NIFTY, BANKNIFTY, MIDCPNIFTY and SENSEX at 1, 5 and 15 minutes. One FYERS `pkg_resources` deprecation warning remains.

The reported NIFTY 15-minute mismatch on **23 September** remains reproduced and repaired: standard candles produce BUY at **12:00**; the selected Heikin Ashi source produces BUY at **10:45**. The real application chart payload contains the 10:45 BUY marker. All replay inputs are truncated to candles closing by **15:15**; post-cutoff prices are not the explanation for this mismatch.

This verifies the tested code and saved data. It does not establish universal, bar-for-bar TradingView parity or certify the entire system free of bugs.

## Additional defects repaired in this audit

| Area | Defect | Repair and proof |
| --- | --- | --- |
| Exit timestamps | Timezone-aware clocks could fail comparison; removing timezone information could mismatch UTC and IST signals | Normalize signal, trade, exit and clock to exchange time; tests cover naive, IST and UTC clocks and the exact bar-close boundary |
| Exit confirmation | An exit reason without a valid exit timestamp could be accepted | Reject missing, invalid and NaT exit times |
| Restored stop geometry | Missing stored stop fell back to 0.25%, even with fixed 0.20% selected | Fixed mode uses configured SL1; dynamic modes retain their cap fallback |
| Restart recovery | S1 best spot, T1 lock and favourable contract price were transient attributes | Added Trade serialization and three additive SQLite columns; actual save/load and legacy-schema tests pass |
| Persistence scheduling | S1 bookkeeping changes did not explicitly schedule a state save | Mark the trade manager dirty when those values change |
| Short futures | Profit lock treated rising futures prices as profits on short positions | Favourable extreme, risk and giveback are direction-aware for futures; bought puts retain long-premium arithmetic |
| Engine Lab inclusion | S1 was absent from the comparison engine list | Added S1 to the common mode list, including the historical sweep |
| Engine Lab dependencies | S1 requires native 5m/15m frames and settings-sensitive caching | Supply native frames; hash full input histories and S1 parameters; invalidate actual-replay cache on S1 settings changes |

The fresh-process chart replay initialized the application's database and added the three missing S1 columns. No orders were submitted and the running scanner was not restarted. Legacy rows are preserved; previously unrecorded index extremes/T1 lock history cannot be reconstructed by adding columns alone.

## Coverage

| Path | Evidence |
| --- | --- |
| Core math, scoring, patterns, signal state, stops, targets, trailing, momentum and circuit breaker | S1 engine and parity regression suites |
| Selected candle source and native requested timeframes | HA conversion, dependency, raw-price and cache tests; 12 saved-data frame comparisons |
| Settings validation, propagation and recalculation | S1 lane, chart and pluggable-engine tests |
| Chart markers, five overlays, exit refresh and source switching | Actual chart-builder replay and JavaScript execution under Node |
| Admission and live/manual authority | Workflow admission, buffer, final-policy, broker-authority and manual/auto tests |
| Cutoff and end-of-day behavior | Closed-candle cutoff and F&O close tests; replay truncated at 15:15 |
| Trade state, SQLite and settings persistence | Round-trip, legacy-schema, persistence and cleanup suites |
| Engine Lab | Unit tests plus all 12 native-frame S1 signal streams matching scanner replay, zero Lab errors |

Loaded reference settings: score **60**, bull/bear gap **20**, ADX **20**, 15m EMA alignment enabled, cooldown **0**, fixed SL1 **0.20%**, targets **2/3/4 R**, trailing enabled with **1.8 ATR**, T1 lock enabled, momentum confirmation **3**, loss streak **3** for all four indices and pause **5**. Existing trailing start **1.5 R** remains. Forecast visuals/filter remain removed.

## Boundaries

- The 10:45 marker names the candle's opening time; its confirmed signal becomes known at 11:00. Synthetic HA opening prices are not executable fills.
- The engine retains Pine chart-state rules. Actual execution uses market prices, entry authority, capital/risk checks, session limits and the configured premium guard/profit lock. Executed trades can therefore differ from indicator-only trades.
- Engine Lab's raw shadow scores use the shared Lab stop/target comparison policy; they are not Pine strategy returns. Historical actual rows use the system's execution replay.
- Trailing and T1 lock are enabled as requested. Individual switches for these and every Pine pattern group are not all exposed in the dashboard. Decorative Pine tables, zones and ribbons are not fully reproduced.
- Exact parity across all historical TradingView bars remains unproven without identical candle history and settings. Stable historical lower-timeframe mapping can differ from TradingView's forming-bar/repainting behavior.
- Verification ran in fresh local processes. **Restart the scanner and refresh the dashboard to load the code; select S1 / Heikin Ashi.** This audit did not validate a reloaded production process against a live broker.

## Artifacts and commands

- `s1_system_audit_20260924.xml`: final 479-test run, 37.81 seconds.
- `s1_candle_source_verification_20260924.py` / `.json`: reproducible 12-frame replay, reference settings, source/data hashes and actual chart marker.
- `s1_engine_lab_verification_20260924.json`: 12 Lab/scanner equality checks and snapshot.
- `tests/test_s1_system_integration_2026_09_24.py`: new regression tests for this audit.

Replay command:

```powershell
.\venv\Scripts\python.exe reports/s1_candle_source_verification_20260924.py
```

Final test command:

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_s1_engine_2026_09_23.py tests/test_s1_parity_fixes_2026_09_23.py tests/test_s1_candle_source_2026_09_24.py tests/test_s1_chart_paths_2026_09_23.py tests/test_s1_lane_2026_09_23.py tests/test_s1_system_integration_2026_09_24.py tests/test_engine_lab.py tests/test_scanner_engine_pluggable.py tests/test_workflow_admission_evidence_2026_09_23.py tests/test_workflow_exit_authority_2026_09_23.py tests/test_workflow_buffer_consistency_2026_09_23.py tests/test_manual_mode_exit.py tests/test_broker_mutation_authority_2026_08_14.py tests/test_final_entry_policy_2026_08_27.py tests/test_trade_row_adapter.py tests/test_closed_candle_mode_honours_the_entry_cutoff_2026_08_10.py tests/test_force_exit_stops_at_the_fno_close_2026_09_11.py tests/test_dashboard_auto_single_authority_2026_09_03.py tests/test_auto_off_alert_repeats_2026_09_23.py tests/test_trade_state_persistence.py tests/test_trade_persistence_roundtrip.py tests/test_settings_persistence.py tests/test_sqlite_cleanup.py -q --tb=short --junitxml=reports/s1_system_audit_20260924.xml
```

`git diff --check` passed for the touched tracked implementation files (only CRLF conversion notices).
