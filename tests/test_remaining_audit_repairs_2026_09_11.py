from datetime import datetime, date
from types import SimpleNamespace
import threading
import time

import pandas as pd
import pytest

from scanner import Scanner
from data.market_data import MarketDataProvider
from data.candle_builder import CandleBuilder
from data.derivative_participation import DerivativeParticipation
from engine.multi_timeframe import MultiTimeframeEngine
from engine.expiry_manager import ExpiryManager
from intelligence.session_context import SessionContextAnalyzer
from intelligence.oi_tracker import OITracker
from intelligence.order_flow import OrderFlowAnalyzer
from intelligence.pcr_engine import PCREngine

INDICES = ['NIFTY','BANKNIFTY','SENSEX','MIDCPNIFTY']

def frame(n=80, freq='5min'):
    return pd.DataFrame({'open':100.,'high':101.,'low':99.,'close':100.,'volume':100.,
                         'volume_basis':'fyers_index'},
        index=pd.date_range('2026-09-11 09:15',periods=n,freq=freq))

@pytest.mark.parametrize('index',INDICES)
@pytest.mark.parametrize('tf',['1min','5min','15min'])
def test_real_regime_requires_source_clock(index,tf):
    s = Scanner.__new__(Scanner)
    s.mode = 'REAL'
    s.latest_regimes = {index:'TRENDING_UP'}
    s._timeframe_regimes = {f'{index}_{tf}':'TRENDING_UP'}
    s._intel_cache_time = {}
    t = SimpleNamespace(instrument=index,timeframe=tf)
    assert not s._trade_regime_is_own_timeframe(t)
    assert s._get_trade_regime(t) == 'UNKNOWN'
    s._intel_cache_time[f'{index}_{tf}'] = time.time()
    assert s._trade_regime_is_own_timeframe(t)
    s._timeframe_regimes = {}
    s._intel_cache_time = {f'{index}_5min':1.0}
    assert s._get_trade_regime(t) == 'UNKNOWN'

def test_stale_breadth_cannot_become_four_votes():
    s = Scanner.__new__(Scanner)
    s.mode='REAL'
    s.active_indices=INDICES
    s.latest_regimes={i:'TRENDING_UP' for i in INDICES}
    s._intel_cache_time={f'{i}_5min':1.0 for i in INDICES}
    s._cached_timeframe_intel=lambda *a,**k:None
    assert s._market_breadth_context()['aligned_count'] == 0

@pytest.mark.parametrize('old_basis,new_basis,ratio,confirmed',[
    ('TRADED','TICK_ACTIVITY',3,False),('TICK_ACTIVITY','TRADED',3,True),
    ('TRADED','TRADED',0,False),('TRADED','INDEX_TICK_ACTIVITY',3,False)])
def test_current_volume_is_one_observation(old_basis,new_basis,ratio,confirmed):
    s=Scanner.__new__(Scanner)
    s.mode='REAL'
    s._candidate_15m_trend_agrees=lambda _:False
    c=SimpleNamespace(instrument='NIFTY',timeframe='5min',inst_type='OPT',direction='LONG',
        confidence=.75,grade='A',adx_value=30,data_provenance='LIVE',volume_basis=old_basis,
        volume_ratio=2,volume_spike_entry=True,
        market_intelligence_context={'volume':{'available':True,'measurement_basis':new_basis,
                                               'relative_volume_ratio':ratio}})
    s._fresh_exact_trade_intel=lambda *a,**k:c.market_intelligence_context
    checks=s._quality_verdict_for_candidate(c,regime='TRENDING_UP').to_dict()['checks']
    assert checks['volume_confirmed'] is confirmed
    assert checks['volume_ratio'] == ratio
    assert checks['volume_basis'] == new_basis

def test_unavailable_aggregate_cannot_supply_signed_score():
    s=Scanner.__new__(Scanner)
    s.mode='REAL'
    assert s._trade_like_signed_intelligence(SimpleNamespace(),
        {'aggregate':{'available':False,'score_norm':-.8}}) == 0

