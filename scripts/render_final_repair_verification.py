"""Render the reconciled final evidence without changing strategy or data."""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
reports = root / 'reports'
a = json.loads((reports / 'paired_replay_expiry_analysis_20260914.json').read_text(encoding='utf-8'))
runtime = json.loads((reports / 'runtime_expiry_verified_20260914.json').read_text(encoding='utf-8'))
before, after = a['baseline'], a['corrected']
audit = a['corrected_price_audit']
assert not audit['issues'] and not audit['guard_errors']
assert audit['contract_expiry_stop_mappings_checked'] == after['trades']
money = lambda v: ('−' if v < 0 else '') + f'₹{abs(v):,.2f}'
old = (reports / 'integrated_repair_verification_20260913.md').read_text(encoding='utf-8')
fixes = old.split('## Implemented corrections\n\n', 1)[1].split('\n## Completed paired replay', 1)[0]
fixes = '\n'.join(line for line in fixes.splitlines() if line.strip()
    and not line.startswith('| Historical entry helpers could inspect')
    and not line.startswith('| Historical DTE depended on'))
fixes += '\n| Historical entry helpers could inspect the next unfinished candle or future peer signal | Thrust, VWAP, structure, reclaim and cached cross-index votes use the same candle-close availability clock. Empty historical depth cannot fetch live depth. |'
fixes += '\n| Historical DTE depended on today\'s broker login/master and could change option stop geometry | Historical expiry uses archived identity; observed stop mapping uses the exact traded contract expiry. The actual option open and mapped/capped stop feed risk sizing. Missing chain expiry abstains from IV/Greeks calculations. Live execution keeps its current-master requirements. |\n'
comparison = '\n'.join(f'| {label} | {fmt(before[key])} | {fmt(after[key])} |' for label, key, fmt in [
    ('Trades', 'trades', str), ('Net after configured costs', 'net', money),
    ('Win rate', 'win_rate', lambda v: f'{v:.2f}%'), ('Profit factor', 'profit_factor', str),
    ('Chronological maximum drawdown', 'max_drawdown', money), ('Configured costs', 'charges', money)])
def table(group):
    return '\n'.join(f'| {name} | {v["trades"]} | {money(v["net"])} | {v["wins"]}/{v["losses"]} |' for name,v in group.items())
