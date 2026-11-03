"""Offline replay of the reported 10:45 versus 12:00 S1 divergence."""
from pathlib import Path
from dataclasses import replace, asdict
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from engine.s1_engine import compute_features, simulate, get_s1_signals, params_from_settings, s1_candle_frame
from engine.alt_scanners import AltSignalEngine
from engine.scanner_chart_mixin import ScannerChartMixin
from types import SimpleNamespace

CUTOFF = pd.Timestamp('2026-09-23 15:15')


def read(symbol, tf):
    path = ROOT / f'data_store/candles/{symbol}_{tf}.csv'
    df = pd.read_csv(path, index_col='timestamp', parse_dates=True)
    return df.loc[df.index + pd.Timedelta(minutes=int(tf.replace('min', ''))) <= CUTOFF]


def events(signals):
    return [{'bar_open': str(s.timestamp), 'side': s.signal_type, 'market_close': s.price,
             'chart_entry': s.s1_chart_entry, 'chart_close': s.s1_chart_signal_close,
             'exit_bar_open': str(s.s1_exit_time) if s.s1_exit_time else None,
             'exit_reason': s.s1_exit_reason}
            for s in signals if pd.Timestamp(s.timestamp).date() == CUTOFF.date()]


def main():
    output = {'date': str(CUTOFF.date()), 'cutoff': str(CUTOFF),
              'engine_sha256': hashlib.sha256((ROOT / 'engine/s1_engine.py').read_bytes()).hexdigest(),
              'frames': []}
    for symbol in ('NIFTY', 'BANKNIFTY', 'MIDCPNIFTY', 'SENSEX'):
        native = {tf: read(symbol, tf) for tf in ('1min', '5min', '15min')}
        for tf in ('1min', '5min', '15min'):
            raw = native[tf]
            if raw.empty:
                continue
            item = {'symbol': symbol, 'timeframe': tf, 'rows': len(raw),
                    'input_hash': hashlib.sha256(pd.util.hash_pandas_object(raw, index=True).values.tobytes()).hexdigest(),
                    'settings': asdict(params_from_settings(instrument=symbol)), 'variants': {}}
            for kind in ('standard', 'heikin_ashi'):
                p = replace(params_from_settings(instrument=symbol), candle_type=kind)
                f = compute_features(raw, tf, p, df_5min=native['5min'], df_15min=native['15min'])
                sigs, positions, stops = get_s1_signals(raw, symbol, tf, p, features=f)
                run = {'events': events(sigs), 'final_position': int(positions[-1]),
                       'last_chart_ohlc': {k: float(f[k][-1]) for k in ('open', 'high', 'low', 'close')},
                       'last_features': {k: float(f[k][-1]) for k in ('rsi', 'rsi5m', 'adx', 'bull_score', 'bear_score')}}
                if symbol == 'NIFTY' and tf == '15min':
                    i = raw.index.get_loc(pd.Timestamp('2026-09-23 10:45'))
                    run['at_1045'] = {k: float(f[k][i]) for k in (
                        'open', 'high', 'low', 'close', 'adx', 'atr', 'body_pct', 'bull_score',
                        'bear_score', 'rel_vol', 'ema9_15', 'ema21_15', 'ema9_15_slope')}
                    run['at_1045']['body_size'] = float(abs(f['close'][i] - f['open'][i]))
                    run['at_1045']['minimum_body'] = float(f['atr'][i] * .15)
                item['variants'][kind] = run
            # Verify the real scanner result uses HA gates but publishes market prices.
            engine = AltSignalEngine(signal_source='s1', signal_mode='realtime')
            result = engine.process(raw, symbol, tf, df_5min=native['5min'], df_15min=native['15min'])
            assert events(result['signals']) == item['variants']['heikin_ashi']['events']
            assert result['candle_type'] == 'heikin_ashi'
            assert all(s.price == raw.loc[s.timestamp, 'close'] for s in result['signals'])
            item['scanner_matches_ha_reference'] = True
            short = get_s1_signals(raw.tail(900), symbol, tf,
                                  df_5min=native['5min'].tail(900), df_15min=native['15min'].tail(900))[0]
            event_identity = lambda values: [(e['bar_open'], e['side'], e['exit_bar_open']) for e in events(values)]
            item['full_and_900_bar_event_times_match'] = event_identity(result['signals']) == event_identity(short)
            assert item['full_and_900_bar_event_times_match']
            print(symbol, tf, 'standard', [e['bar_open'] for e in item['variants']['standard']['events']],
                  'HA', [e['bar_open'] for e in item['variants']['heikin_ashi']['events']])
            output['frames'].append(item)
    nifty = next(r for r in output['frames'] if r['symbol'] == 'NIFTY' and r['timeframe'] == '15min')
    assert [e['bar_open'] for e in nifty['variants']['standard']['events']] == ['2026-09-23 12:00:00']
    assert [e['bar_open'] for e in nifty['variants']['heikin_ashi']['events']] == ['2026-09-23 10:45:00']
    # Exercise the application's chart builder, not just the signal function.
    raw = read('NIFTY', '15min')
    result = AltSignalEngine(signal_source='s1', signal_mode='realtime').process(
        raw, 'NIFTY', '15min', df_5min=read('NIFTY', '5min'))
    host = ScannerChartMixin()
    host.mode, host.chart_stream_enabled = 'HISTORICAL', True
    host.simulation_id = 1
    host.active_chart_instrument, host.active_chart_tf = 'NIFTY', '15min'
    host._cached_results = {'NIFTY_15min': result}
    host._chart_metadata_cache = {}
    host.candles = SimpleNamespace(get_candles=lambda *args: raw)
    host.trades = SimpleNamespace(open_trades={})
    host.intel_snapshots = SimpleNamespace(history=lambda *args, **kwargs: [])
    host.mtf = SimpleNamespace(_result_cache={})
    payload = host._build_chart_from_cache('NIFTY')['15min']
    stamp = int(pd.Timestamp('2026-09-23 10:45', tz='Asia/Kolkata').timestamp())
    marker = [m for m in payload['markers'] if m['time'] == stamp and m['text'].startswith('BUY')]
    assert len(marker) == 1
    output['actual_chart_1045_marker'] = marker[0]
    output['actual_chart_candle_type'] = payload['candle_type']
    (ROOT / 'reports/s1_candle_source_verification_20260924.json').write_text(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
