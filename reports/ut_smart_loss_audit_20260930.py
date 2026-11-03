"""Read-only UT SMART EXIT loss audit; no orders or runtime setting writes.

Only the adjacent evidence JSON is written. The dashboard credential stays in
memory. Production SQLite is opened read-only. Candle reconstruction uses final
OHLC and cannot recover the intrabar ticks observed by yesterday's process.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import ut_smart_engine as uts
from engine import uts_lane
from engine.alt_scanners import AltSignalEngine

OUTPUT = Path(__file__).with_suffix('.json')
SAFE_CONFIG = {'scanner_engine', 'auto_mode', 'signal_fire_mode',
               'live_intrabar_signal_entries', 'ut_timeframe_entry_policy',
               'ut_no_entry_after', 'ut_no_entry_after_5min', 'ut_no_entry_after_1min'}
TRADE_COLS = ('id,instrument,timeframe,direction,entry_price,entry_time,exit_price,'
              'exit_time,exit_reason,pnl,charges,peak_pnl,max_drawdown,is_ghost,'
              'status,execution_status,source_lane,signal_time,initial_stop,'
              'current_stop,entry_spot,spot_stop,entry_filled_at,actual_costs_known')


def request(base, path, token=None):
    headers = {'x-dashboard-token': token} if token else {}
    with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers), timeout=10) as response:
        return json.load(response)


def load_frame(path):
    df = pd.read_csv(path, index_col=0, parse_dates=True).sort_index()
    return df[~df.index.duplicated(keep='last')][['open', 'high', 'low', 'close', 'volume']].astype(float)


def compact_archived_trade(row):
    q = row.get('quality') or {}
    geometry = row.get('geometry') or {}
    price = float(row.get('price') or 0)
    stop = float(geometry.get('premium_stop') or row.get('stop') or 0)
    start, end = pd.Timestamp(row['entry_timestamp']), pd.Timestamp(row['exit_timestamp'])
    bar = pd.Timestamp(row['signal_timestamp'])
    minutes = int(row['timeframe'].replace('min', ''))
    return {
        **{k: row.get(k) for k in ('instrument', 'timeframe', 'direction', 'signal_timestamp',
           'entry_timestamp', 'exit_timestamp', 'price', 'stop', 'exit_price', 'pnl',
           'round_trip_cost', 'peak_pnl', 'source_lane', 'lifecycle_id', 'exit_reason', 'grade', 'confidence')},
        'held_seconds': (end - start).total_seconds(),
        'entry_before_signal_bar_close': bool(start < bar + pd.Timedelta(minutes=minutes)),
        'premium_stop_pct': 100 * (price - stop) / price if price else None,
        'quality_allowed': q.get('allowed'), 'quality_reason_code': q.get('reason_code'),
        'quality_lane': (q.get('checks') or {}).get('lane'),
        'geometry': geometry,
        'entry_intel': {k: (row.get('entry_intel') or {}).get(k) for k in
                        ('adx', 'regime_label', 'day_type', 'aggregate_score', 'delta')},
    }


def mechanism_probes(settings):
    # No state/result freshness check exists before an absent flip is called REPAINT.
    now = datetime(2026, 9, 30, 10, 5)
    trade = SimpleNamespace(source_engine='ut_smart', source_lane='UTS', timeframe='15min',
                            signal_time=datetime(2026, 9, 30, 10), direction='LONG',
                            entered_intracandle=True)
    failed = uts_lane.repaint_exit(copy.deepcopy(trade),
                                  {'error': 'Insufficient UT SMART EXIT data', 'signals': [], 'state': None},
                                  now, settings)
    stale = uts_lane.repaint_exit(copy.deepcopy(trade),
                                 {'signals': [], 'state': {'uts_state_bar_time': datetime(2026, 9, 30, 9, 30)}},
                                 now, settings)
    # The price never moves above 101 after entry at 100. The old signal-bar high
    # nevertheless arms a 2.5-ATR exit on the second subsequent bar.
    cl = np.array([100., 100., 100., 100.])
    hi = np.array([101., 130., 101., 101.])
    lo = np.array([99., 99., 99., 99.])
    trail = np.array([105., 50., 50., 50.])
    ind = dict(src=cl, open=cl, high=hi, low=lo, close=cl, atr=np.full(4, 10.),
               trail=trail, rsi=np.full(4, 50.), hist=np.ones(4), ema_exit=np.zeros(4))
    p = uts.UTSParams(arm_atr=2.5, hard_retrace_atr=2.5, min_bars=2, confirmations=4)
    output = uts.run(ind, p)
    decision = uts_lane.exit_decision(trade, 99., 80., settings=settings)
    assert failed == stale == 'REPAINT'
    assert output['signal'][1] == 1 and output['exit'][3] == uts.EXIT_HARD
    assert not decision.exit and decision.index_stop == 0 and decision.premium_stop == 0
    return {'error_result_exit': failed, 'stale_result_exit': stale,
            'signal_bar_peak_exit': uts.EXIT_NAMES[int(output['exit'][3])],
            'counted_runup_atr': float(output['runup_atr'][3]),
            'actual_post_entry_high_runup_atr': .1,
            'chartless_lane_exit': decision.exit, 'chartless_lane_stop': decision.index_stop,
            'scope': 'Synthetic mechanism proofs; not proof these defects caused recorded exits.'}


def closing_boundary_probe(settings):
    """A confirmed BANKNIFTY flip disappears until the next bar exists in input."""
    import pytz
    prior = load_frame(ROOT / 'data_store/candles/BANKNIFTY_5min.csv')
    archived = load_frame(ROOT / 'data_store/session_archive/2026-09-29/candles/BANKNIFTY_5min.csv')
    frame = pd.concat([prior[prior.index < pd.Timestamp('2026-09-29')], archived]).sort_index()
    frame = frame.loc[:'2026-09-29 11:25'].tail(300)
    now = datetime(2026, 9, 29, 11, 30, 3)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return pytz.timezone('Asia/Kolkata').localize(now).astimezone(tz) if tz else now
    settings = copy.copy(settings)
    settings.get_signal_fire_mode = lambda: 'INTRACANDLE'
    trade = SimpleNamespace(source_engine='ut_smart', source_lane='UTS', timeframe='5min',
                            signal_time=datetime(2026, 9, 29, 11, 25), direction='LONG',
                            entered_intracandle=True)
    # No new prices: the added bar is only a flat sentinel telling the existing
    # implementation that the preceding bar closed.
    next_row = frame.iloc[[-1]].copy()
    next_row.index = pd.DatetimeIndex([pd.Timestamp('2026-09-29 11:30')])
    price = float(frame['close'].iloc[-1])
    for col in ('open', 'high', 'low', 'close'):
        next_row[col] = price
    next_row['volume'] = 0.
    with patch('datetime.datetime', Clock), patch('config.settings.get_settings', lambda: settings):
        missing_next = AltSignalEngine(signal_source='ut_smart').process(frame, 'BANKNIFTY', '5min')
        with_next = AltSignalEngine(signal_source='ut_smart').process(pd.concat([frame, next_row]), 'BANKNIFTY', '5min')
        before = uts_lane.repaint_exit(copy.deepcopy(trade), missing_next, now, settings)
        after = uts_lane.repaint_exit(copy.deepcopy(trade), with_next, now, settings)
    assert before == 'REPAINT' and after == ''
    return {'clock': now.isoformat(), 'signal_time': trade.signal_time.isoformat(),
            'closed_signal_without_next_input_bar': any(s.timestamp == trade.signal_time for s in missing_next['signals']),
            'closed_signal_with_next_input_bar': any(s.timestamp == trade.signal_time for s in with_next['signals']),
            'exit_without_next_input_bar': before, 'exit_with_next_input_bar': after,
            'scope': 'Production engine reproduction using final archived OHLC; original intrabar input state is unavailable.'}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    base = 'http://127.0.0.1:8500'
    status = request(base, '/api/status')
    config = {k: v for k, v in status.get('config', {}).items() if k.startswith('uts_') or k in SAFE_CONFIG}
    settings = SimpleNamespace(**config)
    token = request(base, '/api/client_config')['dashboard_token']
    diagnostics = request(base, '/api/diagnostics', token)
    chart_response = request(base, '/api/chart?instrument=NIFTY&tf=15min&bars=400', token)
    chart = (chart_response.get('chart') or {}).get('15min') or {}
    with sqlite3.connect((ROOT / 'data_store/utbot_data.db').as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        current_trades = [dict(r) for r in conn.execute('SELECT ' + TRADE_COLS + ' FROM trades')]
        today_intel_row = conn.execute("SELECT entry_intel FROM trades WHERE id='T0001_NIFTY_09301015'").fetchone()
        today_intel = json.loads(today_intel_row[0]) if today_intel_row else {}
        session = [dict(r) for r in conn.execute("SELECT id,date,payload FROM session_signals WHERE date IN ('2026-09-29','2026-09-30')")]
    actual_by_day = {}
    for day in ('2026-09-28', '2026-09-29'):
        rows = json.loads((ROOT / 'data_store/session_archive' / day / 'trades.json').read_text(encoding='utf-8'))
        actual = [compact_archived_trade(r) for r in rows if r.get('status') == 'CLOSED'
                  and r.get('source') == 'trade_manager_terminal_fill']
        actual_by_day[day] = {'trades': actual, 'count': len(actual),
                             'net_pnl': sum(r['pnl'] for r in actual),
                             'exit_reasons': dict(Counter(r['exit_reason'] for r in actual)),
                             'repaint_net': sum(r['pnl'] for r in actual if r['exit_reason'] == 'UTS_REPAINT'),
                             'quality_rejected_count': sum(r['quality_allowed'] is False for r in actual),
                             'quality_rejected_net': sum(r['pnl'] for r in actual if r['quality_allowed'] is False)}
    # Final-bar checks are reconstructed at the signal's close, with no later prices.
    confirmations = []
    for row in actual_by_day['2026-09-29']['trades']:
        instrument, tf = row['instrument'], row['timeframe']
        archived = load_frame(ROOT / f'data_store/session_archive/2026-09-29/candles/{instrument}_{tf}.csv')
        prior_history = load_frame(ROOT / f'data_store/candles/{instrument}_{tf}.csv')
        prior_history = prior_history[prior_history.index < pd.Timestamp('2026-09-29')]
        frame = pd.concat([prior_history, archived]).sort_index()
        frame = uts.drop_off_session_bars(frame.loc[:row['signal_timestamp']])
        p = uts.params_from_settings(settings, tf)
        outputs = {}
        for label, subset in [('full_history', frame), ('last_300_bars', frame.tail(300))]:
            ind = uts.indicators(subset, p)
            out = uts.run(ind, p)
            outputs[label] = {'bars': len(subset), 'signal': int(out['signal'][-1]), 'state': int(out['state'][-1]),
                              'close': float(ind['close'][-1]), 'source': float(ind['src'][-1]),
                              'trail': float(ind['trail'][-1]), 'atr': float(ind['atr'][-1])}
        confirmations.append({'instrument': instrument, 'timeframe': tf,
                              'signal_timestamp': row['signal_timestamp'], 'live_direction': row['direction'],
                              'reconstruction': outputs})
    minute = load_frame(ROOT / 'data_store/candles/NIFTY_1min.csv')
    today = minute[minute.index.strftime('%Y-%m-%d') == '2026-09-30']
    bars = today.resample('15min', origin='start_day', offset='15min').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}).dropna()
    prior = load_frame(ROOT / 'data_store/candles/NIFTY_15min.csv')
    joined = uts.drop_off_session_bars(pd.concat([prior[prior.index < pd.Timestamp('2026-09-30')], bars]).sort_index())
    p = uts.params_from_settings(settings, '15min')
    entry_frame = joined.loc[:'2026-09-30 10:00'].tail(300)
    entry_ind = uts.indicators(entry_frame, p)
    entry_out = uts.run(entry_ind, p)
    today_trade = next((r for r in current_trades if r['id'] == 'T0001_NIFTY_09301015'), None)
    today_stop = {}
    if today_trade:
        start, end = pd.Timestamp('2026-09-30 10:15'), pd.Timestamp('2026-09-30 10:33')
        risk = abs(today_trade['entry_spot'] - today_trade['spot_stop'])
        delta = abs(float(today_intel.get('delta') or .5))
        premium_risk = today_trade['entry_price'] - today_trade['initial_stop']
        today_stop = {'trade': today_trade, 'entry_intel': today_intel,
                      'index_stop_distance': risk, 'delta_mapped_index_risk': risk * delta,
                      'actual_premium_risk': premium_risk,
                      'actual_stop_pct': premium_risk / today_trade['entry_price'] * 100,
                      'premium_risk_to_delta_mapped_index_risk': premium_risk / (risk * delta),
                      'underlying_low_until_stop_minute': float(today.loc[start:end, 'low'].min()),
                      'entry_bar_atr_reconstructed': float(entry_ind['atr'][-1]),
                      'entry_bar_flip_reconstructed': int(entry_out['signal'][-1]),
                      'smart_arm_move_reconstructed': float(entry_ind['atr'][-1]) * p.arm_atr,
                      'earliest_two_post_signal_closed_bars_time': '2026-09-30T10:45:00'}
    session_compact = []
    for raw in session:
        row = json.loads(raw['payload'])
        session_compact.append({'id': raw['id'], 'date': raw['date'],
                                **{k: row.get(k) for k in ('instrument', 'timeframe', 'signal_timestamp',
                                   'entry_time', 'status', 'entered_intracandle', 'exit_reason', 'pnl')}})
    source_paths = ('engine/ut_smart_engine.py', 'engine/uts_lane.py', 'engine/s1_lane.py',
                    'engine/alt_scanners.py', 'engine/multi_timeframe.py', 'engine/exit_cycle.py',
                    'engine/signal_processor.py', 'engine/execution_service.py', 'scanner.py',
                    'config/settings.py', 'trading/trade_manager.py',
                    'reports/ut_smart_exit_study_20260925.py', 'reports/ut_smart_intracandle_study_20260926.py')
    result = {'captured_at_ist': str(pd.Timestamp.now(tz='Asia/Kolkata')), 'runtime_config': config,
              'runtime': {k: status.get(k) for k in ('mode', 'scanner_running', 'pid', 'last_scan', 'scan_count')},
              'diagnostics': {k: diagnostics.get(k) for k in ('runtime_build', 'scanner_freshness', 'timeouts',
                'latency', 'exit_reasons', 'repaint_guard', 'risk_configuration', 'live_session_measurement')},
              'archived_actual_fills': actual_by_day, 'current_sqlite_trades': current_trades,
              'session_rows': session_compact, 'final_bar_confirmations': confirmations,
              'today_stop_analysis': today_stop, 'mechanism_probes': mechanism_probes(settings),
              'closing_boundary_probe': closing_boundary_probe(settings),
              'runtime_chart': {'state': chart.get('state'), 'engine': chart.get('engine'),
                                'candle_type': chart.get('candle_type'), 'candles': chart.get('candles'),
                                'markers': chart.get('markers')},
              'source_sha256': {path: hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in source_paths},
              'limitations': ['Archived terminal-fill rows are app-recorded fills; exchange contract notes and actual charges are unavailable.',
                  'Do not sum filled-trade P&L with mirrored session/ghost rows.',
                  'Final candles cannot distinguish every original intrabar disappearance from a stale/failed engine result.',
                  'Final-bar reconstruction uses present parameters and revised vendor history, not immutable live input snapshots.',
                  'Synthetic mechanism probes demonstrate behavior, not realized P&L improvement.',
                  'No matched actual-contract live comparison with other engines exists in this sample.']}
    OUTPUT.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(OUTPUT), 'actual_29_sep': {k:v for k,v in actual_by_day['2026-09-29'].items() if k!='trades'},
                      'today_stop': {k:v for k,v in today_stop.items() if k not in ('trade', 'entry_intel')},
                      'final_bar_confirmations': confirmations, 'probes': result['mechanism_probes'],
                      'closing_boundary_probe': result['closing_boundary_probe']}, indent=2))


if __name__ == '__main__':
    main()