@pytest.mark.parametrize('legacy',[True,False])
def test_wrong_expiry_overlay_abstains(legacy):
    p=MarketDataProvider.__new__(MarketDataProvider)
    p._ltp_cache={}
    def returned():
        f=pd.DataFrame([{'strike':25000,'call_oi':777,'put_oi':888}])
        f.attrs['expiry']=date(2026,9,15)
        return f
    def old(index,*,spot,strikes_each_side): return returned()
    def new(index,*,spot,strikes_each_side,expiry): return returned()
    p.kite=SimpleNamespace(is_connected=True,get_option_chain=old if legacy else new)
    f=pd.DataFrame([{'strike':25000,'call_oi':0,'put_oi':0}])
    result=p._augment_chain_oi_from_kite('NIFTY',f,expiry='22SEP2026')
    assert result.iloc[0].call_oi == 0

def test_futures_rate_is_unity_on_constant_activity():
    p=MarketDataProvider.__new__(MarketDataProvider)
    base=1788849000.
    for minute in range(7):
        for sec in (0,15,30,45):
            p._record_index_futures_volume_snapshot('NIFTY',{'tradingsymbol':'FUT',
                'volume':10000+minute*60+sec},now_ts=base+minute*60+sec)
    ctx=p.get_index_futures_minute_volume_context('NIFTY',now_ts=base+405)
    assert ctx['reliable']
    assert ctx['projected_ratio'] == 1
    assert ctx['coverage_seconds'] == 45
    assert ctx['unallocated_volume'] == 90
    assert not p.get_index_futures_minute_volume_context('NIFTY',now_ts=base+421)['reliable']

def test_candle_boundary_volume_is_conserved_without_fake_allocation():
    b=CandleBuilder()
    for ts,v in [('09:15:00',1000),('09:15:50',1100),('09:16:00',1200),('09:16:50',1300),('09:17:00',1400)]:
        b.update_from_tick('NIFTY',25000,v,datetime.fromisoformat('2026-09-11T'+ts),'fyers_index')
    rows=b.get_candles('NIFTY','1min')
    assert rows.volume.sum()+rows.volume_unallocated.sum() == 400
    assert not rows.volume_complete.any()

@pytest.mark.parametrize('column,value',[('volume',999),('open',98),('volume_basis','changed')])
def test_mtf_older_input_revisions_invalidate(column,value):
    f=frame()
    old=MultiTimeframeEngine._hash_df(f)
    f.loc[f.index[-30],column]=value
    assert MultiTimeframeEngine._hash_df(f) != old

def test_context_clips_full_history_to_requested_clock():
    s=Scanner.__new__(Scanner)
    f=frame(60)
    seen=[]
    s.session_context=SimpleNamespace(analyze=lambda df,**k:seen.append(df.index[-1]) or {})
    s.candles=SimpleNamespace(get_candles=lambda *a:f)
    s._vix_open_for_session=lambda *a:None
    s._hourly_anchor_regime=lambda *a,**k:{}
    s._market_breadth_context=lambda *a,**k:{}
    cutoff=f.index[10]
    s._build_intelligence_contexts('NIFTY',f.loc[:cutoff],as_of=cutoff.tz_localize('Asia/Kolkata'))
    assert seen == [cutoff - pd.Timedelta(minutes=5)]

@pytest.mark.parametrize('index',INDICES)
@pytest.mark.parametrize('tf,seconds',[('1min',60),('5min',300),('15min',900)])
def test_separate_derivative_intervals(index,tf,seconds):
    store=DerivativeParticipation()
    base=1788849000.
    for offset in range(0,seconds*2+1,15):
        for family in ('futures','options'):
            store.observe(index,family,'kite','F' if family=='futures' else 'CE',base+offset,
                10000+offset,oi=1000+offset,price=100+offset/100,
                clock='exchange' if family=='futures' else 'receipt')
    result=store.features(index,tf,base+seconds*2)
    for family in ('futures','options'):
        row=result[family][0]
        assert row['available']
        assert row['volume_change'] == seconds
        assert row['oi_change'] == seconds
        assert row['relative_volume_ratio'] == 1
        assert row['entry_eligible'] is (family=='futures')
    assert not store.features(index,tf,base+seconds*3)['futures'][0]['available']

def test_sparse_option_snapshots_do_not_fabricate_one_minute_features():
    s=DerivativeParticipation()
    for t in (0,90,180): s.observe('NIFTY','options','fyers','CE',1788849000+t,1000+t,clock='receipt')
    assert not s.features('NIFTY','1min',1788849180)['options'][0]['available']

