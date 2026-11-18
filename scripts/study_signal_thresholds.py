"""Offline raw-scanner threshold study. No orders or production setting writes.

Outcomes are observed index moves, NOT option strategy profitability. All
thresholds are fitted before the later calendar partition and kept separate by
engine/index/timeframe/direction. Missing inputs never become zero indicators.
"""
import os, sys, json, socket, hashlib, bisect
from pathlib import Path
from collections import defaultdict
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'reports/threshold_study_20261104'
OUT.mkdir(parents=True,exist_ok=True)
WORK=OUT/'isolated';WORK.mkdir(exist_ok=True)
os.environ['UTBOT_DB_PATH']=str(WORK/'study.db')
os.environ['UTBOT_TEST_DB_PATH']=str(WORK/'study.db')
os.chdir(WORK);sys.path.insert(0,str(ROOT))
def blocked(*a,**k):raise RuntimeError('Offline study: network disabled')
socket.socket.connect=blocked;socket.create_connection=blocked
import numpy as np
import pandas as pd
from loguru import logger
logger.remove();logger.add(OUT/'engine.log',level='WARNING')
from config.settings import get_settings
from engine.engine_lab import build_shadow_engine, ENGINE_LAB_MODES, EngineLab

def save(name,value):
    (OUT/name).write_text(json.dumps(value,indent=2,default=str,allow_nan=False),encoding='utf-8')
def rma(s,n=14):
    out=[];seed=[];value=np.nan
    for x in s:
        if np.isfinite(x):
            if np.isnan(value):
                seed.append(x)
                if len(seed)==n:value=float(np.mean(seed))
            else:value=(value*(n-1)+x)/n
        out.append(value)
    return pd.Series(out,index=s.index)
def features(d):
    diff=d.close.diff();gain=rma(diff.clip(lower=0));loss=rma(-diff.clip(upper=0))
    rsi=100-100/(1+gain/loss);rsi=rsi.where(loss!=0,100).where((gain+loss)!=0,50)
    tr=pd.concat([d.high-d.low,(d.high-d.close.shift()).abs(),(d.low-d.close.shift()).abs()],axis=1).max(axis=1)
    up=d.high.diff();down=-d.low.diff();plus=rma(up.where((up>down)&(up>0),0));minus=rma(down.where((down>up)&(down>0),0))
    adx=rma(100*(plus-minus).abs()/(plus+minus))
    # Never compare futures contracts with cash/constituent share volumes.
    basis=d.get('volume_basis',pd.Series('',index=d.index)).fillna('')
    valid=basis.isin(['fyers_index','constituent_shares']) & (d.volume>0)
    v=d.volume.where(valid);vr=pd.Series(np.nan,index=d.index)
    for b in ['fyers_index','constituent_shares']:
        x=v.where(basis==b);prior=x.shift().rolling(20,min_periods=20).mean()
        vr.loc[basis==b]=(x/prior).loc[basis==b]
    return pd.DataFrame({'rsi':rsi,'adx':adx,'volume_ratio':vr},index=d.index)
def metric(rows):
    valid=[r for r in rows if r['outcome'] in ['REAL_MOVE','STOP_FIRST','NO_TARGET']]
    return {'n':len(valid),'real_moves':sum(r['outcome']=='REAL_MOVE' for r in valid),
            'real_move_pct':round(100*sum(r['outcome']=='REAL_MOVE' for r in valid)/len(valid),2) if valid else None,
            'incomplete':len(rows)-len(valid)}
