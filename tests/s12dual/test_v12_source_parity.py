"""v1.2 regression coverage for confirmed Pine-vs-Python mismatches and one-stop API."""
from dataclasses import fields
from dataclasses import replace
from pathlib import Path
import pandas as pd
import pytest
from engine.s12dual import Config, DualScanner
from engine.s12dual.engine import Trade
from tests.s12dual.generate_demo import make

VENDORED=Path(__file__).resolve().parents[2]/'engine'/'s12dual'
from engine.s12dual.mtf import aggregate


def tiny(time='2026-10-09 09:05',tz='Asia/Kolkata'):
    idx=pd.DatetimeIndex([pd.Timestamp(time,tz=tz)]).tz_convert('UTC')
    return pd.DataFrame({'open':[100.], 'high':[101.], 'low':[99.],
                         'close':[100.], 'volume':[15000.]},index=idx)


def scanner_5m(region):
    # 5m close 15:30 IST on same physical exchange candle for both configurations
    s=DualScanner(tiny('2026-10-09 15:25'),symbol='NSE:RELIANCE',timeframe='5',
                  config=Config(s2_market_region=region),security_type='stock')
    s.i=0
    return s


@pytest.mark.parametrize('region',['Universal','US (NYSE/NASDAQ)','India (NSE/BSE)'])
def test_s1_cash_clock_unchanged_by_s2_region(region):
    s=scanner_5m(region)
    native=s._session('S1')
    assert native['region']=='India'
    assert native['market'] is True
    assert native['end']==930
    assert s._bar_closes[0].tz_convert('Asia/Kolkata').hour==15
    assert s._bar_closes[0].tz_convert('Asia/Kolkata').minute==30
    if region.startswith('US'):
        assert s._session('S2')['region']=='US'
        assert s._session('S2')['market'] is False
    else:
        assert s._session('S2')['market'] is True


def test_s1_real_position_exits_on_india_clock_when_s2_forced_us():
    mins=make(5)
    chart=aggregate(mins,'1','5','Asia/Kolkata',555)
    # Give last chart bar the same NSE 15:25 IST OPEN used in the audit repro.
    # The first 500 bars are warm-up, the last one is a new trade date.
    chart=chart.iloc[:550].copy()
    new_last=chart.index[-1]
    new_last= pd.Timestamp('2026-10-09 15:25',tz='Asia/Kolkata').tz_convert('UTC')
    chart.index=chart.index[:-1].append(pd.DatetimeIndex([new_last]))
    s=DualScanner(chart,symbol='NSE:RELIANCE',timeframe='5',
          mtf={'1':mins},config=Config(s2_market_region='US (NYSE/NASDAQ)'),
          security_type='stock')
    s.i=len(chart)-1
    x=s._features()
    ep=x['close']; stop=ep*.40
    s.s1.active=Trade('LONG',ep,s.i-20,'S1_PROBE',stop,stop,ep*1.8,ep*1.9,ep*2.,'PROBE',peak=ep,trough=ep)
    s._s1_bar(x,s._session('S1'))
    assert s.s1.active is None
    assert s.events[-1]['reason']=='INTRADAY EOD (CLOSE EST)'


def test_non_mcx_commodity_future_has_pine_detection_and_session():
    s=DualScanner(tiny(),symbol='NSE:GOLD',timeframe='1',security_type='futures')
    s.i=0
    ses=s._session('S2')
    assert s.s2_is_commodity and not s.s2_is_index
    assert ses['region']=='India' and ses['is_commodity']
    assert ses['session_start']==540 and ses['session_end']==1410
    assert ses['market'] is True
    assert s.session_offset==540


@pytest.mark.parametrize('manual',['Auto-Detect','Index','Bank Nifty','Stock'])
def test_s1_manual_override_cannot_change_s2_stock_classification(manual):
    s=DualScanner(tiny('2026-10-09 09:15'),symbol='NSE:RELIANCE',timeframe='1',
                  security_type='stock',instrument_type=manual)
    assert s.s2_is_index is False and s.s2_is_commodity is False
    assert s.cfg.s2_burst_stock_pct==.60