def test_partial_prior_session_is_not_a_daily_range_baseline():
    f=frame(60)
    assert SessionContextAnalyzer._average_prior_day_range(f,date(2026,9,12)) == 0
    full=frame(75)
    assert SessionContextAnalyzer._average_prior_day_range(full,date(2026,9,12)) == 2

def test_day_change_cannot_vote_as_one_minute_oi():
    f=pd.DataFrame({'strike':[100,150,200],'call_oi':[100]*3,'put_oi':[100]*3,
                    'call_oi_change':[50]*3,'put_oi_change':[0]*3})
    f.attrs['oi_change_horizon_seconds']=86400
    result=OITracker().analyze(f,timeframe_seconds=60)
    assert result['signal']=='NEUTRAL'
    assert result['change_comparable'] is False

def test_pcr_provider_change_starts_a_separate_history():
    p=PCREngine()
    f=pd.DataFrame({'strike':[100,150,200],'call_oi':[100]*3,'put_oi':[150]*3,
                    'call_volume':[10]*3,'put_volume':[10]*3})
    f.attrs.update(expiry='22SEP2026',provider='fyers',observation_id='one')
    p.analyze(f,'NIFTY')
    f.attrs.update(provider='angelone',observation_id='two')
    assert len(p.analyze(f,'NIFTY')['pcr_history'])==1


def test_real_candidate_cannot_reuse_entry_context_without_fresh_intelligence():
    s=Scanner.__new__(Scanner)
    s.mode='REAL'
    s._fresh_exact_trade_intel=lambda *a,**k:None
    s._candidate_15m_trend_agrees=lambda _:False
    c=SimpleNamespace(instrument='NIFTY',timeframe='5min',volume_ratio=3,
        volume_spike_entry=True,market_intelligence_context={'volume':{'available':True,
        'measurement_basis':'TRADED','relative_volume_ratio':3}})
    assert s._candidate_market_intelligence_context(c) == {}
    checks=s._quality_verdict_for_candidate(c,regime='TRENDING_UP').to_dict()['checks']
    assert not checks['volume_confirmed']


def test_old_partial_volume_does_not_veto_current_measured_flow():
    f=frame(10)
    f['close']=100.8
    f['volume_complete']=True
    f.loc[f.index[0],'volume_complete']=False
    r=OrderFlowAnalyzer().analyze(f)
    assert r['available'] and r['signal']=='BULLISH'
    assert r['session_volume_complete'] is False
    assert r['buy_volume']+r['sell_volume'] in (899,900)


def test_partial_volume_cannot_veto_independent_depth():
    f=frame(10)
    f['volume_complete']=False
    r=OrderFlowAnalyzer().analyze(f,market_context={'depth':{'usable':True,
        'bid_qty':200,'ask_qty':100,'source':'kite_depth'}})
    assert r['available'] and r['signal']=='BULLISH' and r['source']=='kite_depth'


def test_old_volume_measurement_abstains_instead_of_recycling_a_surge():
    from intelligence.volume_profiler import VolumeProfiler
    f=frame(30)
    f.loc[f.index[-4],'volume']=500
    f.loc[f.index[-3:],'volume']=0
    r=VolumeProfiler().analyze(f,'NIFTY')
    assert not r['available'] and not r['is_surge']
    assert r['measurement_lag_bars']==3


@pytest.mark.parametrize('index',INDICES)
@pytest.mark.parametrize('tf',['1min','5min','15min'])
def test_aggregator_keeps_derivatives_separate_and_forwards_them_to_live_consumers(index,tf):
    from intelligence.intelligence_aggregator import IntelligenceAggregator
    a=IntelligenceAggregator()
    f=frame(40,freq=tf)
    seen=[]
    lane={'timeframe':tf,'futures':[{'contract':'FUT','available':False}],
          'options':[{'contract':'CE','available':False}]}
    a.derivative_source=lambda *args:seen.append(args) or lane
    result=a.analyze(instrument=index,timeframe=tf,candle_df=f)
    assert seen[0][:2]==(index,tf)
    assert result['derivative_participation']==lane
    s=Scanner.__new__(Scanner)
    s.mode='REAL'
    s._fresh_exact_trade_intel=lambda *a,**k:result
    row=SimpleNamespace(instrument=index,timeframe=tf)
    assert s._candidate_market_intelligence_context(row)['derivative_participation']==lane
    assert s._trade_like_market_intelligence_context(row)['derivative_participation']==lane
    assert f['volume'].eq(100).all()


