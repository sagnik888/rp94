"""Offline verification of remaining audit gaps; no broker/app startup or DB access.

Extract actual production methods; supply synthetic collaborators. These are diagnostic
observations, not acceptance tests that redefine incorrect behavior as correct.
"""
from pathlib import Path
import ast
import json
import runpy
import time
from datetime import datetime
from types import SimpleNamespace
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
broker = runpy.run_path(str(ROOT / 'reports/broker_volume_audit_20260911.py'))
extract = broker['extracted']
scanner_source = (ROOT / 'reports/scanner_volume_consumers_probe_20260911.py').read_text(encoding='utf-8')
scope = {'__file__': str(ROOT / 'reports/scanner_volume_consumers_probe_20260911.py')}
exec(compile(scanner_source.split('\nresults = {}')[0], 'offline_scanner_setup', 'exec'), scope)
S = scope['Scanner']
scope['ns']['time'] = time
scope['ns']['settings'] = SimpleNamespace(loss_guard_intel_max_age_seconds=90)
tree = ast.parse((ROOT / 'scanner.py').read_text(encoding='utf-8-sig'))
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Scanner')
extra = ['_get_trade_regime', '_trade_regime_is_own_timeframe', '_timeframe_intel_key',
         '_market_breadth_context', '_build_intelligence_contexts']
for n in cls.body:
    if isinstance(n, ast.FunctionDef) and n.name in extra:
        scope['compile_nodes']([ast.ClassDef(name='Extra', bases=[], keywords=[], body=[n], decorator_list=[])], 'scanner.py')
        setattr(S, n.name, scope['ns']['Extra'].__dict__[n.name])

out = {}
P = extract('data/market_data.py', 'MarketDataProvider', {
    '_augment_chain_oi_from_kite', '_chain_oi_is_usable',
    '_record_index_futures_volume_snapshot', 'get_index_futures_minute_volume_context',
})
p = P()
p._KITE_OI_OVERLAY_MIN_INTERVAL = 60
p._ltp_cache = {}
calls = []
def legacy_chain(index, *, spot, strikes_each_side):
    calls.append(index)
    frame = pd.DataFrame([{'strike': 25000, 'call_oi': 777, 'put_oi': 888}])
    frame.attrs['expiry'] = '15SEP2026'
    return frame
p.kite = SimpleNamespace(is_connected=True, get_option_chain=legacy_chain)
chain = pd.DataFrame([{'strike': 25000, 'call_oi': 0, 'put_oi': 0,
                       'call_symbol': 'NEXT_EXPIRY_CE', 'put_symbol': 'NEXT_EXPIRY_PE'}])
chain.attrs['expiry'] = '22SEP2026'
result = p._augment_chain_oi_from_kite('NIFTY', chain, expiry='22SEP2026')
out['wrong_expiry_legacy_overlay'] = {'requested_expiry': '22SEP2026', 'returned_expiry': '15SEP2026',
    'merged_call_oi': float(result.iloc[0].call_oi), 'legacy_called': bool(calls)}

s = S()
s.mode = 'REAL'
s._timeframe_regimes = {'NIFTY_5min': 'TRENDING_UP'}
s.latest_regimes = {'NIFTY': 'TRENDING_UP'}
s._intel_cache_time = {'NIFTY_5min': 1.0}
t = SimpleNamespace(instrument='NIFTY', timeframe='15min')
out['borrowed_regime_skips_source_age'] = {'source_epoch': 1.0, 'resolved': s._get_trade_regime(t)}
s._intel_cache_time = {}
t.timeframe = '5min'
out['missing_regime_timestamp_live'] = {'own_timeframe': s._trade_regime_is_own_timeframe(t),
    'resolved': s._get_trade_regime(t)}
s.active_indices = ['NIFTY', 'BANKNIFTY', 'SENSEX', 'MIDCPNIFTY']
s.latest_regimes = {name: 'TRENDING_UP' for name in s.active_indices}
s._intel_cache_time = {name+'_5min': 1.0 for name in s.active_indices}
s._cached_timeframe_intel = lambda *a, **k: None
out['breadth_stale_legacy_regimes'] = s._market_breadth_context()
s._candidate_15m_trend_agrees = lambda _: False
def quality(candidate_basis, current_basis, ratio=3.0):
    c = SimpleNamespace(instrument='NIFTY', timeframe='5min', inst_type='OPT', direction='LONG',
        confidence=.75, grade='A', adx_value=30, data_provenance='LIVE',
        volume_basis=candidate_basis, volume_ratio=2.0,
        market_intelligence_context={'volume': {'available': True, 'measurement_basis': current_basis,
                                             'relative_volume_ratio': ratio}})
    v = s._quality_verdict_for_candidate(c, regime='TRENDING_UP').to_dict()
    return {k: v['checks'][k] for k in ('volume_ratio', 'volume_basis', 'volume_available', 'volume_confirmed')}
