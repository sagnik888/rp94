# NIFTY 15-minute chart parity: 30 September 2026

Read-only audit used the running dashboard at localhost:8500, PID31600, REAL mode. Its raw chart candles were standard OHLC candles. Credentials were kept only in memory. No broker, mode, cache-reset or settings mutations were made by this audit.

## Reproduced cause

On exactly the same137 raw candles, the old tuned settings produce BUY10:00 and SELL14:15, matching the running dashboard's markers. Uploaded Pine defaults produce BUY09:15, SELL10:30, BUY11:45 and SELL13:30, matching all four chart labels supplied by the user. The default smart exit additionally fires at13:15 for the11:45 long.

| Parameter | Old tuned15m settings observed at audit start | Uploaded Pine default |
|---|---:|---:|
| UT key |2.0|1.0|
| ATR period |14|10|
| Heikin Ashi source |True|False|
| Smart exit arm ATR |2.5|0.80|
| Peak retracement ATR |0.7|0.45|
| Hard retracement ATR |2.5|1.10|
| Required confirmations |4|2|

The same entries reproduced from137 runtime bars,300 merged bars and1153 stored bars. Therefore this observed mismatch is explained by settings, rather than inadequate indicator warmup. The runtime137 snapshot and stored data differ only in the open of the earliest23 September12:30 bar, by1.25 points; today's OHLC values agree.

Bar labels denote opening times. With close confirmation enabled, the09:15 signal becomes actionable at09:30. Tests prove the terminal09:15 candle confirms at09:30 without requiring a later candle to arrive, and a04:00UTC clock gives the same result as09:30IST.

## Vendor final-session anomaly

Stored candles use `source=fyers_history` and `volume_basis=fyers_index`. On29 September, one-minute candles15:15–15:28 have identical O/H/L/C22683.75, followed by a single15:29 closing jump to22716.20. On30 September,15:15–15:27 are all22616.60,15:28 moves to22620.45, and15:29 is flat. The five-minute and fifteen-minute records aggregate those same frozen paths. Volume alone does not establish a valid traded price path.

Consequently the stored29 September15:15 aggregate is O22683.75/H22716.20/L22683.75/C22716.20. Including it creates a BUY15:15 on29 September and suppresses the next09:15 BUY. Excluding the unverified final-session tail restores all four30 September labels. This result is recorded for1m,5m and15m in the JSON trace.

This is a data limitation. A normal NSE final15:15–15:30 candle should be preserved when its source is verified. These FYERS frozen paths cannot establish the exact TradingView final-session OHLC; a universal statement that every15:15 candle is invalid would exceed the evidence.

## Saved proof

- `tests/fixtures/uts_nifty_15min_2026_09_30.csv`:137 raw runtime OHLCV rows.
- `tests/test_uts_nifty_chart_parity_2026_09_30.py`:five regression tests; all passed.
- `reports/uts_nifty_chart_parity_20260930.xml`:test result.
- `reports/uts_nifty_pine_parameter_trace_20260930.json`:old/default settings, runtime137/merged300/all-data signal traces.
- `reports/uts_nifty_fyers_late_session_parity_20260930.json`:1m/5m/15m before/after session-tail traces and granular frozen candle evidence.
- `reports/uts_nifty_15min_300bar_parity_fixture_20260930.csv`:longer warmup fixture.

The root task changed settings during this audit. JSON parameter traces deliberately preserve the old configuration captured at the beginning; later current-settings reads reflect the updated code. Live process activation remains the root task's responsibility.
