"""Offline evidence only: replay local candles; never connect to broker or place orders."""
from pathlib import Path
from dataclasses import asdict
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd
from engine.s1_engine import compute_features, simulate, params_from_settings


def main():
    baseline = json.loads((ROOT / 'reports/s1_data_evidence_20260923.json').read_text())
    results = []
    for reference in baseline['frames']:
        symbol, tf = reference['symbol'], reference['timeframe']
        path = ROOT / reference['file']
        df = pd.read_csv(path, index_col='timestamp', parse_dates=True)
        lower = pd.read_csv(ROOT / f'data_store/candles/{symbol}_5min.csv',
                            index_col='timestamp', parse_dates=True)
        params = params_from_settings(instrument=symbol)
        runs = []
        for size in ('full', '900'):
            frame = df if size == 'full' else df.tail(900)
            five = lower if size == 'full' else lower.tail(900)
            features = compute_features(frame, tf, params, df_5min=five)
            run = simulate(frame, tf, params, features=features)
            trades = [{
                'entry_time': str(frame.index[t.entry_bar]),
                'direction': t.direction,
                'chart_entry': t.entry_price,
                'signal_close': t.signal_close,
                'exit_time': str(frame.index[t.exit_bar]) if t.exit_bar >= 0 else None,
                'exit_reason': t.exit_reason,
            } for t in run.trades if frame.index[t.entry_bar].date() == frame.index[-1].date()]
            runs.append({'size': size, 'final_position': int(run.positions[-1]),
                         'features': {k: float(features[k][-1]) for k in (
                             'close', 'rsi', 'rsi5m', 'adx', 'di_plus', 'di_minus',
                             'bull_score', 'bear_score')}, 'today_trades': trades})
        results.append({'symbol': symbol, 'timeframe': tf, 'last': str(df.index[-1]),
                        'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'settings': asdict(params), 'screenshot': reference['screenshot'],
                        'runs': runs,
                        'same_today_trades_full_vs_900': runs[0]['today_trades'] == runs[1]['today_trades']})
    output = {'engine_sha256': hashlib.sha256((ROOT / 'engine/s1_engine.py').read_bytes()).hexdigest(),
              'notes': ['Local FYERS data differs from TradingView screenshots.',
                        'Replay includes all supplied candles, distinct from confirmed mode which excludes the last candle.',
                        'This checks local consistency, not TradingView parity or execution returns.'],
              'frames': results}
    (ROOT / 'reports/s1_parity_replay_20260923.json').write_text(json.dumps(output, indent=2))
    for row in results:
        print(row['symbol'], row['timeframe'], 'position', row['runs'][0]['final_position'],
              'same_today_trades_full_vs_900', row['same_today_trades_full_vs_900'])


if __name__ == '__main__':
    main()
