"""Read-only production-method probes of derivative decision connectivity.

No provider initialization, network calls, orders, or production state writes.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from data.derivative_participation import DerivativeParticipation
from data.market_data import MarketDataProvider
from intelligence.intelligence_aggregator import IntelligenceAggregator
from intelligence.oi_tracker import OITracker
from scanner import Scanner


def lane(tf, sign):
    return {
        'timeframe': tf,
        **{family: [{'contract': family + '_TEST', 'provider': 'kite',
                  'available': True, 'entry_eligible': True, 'clock': 'exchange',
                  'volume_change': 10000 if sign > 0 else 100,
                  'oi_change': sign * 10000, 'price_change': sign * 100,
                  'relative_volume_ratio': 10 if sign > 0 else 0.1}]
        for family in ('futures', 'options')}
    }


def main():
    rows = []
    for index in ('NIFTY', 'BANKNIFTY', 'MIDCPNIFTY', 'SENSEX'):
        for tf in ('1min', '5min', '15min'):
            bars = pd.DataFrame({'open': 100., 'high': 101., 'low': 99.,
                                 'close': 100., 'volume': 100., 'volume_basis': 'fyers_index'},
                                index=pd.date_range('2026-09-11 09:15', periods=70, freq=tf))
            outputs = []
            quality = []
            for sign in (1, -1):
                a = IntelligenceAggregator()
                a.derivative_source = lambda *args, sign=sign: lane(tf, sign)
                result = a.analyze(instrument=index, timeframe=tf, candle_df=bars)
                result.pop('derivative_participation')
                outputs.append(result)
                scanner = Scanner.__new__(Scanner)
                scanner.mode = 'REAL'
                scanner._candidate_15m_trend_agrees = lambda _: False
                candidate = SimpleNamespace(
                    instrument=index, timeframe=tf, inst_type='OPT', direction='LONG',
                    confidence=.75, grade='A', adx_value=30, data_provenance='LIVE',
                    volume_basis='TRADED', volume_ratio=2, volume_spike_entry=True)
                context = deepcopy(result)
                context['derivative_participation'] = lane(tf, sign)
                scanner._fresh_exact_trade_intel = lambda *a, **k: context
                quality.append(scanner._quality_verdict_for_candidate(candidate, regime='TRENDING_UP').to_dict())
            # String normalization accommodates NumPy scalar values without mutating outputs.
            same_aggregate = outputs[0]['aggregate'] == outputs[1]['aggregate']
            rows.append({'index': index, 'timeframe': tf, 'aggregate_unchanged': same_aggregate,
                         'regime_unchanged': outputs[0]['regime'] == outputs[1]['regime'],
                         'entry_quality_unchanged': quality[0] == quality[1]})

    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._load_option_oi_snapshots = lambda: getattr(provider, '_option_oi_snapshots', {})
    provider._save_option_oi_snapshots = lambda: None
    chain = pd.DataFrame([{'strike': s, 'call_oi': 10000, 'put_oi': 10000,
                           'call_oi_change': 100, 'put_oi_change': 500,
                           'call_volume': 10000, 'put_volume': 10000}
                          for s in (24950, 25000, 25050)])
    base = 1788849000.
    with patch('data.market_data.time.time', return_value=base):
        provider._apply_option_oi_snapshot_deltas('NIFTY', chain, 'fyers', '22SEP2026')
    chain['put_oi'] += 1000
    with patch('data.market_data.time.time', return_value=base+90):
        latest = provider._apply_option_oi_snapshot_deltas('NIFTY', chain, 'fyers', '22SEP2026')
    oi = {tf: OITracker().analyze(latest, spot_price=25000, timeframe_seconds=seconds)
          for tf, seconds in [('1min', 60), ('5min', 300), ('15min', 900)]}

    store = DerivativeParticipation()
    for seconds in range(0, 1801, 15):
        store.observe('NIFTY', 'futures', 'kite', 'FUT', base+seconds, 1000+seconds,
                      oi=5000+seconds, price=25000+seconds/100)
    historical = store.features('NIFTY', '5min', base-86400)
    result = {'derivative_counterfactuals': rows,
              'fyers_reported_oi_after_real_90_second_change': {
                  'source': latest.attrs['oi_change_source'],
                  'horizon_seconds': latest.attrs['oi_change_horizon_seconds'],
                  'results': {tf: {'signal': r['signal'], 'change_comparable': r['change_comparable']}
                              for tf, r in oi.items()}},
              'historical_query_with_current_only_history': historical}
    output = ROOT / 'reports/verification_derivative_paths_20260913.json'
    output.write_text(json.dumps(result, indent=2, default=str), encoding='utf-8')
    print(json.dumps(result, indent=2, default=str))


if __name__ == '__main__':
    main()