@pytest.mark.parametrize('index',INDICES)
@pytest.mark.parametrize('tf,seconds',[('1min',60),('5min',300),('15min',900)])
def test_selected_option_ticks_provide_exchange_timed_intervals(index,tf,seconds):
    p=MarketDataProvider.__new__(MarketDataProvider)
    contract={'token':'123','symbol':index+'TESTCE','expiry':'22SEP2026'}
    assert p._register_derivative_contract(index,contract) is contract
    base=1788849000.
    for offset in range(0,seconds*2+1,15):
        p._record_option_participation_tick('123',{'exchange_timestamp':base+offset,
            'volume_traded':1000+offset,'oi':10000+offset,'last_price':100+offset/100},base+offset)
    row=p.get_derivative_participation(index,tf,base+seconds*2)['options'][0]
    assert row['available'] and row['entry_eligible']
    assert row['contract']==index+'TESTCE|22SEP2026|123'
    assert row['volume_change']==seconds and row['oi_change']==seconds
    assert row['relative_volume_ratio']==1


def test_missing_option_oi_does_not_interrupt_price_tracking():
    p=MarketDataProvider.__new__(MarketDataProvider)
    p._register_derivative_contract('NIFTY',{'token':'1','symbol':'CE','expiry':'22SEP2026'})
    p._record_option_participation_tick('1',{'exchange_timestamp':1788849000,
        'volume_traded':1000,'oi':None,'last_price':100},1788849000)
    assert p.get_derivative_participation('NIFTY','1min',1788849000)['options']==[]


def test_quote_updated_during_request_is_fresh(monkeypatch):
    import data.market_data as module
    now=[1788849000.]
    monkeypatch.setattr(module.time,'time',lambda:now[0])
    p=MarketDataProvider.__new__(MarketDataProvider)
    p._ws_cache={}
    p._quote_divergence={}
    p._volume_fallback_alerted={}
    def quote(index):
        now[0]+=1
        return {'available':True,'volume':1000,'oi':1000,'tradingsymbol':'FUT',
                'exchange_timestamp':now[0]}
    p.kite=SimpleNamespace(is_connected=True,index_futures_activity=quote)
    assert p.get_index_activity('NIFTY')['source']=='kite_index_futures'


@pytest.mark.parametrize('stamp',[None,1788848000.,1788850000.])
def test_fallback_quote_requires_current_exchange_clock(monkeypatch,stamp):
    import data.market_data as module
    monkeypatch.setattr(module.time,'time',lambda:1788849000.)
    p=MarketDataProvider.__new__(MarketDataProvider)
    p._ws_cache={}
    p._quote_divergence={}
    p._volume_fallback_alerted={}
    p.kite=SimpleNamespace(is_connected=True,index_futures_activity=lambda _:{
        'available':True,'volume':1000,'oi':1000,'tradingsymbol':'FUT','exchange_timestamp':stamp})
    r=p.get_index_activity('NIFTY')
    assert not r['futures']['available'] and r['source']=='none'


def test_cached_chain_rechecks_execution_expiry(monkeypatch):
    from engine.expiry_manager import expiry_manager
    monkeypatch.setattr(expiry_manager,'effective_expiry',lambda *a:'22SEP2026')
    p=MarketDataProvider.__new__(MarketDataProvider)
    f=pd.DataFrame({'strike':[100,150,200]})
    f.attrs['expiry']='15SEP2026'
    p._opt_chain_cache={'opt_chain_NIFTY':f}
    p._option_chain_meta={'NIFTY':{'source':'fyers','expiry_aligned':True,'fetched_at':time.time()}}
    assert p.get_cached_option_chain('NIFTY') is None
    s=Scanner.__new__(Scanner)
    s.data=p
    s._chain_cache={'NIFTY':f}
    s._last_chain_fetch={'NIFTY':time.time()}
    s._chain_quality_cache={'NIFTY':{'expiry_aligned':True,'intelligence_usable':True}}
    result,quality=s._get_option_chain_cached('NIFTY',{},allow_fetch=False)
    assert result.empty and not quality.get('intelligence_usable',False)
    f.attrs['expiry']='22SEP2026'
    assert p.get_cached_option_chain('NIFTY') is f


