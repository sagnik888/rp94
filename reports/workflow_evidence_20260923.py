"""Read-only ledger/shadow audit. Writes only the adjacent JSON report."""
from __future__ import annotations

import collections
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config.settings import get_settings
from intelligence.entry_policy_evaluation import extract_shadow_rows


def metrics(rows):
    available = [r for r in rows if r['outcome_available'] and r['after_cost_pnl_rs'] is not None]
    pnls = [r['after_cost_pnl_rs'] for r in available]
    return {
        'registered': len(rows), 'available': len(available),
        'wins': sum(p > 0 for p in pnls), 'losses': sum(p < 0 for p in pnls),
        'net_sum_rs': round(sum(pnls), 2),
        'mean_rs': round(statistics.mean(pnls), 2) if pnls else None,
        'median_rs': round(statistics.median(pnls), 2) if pnls else None,
        'entry_delay_median_s': round(statistics.median([r['entry_quote_delay_seconds'] for r in available if r['entry_quote_delay_seconds'] is not None]), 2) if available else None,
    }


def main():
    start = '2026-09-15'
    end = '2026-09-22'
    dbpath = ROOT / 'data_store/utbot_data.db'
    with sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        trades = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE data_provenance='LIVE' AND entry_filled_at!='' AND entry_filled_at>=? AND entry_filled_at<? ORDER BY entry_timestamp", (start, '2026-09-23'))]
        selected = []
        for trade in trades:
            duration = (datetime.fromisoformat(trade['exit_timestamp']) - datetime.fromisoformat(trade['entry_filled_at'])).total_seconds()
            row = {k: trade[k] for k in ('id','instrument','timeframe','grade','confidence','entry_filled_at','exit_timestamp','pnl','peak_pnl','exit_reason','scalp_lock_mode','runner_mode','market_regime','actual_costs_known','broker_realized_pnl')}
            row['fill_to_exit_seconds'] = round(duration, 3)
            row['quality_verdict'] = json.loads(trade['quality_verdict'] or '{}')
            selected.append(row)
        decisions = [dict(r) for r in conn.execute('SELECT date,timestamp,instrument,timeframe,direction,reason FROM signal_decisions WHERE date>=? AND date<=? ORDER BY timestamp', (start, end))]
        rejection_categories = collections.defaultdict(collections.Counter)
        for r in decisions:
            reason = r['reason'] or ''
            codes = re.findall(r'\[([A-Z][A-Z_]+)\]', reason)
            if codes: category = codes[-1]
            elif 'repainted during' in reason: category = 'STABILIZATION_REPAINT'
            elif 'Price-action ADVERSE' in reason: category = 'PRICE_ACTION_ADVERSE'
            elif 'Counter-session-bias' in reason: category = 'COUNTER_SESSION_BIAS'
            elif 'Session trend-phase' in reason: category = 'SESSION_TREND_PHASE'
            elif 'Market location risk' in reason: category = 'MARKET_LOCATION'
            else: category = 'OTHER'
            rejection_categories[r['date']][category] += 1
    events = []
    event_files = []
    for path in sorted((ROOT / 'data_store/rtracker/events').glob('2026-09-*.jsonl')):
        if start <= path.stem <= end:
            event_files.append(str(path.relative_to(ROOT)))
            events.extend(json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip())
    shadow = [r for r in extract_shadow_rows(events) if start <= r['session_date'] <= end]
    cohorts = {}
    for horizon in (15, 30, 60):
        cohorts[str(horizon)] = {group: metrics([r for r in shadow if r['horizon_minutes'] == horizon and r['decision_group'] == group]) for group in ('ACCEPTED', 'REJECTED')}
    days = {}
    for day in sorted({r['session_date'] for r in shadow}):
        days[day] = {group: metrics([r for r in shadow if r['session_date'] == day and r['horizon_minutes'] == 30 and r['decision_group'] == group]) for group in ('ACCEPTED', 'REJECTED')}
    settings = get_settings()
    keys = ['scanner_engine','auto_mode','ut_signal_mode','live_intrabar_signal_entries','signal_grade_preference','ut_confidence_calibration_live_apply_enabled','adaptive_hold_enabled','stale_regime_reversal_enabled','live_signal_stabilization_seconds_5min','live_signal_stabilization_seconds_15min','stabilization_regrade_enabled','opt_early_loss_cut_enabled','early_loss_cut_min_hold_seconds','breakeven_stop_arm_r_multiple','breakeven_stop_min_age_seconds','breakeven_stop_floor_rs','ut_market_location_gate']
    durations = [r['fill_to_exit_seconds'] for r in selected]
    report = {
        'scope': {'start': start, 'end': end, 'ledger': str(dbpath), 'event_files': event_files, 'database_access': 'mode=ro'},
        'settings_from_disk': {k: getattr(settings,k,None) for k in keys},
        'signal_fire_mode': settings.get_signal_fire_mode(),
        'source_sha256': {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in ['scanner.py','engine/signal_manager.py','engine/signal_processor.py','engine/quality_verdict.py','trading/trade_manager.py','config/settings.py']},
        'filled_live_summary': {'count': len(selected), 'wins': sum(r['pnl']>0 for r in selected), 'losses': sum(r['pnl']<0 for r in selected), 'recorded_net_rs': round(sum(r['pnl'] for r in selected),2), 'median_fill_to_exit_seconds': statistics.median(durations), 'min_seconds': min(durations), 'max_seconds': max(durations), 'under_180_seconds': sum(d<180 for d in durations), 'scalp_lock_count': sum(bool(r['scalp_lock_mode']) for r in selected), 'exit_reasons': dict(collections.Counter(r['exit_reason'] for r in selected))},
        'filled_live_trades': selected,
        'rejection_activity_rows_not_unique_signals': dict(rejection_categories),
        'shadow_fixed_horizon_comparison': cohorts,
        'shadow_30min_by_session': days,
        'shadow_unavailable_reasons': dict(collections.Counter(r['unavailable_reason'] for r in shadow if not r['outcome_available'])),
        'limitations': ['Ledger P&L is app recorded, not independent broker reconciliation.', 'Rejection activity contains duplicate descriptions and is not a unique raw-signal funnel.', 'Shadow outcomes use recorded executable touches and modeled costs, not fills or complete exit simulation.', 'Accepted and rejected cohorts are not randomized, include overlapping exposure, and do not prove causal filter value.', 'Current saved settings differ from some historical builds; no running build was verified.'],
    }
    dest = Path(__file__).with_suffix('.json')
    dest.write_text(json.dumps(report,indent=2), encoding='utf-8')
    print(json.dumps({k:report[k] for k in ['filled_live_summary','shadow_fixed_horizon_comparison','shadow_30min_by_session','shadow_unavailable_reasons']},indent=2))


if __name__ == '__main__':
    main()
