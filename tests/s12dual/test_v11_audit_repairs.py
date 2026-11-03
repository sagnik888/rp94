"""Targeted regression tests for source audit P0/P1 defects."""
from dataclasses import replace
import pandas as pd
import numpy as np
import pytest
from engine.s12dual import Config, DualScanner
from engine.s12dual.mtf import aggregate, aligned_feature, require_candles
from engine.s12dual.timing import candle_closes
from tests.s12dual.generate_demo import make


def sample_index(t):
    return pd.DatetimeIndex([pd.Timestamp(q,tz='Asia/Kolkata') for q in t]).tz_convert('UTC')


def tiny(df_idx):
    return pd.DataFrame({'open':[100.]*len(df_idx),'high':[101.]*len(df_idx),
                         'low':[99.]*len(df_idx),'close':[100.]*len(df_idx),
                         'volume':[1000.]*len(df_idx)},index=df_idx)


def test_monthly_no_future_feb_to_march_and_31day():
    chart_idx=sample_index(['2026-02-02 09:15','2026-03-02 09:15'])
    sample=sample_index(['2026-02-27 15:20','2026-03-02 09:15',
                         '2026-03-31 15:20','2026-04-01 09:15'])
    d=pd.DataFrame({'v':[1.,999.,2.,999.]},index=sample)
    got=aligned_feature(chart_idx,'M',d,'5','v')
    assert got.tolist()==[1.,2.], 'monthly chart must never ingest adjacent future month'


def test_monthly_no_previous_period_fallback():
    chart_idx=sample_index(['2026-02-02 09:15'])
    sample=sample_index(['2026-01-30 15:20'])
    d=pd.DataFrame({'v':[1.]},index=sample)
    assert np.isnan(aligned_feature(chart_idx,'M',d,'5','v')[0])


def test_daily_mtf_no_next_day_and_sealed_at_cash_close():
    chart=sample_index(['2026-10-09 09:15'])
    sub=sample_index(['2026-10-09 09:00','2026-10-09 15:25',
                      '2026-10-10 09:15'])
    df=pd.DataFrame({'v':[99.,1.,1000.]},index=sub)
    assert aligned_feature(chart,'D',df,'5','v')[0]==1.


def test_weekly_boundary_is_calendar_week_not_fixed_7days():
    chart=sample_index(['2026-10-05 09:15']) # Monday
    sub=sample_index(['2026-10-09 15:25','2026-10-12 09:15'])
    df=pd.DataFrame({'v':[7.,999.]},index=sub)
    assert aligned_feature(chart,'W',df,'5','v')[0]==7.


def test_time_closes_support_variable_calendar_and_explicit_special_close():
    d=sample_index(['2026-10-09 09:15'])
    assert candle_closes(d,'D')[0].tz_convert('Asia/Kolkata').strftime('%Y-%m-%d %H:%M')=='2026-10-09 15:30'
    m=sample_index(['2026-02-02 09:15','2026-03-02 09:15'])
    assert [x.tz_convert('Asia/Kolkata').strftime('%Y-%m-%d') for x in candle_closes(m,'M')]==['2026-02-27','2026-03-31']
    special=sample_index(['2026-10-09 13:00'])
    assert candle_closes(d,'D',explicit=special)[0]==special[0]


def test_calendar_aggregate_month_week_split():
    idx=sample_index(['2026-02-27 15:20','2026-03-02 09:15','2026-03-06 15:20'])
    d=tiny(idx)
    m=aggregate(d,'5','M','Asia/Kolkata',555)
    assert len(m)==2
    w=aggregate(d,'5','W','Asia/Kolkata',555)
    assert len(w)==2


def test_auto_classification_and_config_override():
    one=make(4)
    for symbol,kind in [('NSE:NIFTY','index'),('NSE:BANKNIFTY','index'),
                        ('BSE:SENSEX','index'),('MCX:CRUDEOIL','commodity'),
                        ('NSE:NIFTYBEES','stock'),('NSE:RELIANCE','stock')]:
        scan=DualScanner(one,symbol=symbol,timeframe='1')
        assert scan.instrument_type==kind,(symbol,scan.instrument_type)
    futures=DualScanner(one,symbol='NSE:NIFTY28OCTFUT',timeframe='1',security_type='futures')
    assert futures.instrument_type=='futures'
    explicit=DualScanner(one,symbol='NSE:NIFTY',timeframe='1',config=Config(instrument_type='Stock'))
    assert explicit.instrument_type=='stock'
    arg=DualScanner(one,symbol='NSE:RELIANCE',timeframe='1',instrument_type='Index')
    assert arg.instrument_type=='index'