def test_session_clock_is_consistent_between_utc_and_ist():
    f=frame(12)
    f.index=f.index.tz_localize('Asia/Kolkata')
    a=SessionContextAnalyzer()
    ist=a.analyze(f)
    utc=a.analyze(f.tz_convert('UTC'))
    assert ist==utc


def test_derivative_counter_reset_and_provider_switch_require_new_baselines():
    s=DerivativeParticipation()
    for offset in range(0,121,15):
        s.observe('NIFTY','futures','kite','FUT',1788849000+offset,1000+offset)
    s.observe('NIFTY','futures','kite','FUT',1788849135,5)
    s.observe('NIFTY','futures','other','FUT',1788849135,5000)
    assert all(not row['available'] for row in s.features('NIFTY','1min',1788849135)['futures'])


def test_exit_confirmation_requires_new_observation_not_poll_time():
    s=Scanner.__new__(Scanner)
    t=SimpleNamespace()
    base=pd.Timestamp('2026-09-11 12:00')
    def poll(offset,identity):
        return s._exit_confirmation_ready(t,'_confirmation',('price','intel'),
            base+pd.Timedelta(seconds=offset),5,observation_id=identity)[0]
    assert not poll(0,'same')
    assert not poll(6,'same')
    assert not poll(10,'same')
    assert poll(11,'new')
    assert not poll(20,'a')
    assert not poll(22,'b')
    assert not poll(26,'b')
    assert poll(27,'c')


def test_unavailable_primitives_cannot_corroborate_or_establish_exit_reference():
    s=Scanner.__new__(Scanner)
    bad={'pcr':{'available':False,'primary_pcr':2},'oi':{'available':False,'signal':'BEARISH'},
         'order_flow':{'available':False,'ratio':.5},'volume':{'available':False,'signal':'BEARISH_VOLUME'},
         'regime':{'available':False,'regime':'TRENDING_DOWN'}}
    assert not s._intel_evidence_is_usable(bad)
    assert s._intel_reversal_corroboration('LONG',bad)[:2] == (0,0)


@pytest.mark.parametrize('basis',['TICK_ACTIVITY','INDEX_TICK_ACTIVITY','MIXED','MIXED_UNITS'])
def test_session_delta_cannot_vote_from_proxy_or_mixed_volume(basis):
    f=frame(10)
    f['close']=100.9
    f['volume_basis']=basis
    r=SessionContextAnalyzer()._session_cumulative_delta(f)
    assert r['source']=='unavailable' and r['bias']=='NEUTRAL'


def test_session_delta_excludes_partial_bars_and_rejects_incompatible_units():
    f=frame(10)
    f['close']=100.9
    f['volume_complete']=False
    a=SessionContextAnalyzer()
    assert a._session_cumulative_delta(f)['source']=='unavailable'
    f.loc[f.index[-5:],'volume_complete']=True
    r=a._session_cumulative_delta(f)
    assert r['source']=='volume' and r['bias']=='BULLISH' and not r['volume_complete']
    f.loc[f.index[-1],'volume_basis']='kite_contract_volume'
    assert a._session_cumulative_delta(f)['source']=='unavailable'


def test_consumed_tick_commit_cannot_earn_an_exit_confirmation():
    s=Scanner.__new__(Scanner)
    t=SimpleNamespace(symbol_token='123',_contract_tick_pending_token='123',
        _contract_tick_pending_cursor=10,_contract_tick_cursor_token='123',_contract_tick_cursor=9)
    base=pd.Timestamp('2026-09-11 12:00')
    before=Scanner._contract_observation_identity(t)
    assert before == ('123',10)
    assert not s._exit_confirmation_ready(t,'_confirmation',('price',),base,5,observation_id=before)[0]
    Scanner._commit_trade_contract_tick_path(t)
    after=Scanner._contract_observation_identity(t)
    assert after==before
    assert not s._exit_confirmation_ready(t,'_confirmation',('price',),base+pd.Timedelta(seconds=6),5,observation_id=after)[0]
    t._contract_tick_pending_token='123'
    t._contract_tick_pending_cursor=11
    assert s._exit_confirmation_ready(t,'_confirmation',('price',),base+pd.Timedelta(seconds=7),5,
        observation_id=Scanner._contract_observation_identity(t))[0]
    t.symbol_token='456'
    assert Scanner._contract_observation_identity(t)==('456',0)