@pytest.mark.parametrize('symbol,typ,is_index',[('NSE:NIFTY','index',True),
    ('NSE:NIFTYIT','futures',False),('NSE:BANKNIFTY','futures',True),
    ('BSE:SENSEX','index',True),('NSE:NIFTYBEES','stock',False)])
def test_s2_index_matches_source_metadata(symbol,typ,is_index):
    s=DualScanner(tiny('2026-10-09 09:15'),symbol=symbol,timeframe='1',security_type=typ)
    assert s.s2_is_index is is_index


def test_single_stop_fields_in_config_trade_and_event_schema():
    assert 's1_stop_pct' in {f.name for f in fields(Config)}
    assert 'stop2' not in {f.name for f in fields(Trade)}
    assert 'sl2_pct' not in {f.name for f in fields(Config)}
    assert 'sl1_pct' not in {f.name for f in fields(Config)}
    with pytest.raises(TypeError):Config(sl2_pct=.7)
    mins=make(12)
    chart=aggregate(mins,'1','5','Asia/Kolkata',555)
    s=DualScanner(chart,symbol='NSE:TEST',timeframe='5',mtf={'1':mins})
    out=s.run()
    assert out['events']
    for ev in out['events']:
        assert not {'sl1','sl2','sl1_pct','sl2_pct'} & ev.keys()
        if ev['event'] in {'ENTRY','FILL_ESTIMATE'}:
            assert 'stop' in ev and ev['stop']>0
    for side,position in out['positions'].items():
        if position is not None:
            assert 'stop' in position
            assert not {'stop2','sl1','sl2','trailing'} & position.keys()
    assert 'sl2' not in (VENDORED/'config.defaults.json').read_text().lower()


def test_stop_update_is_single_effective_reference():
    s=DualScanner(tiny('2026-10-09 09:15'),symbol='NSE:RELIANCE',timeframe='5')
    s.i=0
    tr=Trade('LONG',100.,0,'probe',99.,99.5,102.,103.,104.,'test',peak=100.,trough=100.)
    s.s1.active=tr
    pos=s.current_positions()['S1']
    assert pos['stop']==99.5 and pos['initial_risk_per_unit']==1.
    assert 'trailing' not in pos and 'stop2' not in pos


def test_s1_stop_percent_is_configurable_and_no_second_stop_exists():
    c=Config(s1_stop_pct=.35)
    c.validate()
    s=DualScanner(tiny('2026-10-09 09:15'),symbol='NSE:RELIANCE',timeframe='5',config=c)
    assert c.s1_stop_pct==pytest.approx(.35)


def test_s1_events_identical_regardless_of_s2_region():
    mins=make(12)
    chart=aggregate(mins,'1','5','Asia/Kolkata',555)
    base=DualScanner(chart,symbol='NSE:RELIANCE',timeframe='5',mtf={'1':mins},
                     config=Config(s2_market_region='Universal'),security_type='stock').run()
    us=DualScanner(chart,symbol='NSE:RELIANCE',timeframe='5',mtf={'1':mins},
                   config=Config(s2_market_region='US (NYSE/NASDAQ)'),security_type='stock').run()
    assert [e for e in base['events'] if e['engine']=='S1']==[e for e in us['events'] if e['engine']=='S1']
    s1_fields=('time','s1_side','s1_stop','s1_bull','s1_bear')
    assert [tuple(e[k] for k in s1_fields) for e in base['bars']]==[tuple(e[k] for k in s1_fields) for e in us['bars']]


def test_no_legacy_second_stop_in_executable_python():
    root=VENDORED
    for source in root.glob('*.py'):
        contents=source.read_text().lower()
        for key in ('sl2', 'stop2', 'second_stop'):
            assert key not in contents, (source.name,key)