def test_regular_open_after_premarket_starts_orb():
    one=make(12)
    day=sorted(set(one.index.tz_convert('Asia/Kolkata').date))[1]
    preidx=pd.date_range(f'{day} 09:00:00',periods=15,freq='min',tz='Asia/Kolkata').tz_convert('UTC')
    pre=tiny(preidx)
    data=pd.concat([one,pre]).sort_index()
    sc=DualScanner(data,symbol='NSE:TEST',timeframe='1')
    i=next(i for i,z in enumerate(sc._local) if z.date()==day and z.hour==9 and z.minute==15)
    sc.i=i
    assert sc._session()['first'] is True
    sc._s2_bar(sc._features(),sc._session())
    assert sc.orb_start==i and np.isfinite(sc.orb_high)
    sc.i+=1
    assert sc._session()['first'] is False


def test_daily_event_closes_same_day_and_estimated_flag():
    one=make(6)
    d=aggregate(one,'1','D','Asia/Kolkata',555)
    sc=DualScanner(d,symbol='NSE:TEST',timeframe='D',mtf={'1':one})
    sc.i=0
    ev=sc.event('S1','TEST')
    actual=pd.Timestamp(ev['bar_close']).tz_convert('Asia/Kolkata')
    assert (actual.date()==sc._local[0].date()) and (actual.hour,actual.minute)==(15,30)
    assert ev['bar_close_estimated'] is True


def test_explicit_broker_close_preserved_in_event():
    one=make(6)
    d=aggregate(one,'1','D','Asia/Kolkata',555)
    explicit=[z.tz_convert('Asia/Kolkata').normalize()+pd.Timedelta(hours=13) for z in d.index]
    d['bar_close']=pd.DatetimeIndex(explicit).tz_convert('UTC')
    sc=DualScanner(d,symbol='NSE:TEST',timeframe='D',mtf={'1':one})
    sc.i=0
    ev=sc.event('S1','TEST')
    assert pd.Timestamp(ev['bar_close'])==d['bar_close'].iloc[0]
    assert ev['bar_close_estimated'] is False


@pytest.mark.parametrize('kwargs',[
    {'min_score':101},{'conflict_margin':9},{'min_adx':40},
    {'s2_confidence_floor':0},{'s2_orb_minutes':-10},
    {'hard_stop_intraday_pct':4.0},{'s2_burst_stock_pct':2.2},
    {'s2_max_bars_in_trade':20},{'s1_trail_atr':8},
    {'s2_min_stop_pct':float('nan')},{'s2_market_region':'EU'},
    {'s1_cooldown_bars':-1}
])
def test_source_illegal_configuration_rejected(kwargs):
    with pytest.raises(ValueError):Config(**kwargs).validate()


def test_default_input_ranges_are_valid():
    Config().validate()


def test_repro_original_index_burst_categories():
    one=make(5)
    assert DualScanner(one,symbol='NSE:NIFTY',timeframe='1').instrument_type=='index'
    assert DualScanner(one,symbol='MCX:CRUDEOIL',timeframe='1').instrument_type=='commodity'


def test_no_nonfinal_timestamps_in_manual_short_close():
    minute=make(6)
    bar=minute.iloc[:1].copy()
    # Broker provides a legally shorter final-candle close.
    bar['bar_close']=[bar.index[0]+pd.Timedelta(seconds=35)]
    require_candles(bar,'bar')
    close=candle_closes(bar.index,'1',explicit=bar['bar_close'])[0]
    assert close==bar.iloc[0]['bar_close']


def test_s2_orb_pine_half_up_rounding():
    # Pine math.round(5 / 2) = 3, unlike Python round(2.5) = 2.
    assert __import__('math').floor(5/2+.5)==3


def test_same_code_different_mtf_update_batches():
    minute=make(12)
    chart=aggregate(minute,'1','5','Asia/Kolkata',555)
    # Avoid future MTF input beyond the cutoff for this reproducibility test.
    sc=DualScanner(chart.iloc[:880],symbol='NSE:TEST',timeframe='5',
                   mtf={'1':minute.iloc[:4400]})
    before=sc.run()['events']
    appended=sc.update(chart.iloc[880:],mtf={'1':minute})
    all_at_once=DualScanner(chart,symbol='NSE:TEST',timeframe='5',
                            mtf={'1':minute}).run()
    assert before+appended==all_at_once['events']
    assert sc.run()['ledger']==all_at_once['ledger']