def main():
    settings=get_settings();params=EngineLab._sanitize_params(settings.get_ut_engine_params())
    frames={};coverage={};hashes={}
    for inst in ['NIFTY','BANKNIFTY','SENSEX','MIDCPNIFTY']:
        frames[inst]={}
        for tf in ['1min','5min','15min']:
            p=ROOT/f'data_store/candles/{inst}_{tf}.csv';d=pd.read_csv(p,index_col=0,parse_dates=True).sort_index()
            if d.index.tz is not None:d.index=d.index.tz_convert('Asia/Kolkata').tz_localize(None)
            duplicates=int(d.index.duplicated().sum());d=d[~d.index.duplicated(keep='last')]
            d=d.loc[d.index<'2026-11-04'];frames[inst][tf]=d
            coverage[inst+'_'+tf]={'bars':len(d),'first':str(d.index.min()),'last':str(d.index.max()),'duplicates_removed':duplicates,'sessions':len(set(d.index.date))}
            hashes[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
    rows=[];errors=[]
    for mode in ENGINE_LAB_MODES:
        for inst,tfmap in frames.items():
            for tf,d in tfmap.items():
                if mode=='ut_ema' and tf=='1min':continue
                try:
                    engine=build_shadow_engine(mode,dict(params))
                    extra={}
                    if mode in ['ut_ema','ut_smart','ut_smart_s1','s1s2_dual']:
                        extra={'as_of':pd.Timestamp('2026-11-04')}
                    if mode in ['s1','quad_prime_s1','ut_smart_s1','s1s2_dual']:
                        extra.update({f'df_{dep}':v for dep,v in tfmap.items() if dep!=tf and dep in ['5min','15min']})
                    result=engine.process(df=d,instrument=inst,timeframe=tf,capital=100000,risk_pct=1,lots=1,lot_size=1,**extra)
                    if result.get('error'):raise RuntimeError(str(result['error']))
                    f=features(d);seen=set();period=pd.Timedelta(minutes=int(tf.replace('min','')))
                    for sig in result.get('signals',[]):
                        if mode=='ut_ema' and getattr(sig,'utema_action','')!='ENTRY':continue
                        side=str(sig.signal_type).upper()
                        if side not in ['BUY','SELL']:continue
                        t=pd.Timestamp(sig.timestamp)
                        if t.tzinfo:t=t.tz_convert('Asia/Kolkata').tz_localize(None)
                        if t<pd.Timestamp('2026-10-05') or t not in d.index or (t,side) in seen:continue
                        seen.add((t,side));i=d.index.get_loc(t)
                        if i<50 or i+1>=len(d):continue
                        future=d.iloc[i+1:i+7];outcome='INCOMPLETE';entry=float(future.open.iloc[0]);sign=1 if side=='BUY' else -1
                        # Signal confirmed at close; execute at next observed open.
                        # Gaps/session boundaries cannot be silently bridged.
                        expected=pd.date_range(t+period,periods=6,freq=period)
                        if len(future)==6 and future.index.equals(expected) and future.index[-1].date()==t.date():
                            outcome='NO_TARGET'
                            for _,bar in future.iterrows():
                                adverse=(entry-bar.low)/entry if sign==1 else (bar.high-entry)/entry
                                favorable=(bar.high-entry)/entry if sign==1 else (entry-bar.low)/entry
                                if adverse>=.002:outcome='STOP_FIRST';break
                                if favorable>=.004:outcome='REAL_MOVE';break
                        row={'engine':mode,'instrument':inst,'tf':tf,'direction':side,'bar':str(t),'available_at':str(t+period),'date':str(t.date()),'entry':entry,'outcome':outcome}
                        for name,value in f.iloc[i].items():row[name]=float(value) if np.isfinite(value) else None
                        row['pcr']=None;rows.append(row)
                except Exception as exc:
                    errors.append({'engine':mode,'instrument':inst,'tf':tf,'error':str(exc)[:300]})
                    print('ERROR',mode,inst,tf,str(exc)[:180],flush=True)
        print(mode,len(rows),'signals',len(errors),'errors',flush=True)
    save('signals_without_pcr.json',rows);save('coverage.json',coverage);save('errors.json',errors)
    # Archive only contemporaneous, complete chains. Never attach a future PCR.
    chains=defaultdict(list)
    for p in sorted((ROOT/'data_store/derivative_observations').glob('2026-*.jsonl')):
        if not '2026-10-05'<=p.stem<='2026-11-03':continue
        for raw in p.open('rb'):
            if b'"kind": "chain"' not in raw and b'"kind":"chain"' not in raw:continue
            e=json.loads(raw);q=e.get('quality')or{}
            if q.get('coverage_complete') is not True or q.get('exchange_clocks_fresh') is not True:continue
            rr=e.get('rows')or[];calls=sum(float(x.get('call_oi')or 0) for x in rr);puts=sum(float(x.get('put_oi')or 0) for x in rr)
            if calls<=0 or len(rr)<21:continue
            chains[(e['instrument'],p.stem)].append((float(e['at']),puts/calls))
        print('PCR archive',p.stem,flush=True)
    for key in chains:chains[key].sort()
    for r in rows:
        v=chains.get((r['instrument'],r['date']),[])
        clock=pd.Timestamp(r['available_at']).tz_localize('Asia/Kolkata').timestamp();pos=bisect.bisect_right(v,(clock,float('inf')))-1
        if pos>=0 and 0<=clock-v[pos][0]<=120:r['pcr']=v[pos][1]
    save('signals.json',rows)
    groups=defaultdict(list)
    for r in rows:groups['|'.join(r[k] for k in ['engine','instrument','tf','direction'])].append(r)
    results=[]
    for key,group in groups.items():
        train=[r for r in group if r['date']<'2026-10-25' and r['outcome']!='INCOMPLETE'];test=[r for r in group if r['date']>='2026-10-25' and r['outcome']!='INCOMPLETE']
        winners=[r for r in train if r['outcome']=='REAL_MOVE'];rules={}
        for feature in ['volume_ratio','rsi','adx','pcr']:
            values=[r[feature] for r in winners if r[feature] is not None]
            if len(values)<10:continue
            # Directly test the requested winning-average minimum. SELL RSI is
            # an upper bound; a universal rising RSI floor reverses its meaning.
            threshold=float(np.mean(values));upper=feature=='rsi' and key.endswith('|SELL')
            eligible=[r for r in test if r[feature] is not None]
            kept=[r for r in eligible if r[feature]<=threshold] if upper else [r for r in eligible if r[feature]>=threshold]
            rules[feature]={'threshold':threshold,'comparison':'<=' if upper else '>=','training_winners_with_feature':len(values),'later_available':metric(eligible),'later_kept':metric(kept),'later_missing':len(test)-len(eligible)}
        results.append({'segment':key,'training':metric(train),'later':metric(test),'rules':rules})
    save('thresholds.json',results)
    summary={'window':'2026-10-05 through 2026-11-03','calendar_days':30,'split':'2026-10-25','label':'Next 6 same-session bars: +0.4% before -0.2%; same-bar collision stop-first. Index diagnostic, not option P&L or native exit backtest.',
             'signals':len(rows),'outcomes':metric(rows),'coverage':{k:sum(r[k] is not None for r in rows) for k in ['volume_ratio','rsi','adx','pcr']},'segments':len(groups),'segments_with_testable_rules':sum(bool(r['rules']) for r in results),'errors':errors,'source_hashes':hashes,'live_thresholds_enabled':False}
    save('summary.json',summary);print(json.dumps({k:v for k,v in summary.items() if k not in ['source_hashes','errors']},indent=2))
if __name__=='__main__':main()
