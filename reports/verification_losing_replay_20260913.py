"""Read-only verification of the captured 96-trade replay; writes reports only.

No app initialization, broker calls, orders, settings changes or replay retuning.
The extracted simulator is the current production method with live guards off;
its small fixture proves the entry-time contract, not a replacement P&L result.
"""
import ast
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / 'reports'


def local_time(value):
    ts = (pd.Timestamp(value, unit='s', tz='UTC')
          if isinstance(value, (int, float)) else pd.Timestamp(value))
    return ts.tz_convert('Asia/Kolkata').tz_localize(None) if ts.tzinfo else ts


def summarize(rows):
    values = [float(row['pnl']) for row in rows]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    return dict(trades=len(rows), wins=len(wins), losses=len(losses),
                pnl=round(sum(values), 2), gross_winners=round(sum(wins), 2),
                gross_losers=round(sum(losses), 2),
                average_winner=round(sum(wins) / len(wins), 2) if wins else 0,
                average_loser=round(sum(losses) / len(losses), 2) if losses else 0,
                profit_factor=sum(wins) / -sum(losses) if losses else None)


def breakdown(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return {name: summarize(group) for name, group in sorted(groups.items())}


def simulator_timing_probe():
    source = ROOT / 'engine/signal_processor.py'
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SignalProcessor')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == '_simulate_actual_option_exit')
    module = ast.Module(body=[
        ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
        ast.ClassDef(name='Probe', bases=[], keywords=[], body=[method], decorator_list=[])
    ], type_ignores=[])
    settings = SimpleNamespace(ut_force_exit_time='15:25', backtest_live_exits_enabled=False)
    env = dict(pd=pd, datetime=datetime, timedelta=timedelta,
               SimpleNamespace=SimpleNamespace, get_settings=lambda: settings,
               _setting_enabled=lambda value, default=False: bool(value))
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), env)
    start = pd.Timestamp('2026-09-11 09:15')
    frame = pd.DataFrame({
        'open': [100, 100, 98, 100], 'high': [101, 102, 100, 102],
        'low': [99, 99, 84, 99], 'close': [100, 101, 90, 100],
    }, index=pd.to_datetime(['2026-09-11 09:15', '2026-09-11 09:16',
                             '2026-09-11 09:17', '2026-09-11 09:30']))
    result = env['Probe']()._simulate_actual_option_exit(
        frame, start, pd.Timestamp('2026-09-11 09:30'), 85, 3,
        natural_sl_distance=15, tf15_timeframe='15min')
    assert result['reason'] == 'SL HIT'
    assert result['exit_time'] + pd.Timedelta(minutes=1) < start + pd.Timedelta(minutes=15)
    return dict(signal_bar_start=str(start), signal_bar_close=str(start + pd.Timedelta(minutes=15)),
                result=result, interpretation='Simulator accepts bar-start entry and exits before a 15m signal can be confirmed.')


def main():
    capture = REPORTS / 'verification_20260913_trades.json'
    payload = json.loads(capture.read_text(encoding='utf-8'))
    rows = payload['closed']
    total = summarize(rows)
    assert total['trades'] == 96 and total['wins'] == 22 and total['losses'] == 74
    assert abs(total['pnl'] - (-60093.48)) < .001
    assert len({r['id'] for r in rows}) == 96
    assert abs(total['pnl'] - payload['summary']['daily_pnl']) < .001
    early = [r for r in rows if local_time(r['exit_timestamp']) + pd.Timedelta(minutes=1)
             < local_time(r['entry_timestamp']) + pd.Timedelta(r['timeframe'])]
    source_hashes = json.loads((REPORTS / 'remaining_audit_repairs_20260912_sources.json').read_text())['source_sha256']
    changed = [name for name, sha in source_hashes.items()
               if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != sha]
    gross = sum((r['exit_price'] - r['entry_price']) * r['lots'] * r['lot_size'] for r in rows)
    result = dict(
        capture_sha256=hashlib.sha256(capture.read_bytes()).hexdigest(),
        capture_simulation_id=payload['meta']['simulation_id'],
        total=total,
        calculated_option_price_pnl_before_charges=round(gross, 2),
        charges_inferred_from_rounded_prices=round(gross-total['pnl'], 2),
        timeframe=breakdown(rows, lambda r:r['timeframe']),
        grade=breakdown(rows, lambda r:r['grade']),
        index=breakdown(rows, lambda r:r['instrument'].split()[0]),
        engine=breakdown(rows, lambda r:r['source_engine']),
        exit=breakdown(rows, lambda r:r['exit_reason']),
        date=breakdown(rows, lambda r:str(local_time(r['entry_timestamp']).date())),
        completed_before_signal_bar_close=dict(
            summary=summarize(early), by_timeframe=dict(Counter(r['timeframe'] for r in early)),
            by_exit=dict(Counter(r['exit_reason'] for r in early)), ids=[r['id'] for r in early],
            qualification='Comparison allows exit at the END of the labelled 1m option candle; these rows still finish before their signal timeframe closes. It is not the P&L impact of correcting timing.'),
        simulator_timing_probe=simulator_timing_probe(),
        prior_certified_sources=dict(count=len(source_hashes), changed=changed),
        source_mtimes={name:datetime.fromtimestamp((ROOT/name).stat().st_mtime).isoformat()
                       for name in ('scanner.py','engine/signal_processor.py')},
    )
    output = REPORTS / 'verification_losing_replay_20260913.json'
    output.write_text(json.dumps(result, indent=2, default=str), encoding='utf-8')
    print(json.dumps({key:result[key] for key in ('capture_simulation_id','total',
        'calculated_option_price_pnl_before_charges','charges_inferred_from_rounded_prices',
        'prior_certified_sources')}, indent=2))
    print('Closed before source candle could close:',len(early),summarize(early)['pnl'])
    print('Production simulator timing probe:',result['simulator_timing_probe']['result']['reason'])


if __name__ == '__main__':
    main()
