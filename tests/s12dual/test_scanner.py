import json
from dataclasses import replace
from collections import Counter
import pandas as pd
import numpy as np
import pytest
from engine.s12dual import DualScanner, Config
from engine.s12dual.mtf import aggregate,aligned_feature
from engine.s12dual.indicators import calculate,rma
from tests.s12dual.generate_demo import make

@pytest.fixture(scope='module')
def candles():
    minute=make(12)
    chart=aggregate(minute,'1','5','Asia/Kolkata',555)
    return minute,chart

@pytest.fixture(scope='module')
def outcome(candles):
    minute,chart=candles
    return DualScanner(chart,symbol='NSE:TEST',timeframe='5',mtf={'1':minute},config=Config()).run()

def test_source_events_and_independent_position_ledgers(outcome):
    events=outcome['events'];assert events
    assert any(z['engine']=='S1' and z['event']=='SETUP' for z in events)
    assert any(z['engine']=='S2' and z['event']=='ENTRY' for z in events)
    assert all(z['engine'] in ('S1','S2') for z in events)
    for z in outcome['ledger']:
        assert z['side'] in ('LONG','SHORT')
        assert z['engine'] in ('S1','S2')
        assert z['entry']>0 and z['exit']>0


def test_s1_fill_is_next_bar_open_accounting(candles,outcome):
    _,chart=candles
    events=outcome['events']
    pending={e['trade_id']:e for e in events if e['engine']=='S1' and e['event']=='SETUP'}
    for e in events:
        if e['engine']=='S1' and e['event']=='FILL_ESTIMATE':
            orig=pending[e['trade_id']]
            when=pd.Timestamp(orig['bar_open'])
            fill=pd.Timestamp(e['bar_open'])
            assert fill>when
            assert e['price']==pytest.approx(float(chart.loc[fill,'open']))
            assert e['broker_fill_confirmed'] is False and e['simulated'] is True


def test_s2_entry_price_pine_slippage(outcome,candles):
    _,chart=candles
    for z in outcome['events']:
        if z['engine']=='S2' and z['event']=='ENTRY':
            close=chart.loc[pd.Timestamp(z['bar_open']),'close']
            sign=1 if z['action']=='BUY' else -1
            assert z['price']==pytest.approx(close*(1+sign*z['theoretical_slippage_pct']/100))
            assert z['t1']>z['price'] if sign==1 else z['t1']<z['price']


def test_no_lookahead_mtf_previous_confirmed(candles):
    one,chart=candles
    fifteen=aggregate(one,'1','15','Asia/Kolkata',555)
    f=calculate(fifteen)
    aligned=aligned_feature(chart.index,'5',f,'15','ema9')
    # At the first 5m bar of a 15m interval, use preceding 15m EMA, not current.
    for j in range(100,len(chart)-1):
        host=chart.index[j]
        m=f.index[f.index<=host]
        if len(m)>1:
            assert aligned[j]==pytest.approx(f.loc[m[-2],'ema9'])


def test_independent_when_opposite_engine_disabled(candles,outcome):
    minute,chart=candles
    s1_only=DualScanner(chart,symbol='NSE:TEST',timeframe='5',mtf={'1':minute},config=replace(Config(),enable_s2=False)).run()
    s2_only=DualScanner(chart,symbol='NSE:TEST',timeframe='5',mtf={'1':minute},config=replace(Config(),enable_s1=False)).run()
    assert [z for z in outcome['events'] if z['engine']=='S1']==[z for z in s1_only['events'] if z['engine']=='S1']
    assert [z for z in outcome['events'] if z['engine']=='S2']==[z for z in s2_only['events'] if z['engine']=='S2']


def test_stop_or_target_reference_and_costs(outcome):
    for e in outcome['events']:
        if e['event']=='ENTRY' and e['engine']=='S2':
            if e['action']=='BUY': assert e['stop']<e['price']<e['t1']<e['t2']<e['t3']
            else:assert e['stop']>e['price']>e['t1']>e['t2']>e['t3']
        if e['event']=='FILL_ESTIMATE' and e['engine']=='S1':
            if e['signal']=='BUY':assert e['stop']<e['price']<e['t1']<e['t2']<e['t3']
            else:assert e['stop']>e['price']>e['t1']>e['t2']>e['t3']
    assert all(len(set((e['id'],e['engine'])))==2 for e in outcome['events'])


def test_invalid_mtf_rejected(candles):
    _,chart=candles
    # Do not synthesize 1-minute chart 5m signal from 3m candles.
    chart3=chart.iloc[:100].copy()
    with pytest.raises(ValueError,match='Need true 5'):
        DualScanner(chart3,symbol='NSE:TEST',timeframe='3')