matched = a['matched_ids']
chron = a['corrected_groups']['chronological']
report = f'''# Trading-system repairs and final verification — 14 September 2026

The reproduced defects are repaired and the clean full suite passed **6,256 tests, zero failures/errors/skips**. The final frozen-input replay returned **{money(after['net'])} after configured costs across {after['trades']} option trades**, with {after['wins']} wins and {after['losses']} losses. **Reliable profitability has not been established.**

This final result supersedes the earlier corrected 86-trade / −₹63.92 provisional result and the stopped `offline_final_20260914` run. The latter was invalidated when runtime verification exposed the remaining expiry-to-stop mismatch. No strategy parameter was fitted to this sample to manufacture a profit.

The originally reported **−₹60,093.48, 96 trades, 22 wins and 74 losses** remains a captured backtest result. Its old running process had no trustworthy source fingerprint or original contract-input trace. The saved-source baseline below therefore cannot be presented as an exact reconstruction of those 96 trades.

## Implemented corrections

{fixes}
## Completed paired replay

| Identical frozen inputs and settings | Before repairs | Final corrected source |
|---|---:|---:|
{comparison}

The paired net change is **{money(a['net_change'])}**. This comparison measures changes in software decisions and replay correctness; it does not establish that the old build's apparent gains were tradable. The corrected gross result before configured costs is {money(after['net'] + after['charges'])}. Costs of {money(after['charges'])} then produce the net above.

| Index | Trades | Net after costs | Wins/losses |
|---|---:|---:|---:|
{table(a['corrected_groups']['index'])}

| Timeframe | Trades | Net after costs | Wins/losses |
|---|---:|---:|---:|
{table(a['corrected_groups']['timeframe'])}

| Grade | Trades | Net after costs | Wins/losses |
|---|---:|---:|---:|
{table(a['corrected_groups']['grade'])}

{matched['same_contract']} trades share an original signal ID and exact contract across the two builds. Their net changed from {money(matched['baseline_same_contract']['net'])} to {money(matched['corrected_same_contract']['net'])}. There are {matched['changed_contract']} shared IDs with changed contracts, {len(matched['removed'])} removed IDs and {len(matched['added'])} added IDs. Aggregate differences cannot be attributed solely to filtering.

Chronological slices are descriptive: {', '.join(name + ' ' + money(v['net']) for name,v in chron.items())}; the last five available sessions returned {money(a['corrected_groups']['last_five_sessions']['net'])}. These are not a prospective holdout certification. Removing a losing index or changing gates after reading these results would require a fresh independent evaluation.

## Trade-path verification

All **{audit['observed_option_rows_checked']} observed option trade paths** passed the independent contract, input digest, signal-availability, first-eligible-open and exit-OHLC checks. All **{audit['contract_expiry_stop_mappings_checked']}** passed exact expiry/DTE, delta sign, stop mapping and sizing-entry/risk/lot reconciliation. There were **zero price/contract issues and zero `ERROR_ABSTAIN` guard events**. The validated Parquet bundle accounted for {audit['bundle_union_column_digests_verified']} digests containing extra null telemetry columns; their original OHLCV and complete bundle hashes were matched.

{len(audit['rows_missing_complete_index_1m_management_window'])} trades lacked a complete underlying one-minute management window. Their option price paths were present; missing underlying observations cannot be recreated by a code fix. The run contains observed options, with no synthetic-option or futures-proxy trades.

The delta used for natural stop sizing remains an **estimate using the existing 15% IV assumption** when historical Greeks are absent. The audit records that fact. Observed option prices are not evidence that historical Greeks or real execution were observed.

## Broker and data meaning

FYERS constituent-index candle volume, futures-contract cumulative volume/OI, and option-contract volume/OI remain separate data families. Futures/option interval evidence must match provider, contract, expiry, strike/leg, units, exchange clock and 1/5/15-minute coverage. Receipt-timed or sparse chain fallbacks cannot impersonate an exchange-measured minute interval. Missing participation does not become bearish, zero activity, or a second cash-volume confirmation.

Eligible derivative participation can adjust existing aggregate conviction once within ±10%; it cannot invent/reverse direction or add a hard entry gate. Exact historical intelligence feeds entry and post-entry management; regime/session data use completed bars at the decision clock. See `derivative_repairs_20260913.md`, `historical_context_repairs_20260913.md`, `final_entry_clock_repairs_20260914.md` and `historical_expiry_repairs_20260914.md` for implementation evidence.

Historical chain, exchange-timed futures/option participation and VIX coverage are unavailable for these saved sessions. The replay therefore verifies missing-data abstention and the observed price/regime paths, not the profitability of derivative-volume inputs. The new archive records future observations; it cannot reconstruct the past.

## Reproducibility and runtime

The inputs contain **345 checksummed files**. Input manifest SHA256: `{a['input_manifest_sha256']}`. Before/after settings hashes match and both runs recorded zero network attempts. The harness uses production historical candidate and management adapters, isolated databases, exact saved option prices and the same full cached frames. Application loading/retention is checked separately.

```text
venv/Scripts/python.exe -W ignore -m pytest tests -o addopts=--import-mode=importlib -q --disable-warnings --tb=short --junitxml=reports/all_expiry_verified_20260914.xml
venv/Scripts/python.exe -W ignore scripts/verify_offline_engine_replay.py --source reports/replay_expiry_final_source_20260914 --inputs reports/replay_frozen_inputs_v2_20260913 --output reports/offline_expiry_final_20260914.json
venv/Scripts/python.exe scripts/analyze_paired_engine_replay.py --corrected reports/offline_expiry_final_20260914.json --output reports/paired_replay_expiry_analysis_20260914.json --require-expiry-evidence
```

The clean full suite passed in **288.66 seconds**. Two synthetic-mark test stubs in the preceding run lacked the new expiry adapter; the fixtures were completed and the entire suite rerun. No failing test was disabled. Monetary figures use full-precision prices and charges because the generic serializer rounds displayed P&L to whole rupees; drawdown is ordered by exact exit timestamps.

The app was restarted in **HISTORICAL mode, Auto off, 30-day selection restored**. Verified runtime fingerprint: `{runtime['runtime_build']['fingerprint']}`, with `reload_required=false`. No live orders were placed. The runtime proof records dashboard recalculation state separately from the completed frozen replay; a building dashboard is not a completed performance result. See `runtime_expiry_verified_20260914.json`.

## Remaining limits and live-market implications

- Saved one-minute index history covers approximately 14 sessions and five-minute history 22 sessions. A 30-day selection does not create the missing observations.
- The saved live configuration uses INTRACANDLE signals. OHLC cannot recreate actual intrabar signal arrival or event order. This replay waits for source-bar closure and uses conservative stop-first handling when candle ordering is unknown.
- Configured execution costs are included. Liquidity, queue priority, real spreads and actual broker fills have not been validated by a candle replay.
- A/A+ grades are not established win probabilities. Software correctness and profitable trading are separate claims. A fully recorded independent sample is still needed to test the strategy's edge.
- Historical market-location gating retains its pre-existing explicit bypass; the entry-clock repair does not claim that this gate runs on historical trades.

Backtest losses do **not** debit the broker account. Shared data, grading and trade-management defects can affect real trades, but a backtest amount is not an exact forecast of live loss. The fixes and passing tests do not justify a guarantee of profitability.
'''
(reports / 'integrated_repair_verification_20260913.md').write_text(report,encoding='utf-8')
print(json.dumps({'written':'integrated_repair_verification_20260913.md','corrected':after,'checked':audit['observed_option_rows_checked']}))