out['quality_current_proxy_with_old_traded_basis'] = quality('TRADED', 'TICK_ACTIVITY')
out['quality_current_traded_with_old_proxy_basis'] = quality('TICK_ACTIVITY', 'TRADED')
out['quality_current_zero_uses_old_ratio'] = quality('TRADED', 'TRADED', 0.0)
out['unavailable_aggregate_signed_score'] = s._trade_like_signed_intelligence(t,
    {'aggregate': {'available': False, 'score_norm': -.8}})

p = P()
base = 1788849000.0
for minute in range(7):
    for second in (0, 15, 30, 45):
        p._record_index_futures_volume_snapshot('NIFTY',
            {'tradingsymbol': 'NIFTYFUT', 'volume': 10000 + minute * 60 + second},
            now_ts=base + minute * 60 + second)
ctx = p.get_index_futures_minute_volume_context('NIFTY', now_ts=base+6*60+45)
out['futures_partial_bucket_called_reliable'] = {'true_full_minute_volume': 60,
    **{k:ctx[k] for k in ('baseline_volume', 'current_volume', 'projected_ratio', 'reliable', 'unit')}}
out['fixed_no_clock_only_rollover_spike'] = p.get_index_futures_minute_volume_context('NIFTY',
    now_ts=base+7*60+1)['reliable'] is False

# CandleBuilder has only local dataframe/logging dependencies.
candle_ns = runpy.run_path(str(ROOT / 'data/candle_builder.py'))
b = candle_ns['CandleBuilder']()
for stamp, cumulative in [('09:15:00',1000),('09:15:50',1100),('09:16:00',1200),
                          ('09:16:50',1300),('09:17:00',1400)]:
    b.update_from_tick('NIFTY',25000,cumulative,datetime.fromisoformat('2026-09-11T'+stamp),'fyers_index')
out['candle_boundary_quantity_loss'] = {'total_increase': 400,
    'stored_sum': float(b.get_candles('NIFTY','1min').volume.sum())}
M = extract('engine/multi_timeframe.py', 'MultiTimeframeEngine', {'_hash_df'})
frame = pd.DataFrame({'open':100.,'high':101.,'low':99.,'close':100.,'volume':100.},
                     index=pd.date_range('2026-09-11 09:15',periods=80,freq='min'))
h = M._hash_df(frame)
frame.iloc[-30, frame.columns.get_loc('volume')] = 999
out['mtf_older_correction_missed'] = M._hash_df(frame) == h

captured = {}
def analyze_context(df, **kwargs):
    captured['latest_consumed'] = str(df.index[-1])
    return {}
s.session_context = SimpleNamespace(analyze=analyze_context)
session_frame = frame.iloc[:60].copy()
session_frame.index = pd.date_range('2026-09-11 09:15',periods=60,freq='5min')
s.candles = SimpleNamespace(get_candles=lambda *args: session_frame)
s._vix_open_for_session = lambda *args: None
s._hourly_anchor_regime = lambda *args, **kwargs: {}
s._market_breadth_context = lambda *args, **kwargs: {}
cutoff = session_frame.index[10]
s._build_intelligence_contexts('NIFTY',session_frame.loc[:cutoff],as_of=cutoff)
out['session_full_cache_ignores_asof'] = {'requested_cutoff':str(cutoff),**captured}

import hashlib
out['source_sha256'] = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
    'scanner.py','data/market_data.py','data/candle_builder.py','engine/multi_timeframe.py',
    'intelligence/pcr_engine.py','intelligence/oi_tracker.py','engine/quality_verdict.py')}

target = ROOT / 'reports/postfix_verification_20260911.json'
target.write_text(json.dumps(out,indent=2,default=str),encoding='utf-8')
print(json.dumps(out,indent=2,default=str))