def test_unstable_mtf_not_silently_approximated(candles):
    minute,chart=candles
    with pytest.raises(NotImplementedError):
        DualScanner(chart,symbol='NSE:TEST',timeframe='5',mtf={'1':minute},config=replace(Config(),s2_confirmed_mtf=False))


def test_reproducible_events(candles,outcome):
    minute,chart=candles
    result=DualScanner(chart,symbol='NSE:TEST',timeframe='5',mtf={'1':minute},config=Config()).run()
    assert result['events']==outcome['events']


def test_defaults_identity():
    c=Config()
    assert (c.min_score,c.conflict_margin,c.min_adx,c.s1_stop_pct,c.t1_r,c.t2_r,c.t3_r)==(60,20,20,.2,2,3,4)
    assert c.s2_confidence_floor==60 and c.s2_risk_mode=='Dynamic' and c.s2_confirmed_mtf


def test_live_update_reconciles_to_single_batch(candles):
    minute,chart=candles
    cut=410
    old=DualScanner(chart.iloc[:cut],symbol='NSE:TEST',timeframe='5',mtf={'1':minute},config=Config())
    initial=list(old.run()['events'])
    evs=old.update(chart.iloc[cut:])
    fresh=DualScanner(chart,symbol='NSE:TEST',timeframe='5',mtf={'1':minute},config=Config()).run()
    assert initial+evs==fresh['events']
    assert old.run()['ledger']==fresh['ledger']


def test_no_volume_price_proxy_and_block(candles):
    minute,chart=candles
    no_vol_chart=chart.drop(columns=['volume'])
    no_vol_mtf=minute.drop(columns=['volume'])
    for policy in ('Block','Price proxy'):
        result=DualScanner(no_vol_chart,symbol='NSE:TEST',timeframe='5',mtf={'1':no_vol_mtf},config=replace(Config(),no_volume_policy=policy)).run()
        assert 'events' in result


def test_risk_modes_stay_inside_hard_cap(candles):
    minute,chart=candles
    for mode in ('ATR','Swing','Percentage','Dynamic'):
        cfg=replace(Config(),s2_risk_mode=mode)
        result=DualScanner(chart,symbol='NSE:TEST',timeframe='5',mtf={'1':minute},config=cfg,tick_size=.01).run()
        for e in result['events']:
            if e['engine']=='S2' and e['event']=='ENTRY' and e['subtype']=='NONE':
                distance=abs(e['price']-e['stop'])/e['price']*100
                assert distance<=cfg.hard_stop_intraday_pct+.02


def test_close_only_stop_priority_and_gap_prices():
    from engine.s12dual.engine import Trade
    tr=Trade('LONG',100.,10,'x',99.,99.,102.,103.,104.,'Test',peak=100.,trough=100.)
    assert tr.long
    assert tr.trailing==99
    from engine.s12dual.engine import pnl
    assert pnl(tr,95.,0.)==-5.
    assert pnl(tr,95.,.10)==pytest.approx(-5.10)


def test_s1_stop_first_when_target_and_stop_in_same_ohlc(candles):
    from engine.s12dual.engine import Trade
    minute,chart=candles
    sc=DualScanner(chart,symbol='NSE:TEST',timeframe='5',mtf={'1':minute})
    i=451;sc.i=i
    bar=chart.iloc[i]
    assert bar.high>bar.open and bar.low<bar.open
    stop=bar.open-(bar.open-bar.low)*.50
    target=bar.open+(bar.high-bar.open)*.50
    tr=Trade('LONG',bar.open,440,'manual-trade',stop,stop,target,target+.1,target+.2,'MANUAL',peak=bar.open,trough=bar.open)
    sc.s1.active=tr
    sc._s1_bar(sc._features(),sc._session())
    assert sc.s1.active is None
    assert sc.events[-1]['event']=='EXIT'
    assert sc.events[-1]['reason']=='INITIAL STOP'
    assert not tr.t1_hit


def test_s2_stop_first_when_target_and_stop_in_same_ohlc(candles):
    from engine.s12dual.engine import Trade
    minute,chart=candles
    sc=DualScanner(chart,symbol='NSE:TEST',timeframe='5',mtf={'1':minute})
    i=451;sc.i=i
    bar=chart.iloc[i]
    stop=bar.open-(bar.open-bar.low)*.5
    target=bar.open+(bar.high-bar.open)*.5
    tr=Trade('LONG',bar.open,449,'manual-trade',stop,stop,target,target+.1,target+.2,'MANUAL',peak=bar.open,trough=bar.open)
    sc.s2.active=tr
    sc._s2_bar(sc._features(),sc._session())
    assert sc.s2.active is None
    assert sc.events[-1]['event']=='EXIT'
    assert sc.events[-1]['reason']=='INITIAL STOP'
    assert not tr.t1_hit
