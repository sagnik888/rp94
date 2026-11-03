"""Offline S1 causality audit at the operator's 15:15 IST cutoff."""
from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from config.settings import get_settings
from engine.signal_processor import _entry_cutoff
from engine.s1_engine import compute_features, simulate, params_from_settings

CUTOFF = pd.Timestamp('2026-09-23 15:15')
CASES = [('NIFTY', '5min'), ('NIFTY', '15min'), ('BANKNIFTY', '15min'), ('MIDCPNIFTY', '5min')]


def read(symbol, tf):
    return pd.read_csv(ROOT / f'data_store/candles/{symbol}_{tf}.csv',
                       index_col='timestamp', parse_dates=True).loc[:'2026-09-23 23:59:59']


def events(run, frame, minutes):
    """Only events whose signal/exit bar has closed by the cutoff."""
    delta = pd.Timedelta(minutes=minutes)
    out = []
    for t in run.trades:
        ts = frame.index[t.entry_bar]
        if ts.date() != CUTOFF.date() or ts + delta > CUTOFF:
            continue
        exit_ts = frame.index[t.exit_bar] if t.exit_bar >= 0 else None
        exit_known = exit_ts is not None and exit_ts + delta <= CUTOFF
        out.append({'entry_bar_open': str(ts), 'entry_available_at': str(ts + delta),
                    'direction': t.direction, 'chart_entry': t.entry_price,
                    'signal_close': t.signal_close,
                    'exit_bar_open': str(exit_ts) if exit_known else None,
                    'exit_available_at': str(exit_ts + delta) if exit_known else None,
                    'exit_reason': t.exit_reason if exit_known else None})
    return out


def poison_future(frame, minutes):
    frame = frame.copy()
    mask = frame.index + pd.Timedelta(minutes=minutes) > CUTOFF
    frame.loc[mask, ['open', 'close']] = 1000000.
    frame.loc[mask, 'high'] = 1200000.
    frame.loc[mask, 'low'] = 1.
    frame.loc[mask, 'volume'] = 1.e12
    return frame


def main():
    settings = get_settings()
    result = {'cutoff_ist': str(CUTOFF), 'timestamp_convention': 'bar open; include bars closing by cutoff',
              'settings': {key: getattr(settings, key) for key in (
                  'scanner_engine', 'ut_session_start', 'ut_session_end', 'ut_force_exit_time',
                  'ut_no_entry_after', 'ut_5min_no_entry_after', 'eod_protect_enabled',
                  'eod_protect_window_minutes', 'block_entries_during_eod_protect')},
              'engine_sha256': hashlib.sha256((ROOT / 'engine/s1_engine.py').read_bytes()).hexdigest(),
              'cases': []}
    for symbol, tf in CASES:
        minutes = int(tf.replace('min', ''))
        full, five = read(symbol, tf), read(symbol, '5min')
        prefix = full.loc[full.index + pd.Timedelta(minutes=minutes) <= CUTOFF]
        lower_prefix = five.loc[five.index + pd.Timedelta(minutes=5) <= CUTOFF]
        p = params_from_settings(settings, symbol)
        runs = {}
        for name, frame, lower in (
            ('full', full, five), ('cutoff', prefix, lower_prefix),
            ('future_poisoned', poison_future(full, minutes), poison_future(five, 5)),
        ):
            f = compute_features(frame, tf, p, df_5min=lower)
            runs[name] = (frame, simulate(frame, tf, p, features=f))
        base = runs['cutoff'][1]
        checks = {}
        for name in ('full', 'future_poisoned'):
            other = runs[name][1]
            feature_differences = [key for key, values in base.features.items()
                                   if not np.allclose(values, other.features[key][:len(prefix)], equal_nan=True)]
            checks[name] = {
                'feature_differences': feature_differences,
                'positions_identical': bool(np.array_equal(base.positions, other.positions[:len(prefix)])),
                'stops_identical': bool(np.allclose(base.stops, other.stops[:len(prefix)], equal_nan=True)),
                'events_identical': events(base, prefix, minutes) == events(other, runs[name][0], minutes)}
            assert not feature_differences, (symbol, tf, name, feature_differences)
            assert all(checks[name][k] for k in ('positions_identical', 'stops_identical', 'events_identical'))
        latest = prefix.iloc[-1]
        cutoff_label, _ = _entry_cutoff(settings, tf)
        row = {'symbol': symbol, 'timeframe': tf, 'effective_entry_cutoff': cutoff_label,
               'last_completed_bar_open': str(prefix.index[-1]),
               'last_completed_ohlcv': {k: float(latest[k]) for k in ('open', 'high', 'low', 'close', 'volume')},
               'raw_s1_position_at_cutoff_before_execution_squareoff': int(base.positions[-1]),
               'raw_s1_session_rules': p.session_rules,
               'features_at_cutoff': {k: float(base.features[k][-1]) for k in (
                   'rsi', 'rsi5m', 'adx', 'di_plus', 'di_minus', 'bull_score', 'bear_score')},
               'events_through_cutoff': events(base, prefix, minutes), 'causality_checks': checks}
        result['cases'].append(row)
        print(symbol, tf, 'last completed', prefix.index[-1], 'close', latest['close'],
              'position', base.positions[-1], 'events', len(row['events_through_cutoff']), 'causality PASS')
    (ROOT / 'reports/s1_cutoff_verification_20260924.json').write_text(json.dumps(result, indent=2))
    print('Settings:', result['settings'])


if __name__ == '__main__':
    main()
