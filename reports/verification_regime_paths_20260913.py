"""Read-only production-method probes; no broker I/O or scanner initialization."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
import pandas as pd
from loguru import logger
from intelligence.session_context import SessionContextAnalyzer

ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / 'scanner.py').read_text(encoding='utf-8-sig'))
owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Scanner')
names = {'_build_intelligence_contexts','_get_historical_vix','_hourly_anchor_regime',
         '_compute_historical_intel_at'}
selected = [n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name in names]
module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
    ast.ClassDef(name='ProbeScanner', bases=[], keywords=[], body=selected, decorator_list=[])], type_ignores=[])
ns = {'pd':pd, 'logger':logger, 'SessionContextAnalyzer':SessionContextAnalyzer,
      'aggregate_intelligence_score':lambda agg:agg.get('score_norm',0)}
exec(compile(ast.fix_missing_locations(module), str(ROOT/'scanner.py'), 'exec'),ns)
Scanner=ns['ProbeScanner']

def make(frame):
    s=Scanner()
    s.candles=SimpleNamespace(get_candles=lambda *a:frame)
    s.session_context=SessionContextAnalyzer()
    s._vix_open_for_session=lambda *a:None
    s._market_breadth_context=lambda **k:{}
    s._detect_regime_warmed=lambda f,*a:{'last_seen':str(f.index[-1])}
    return s

frame=pd.DataFrame({'open':100.,'high':101.,'low':99.,'close':100.,'volume':100.,'volume_basis':'fyers_index'},
    index=pd.date_range('2026-09-11 09:15',periods=75,freq='5min'))
cutoff=pd.Timestamp('2026-09-11 10:02')
base=make(frame)._build_intelligence_contexts('NIFTY',frame,as_of=cutoff)['session_context']
changed=frame.copy()
changed.loc[pd.Timestamp('2026-09-11 10:00'),'high']=150.
result=make(changed)._build_intelligence_contexts('NIFTY',changed,as_of=cutoff)['session_context']
findings={'partial_higher_timeframe_lookahead':{
    'as_of':str(cutoff), 'mutated_bar':'10:00-10:05',
    'before_session_high':base['session_high'], 'after_session_high':result['session_high'],
    'before_range_pct':base['session_range_pct'], 'after_range_pct':result['session_range_pct']}}

s=Scanner()
s._vix_data={'2026-09-11 15:30:00':28.0}
findings['vix_future_observation']={'as_of':'2026-09-11 09:20',
    'sole_observation':'2026-09-11 15:30:00',
    'returned':s._get_historical_vix(pd.Timestamp('2026-09-11 09:20'))}
s=Scanner()
s._vix_data={'2026-09-11 00:00:00':27.0}
findings['vix_same_day_close_at_midnight']={'as_of':'2026-09-11 09:20',
    'returned':s._get_historical_vix(pd.Timestamp('2026-09-11 09:20'))}

frames=[]
for day in pd.bdate_range('2026-09-01','2026-09-11'):
    f=frame.copy(); f.index=pd.date_range(day+pd.Timedelta(hours=9,minutes=15),periods=75,freq='5min'); frames.append(f)
long=pd.concat(frames)
findings['anchor_timezone_cutoff_fail_open']={
    'naive':make(long)._hourly_anchor_regime('NIFTY',as_of=pd.Timestamp('2026-09-08 10:02')),
    'aware':make(long)._hourly_anchor_regime('NIFTY',as_of=pd.Timestamp('2026-09-08 10:02',tz='Asia/Kolkata'))}

print(json.dumps(findings,indent=2))
(ROOT/'reports/verification_regime_paths_20260913.json').write_text(json.dumps(findings,indent=2),encoding='utf-8')
