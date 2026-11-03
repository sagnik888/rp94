"""UTS chart plots update independently of confirmed entry and trade state."""
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from engine.alt_scanners import AltSignalEngine
from engine import ut_smart_engine as uts


@pytest.fixture
def chart_settings(monkeypatch):
    settings = Settings()
    for field,value in {
        'uts_key_value':1.,'uts_atr_period':10,'uts_use_heikin_ashi':False,
        'uts_session_end':'15:30','uts_lane_allow_intrabar_entries':False,
    }.items():
        object.__setattr__(settings,field,value)
    monkeypatch.setattr('config.settings.get_settings',lambda:settings)
    return settings


def candles():
    index = pd.date_range('2026-09-29 09:15',periods=24,freq='15min').append(
        pd.date_range('2026-09-30 09:15',periods=16,freq='15min'))
    close = 23000. + np.arange(len(index))*3.
    opening = np.r_[close[0],close[:-1]]
    return pd.DataFrame({'open':opening,'high':np.maximum(opening,close)+6.,
        'low':np.minimum(opening,close)-6.,'close':close,'volume':1000.},index=index)


def signal_identity(result):
    return [(s.timestamp,s.signal_type,getattr(s,'uts_forming',False)) for s in result['signals']]


def test_ut_trail_colour_uses_source_side_even_while_trade_state_is_flat(chart_settings):
    frame = candles()
    clock = frame.index[-1]+timedelta(minutes=15)
    result = AltSignalEngine(signal_source='ut_smart').process(
        frame,instrument='NIFTY',timeframe='15min',as_of=clock)
    ind = uts.indicators(frame,uts.params_from_settings(chart_settings,'15min'))
    valid = np.flatnonzero(np.isfinite(ind['trail']))
    assert valid.size and result['state']['position'] == 0
    for i in valid:
        expected = 'green' if ind['src'][i] > ind['trail'][i] else 'red'
        assert result['trailing_stop_colors'][i] == expected


def test_forming_trail_and_ema_move_without_admitting_forming_flip(chart_settings):
    frame = candles()
    stamp = frame.index[-1]
    frame.loc[stamp,['high','low','close']] = [frame.loc[stamp,'open']+6.,
        frame.loc[stamp,'close']-306.,frame.loc[stamp,'close']-300.]
    clock = stamp+timedelta(minutes=2)
    engine = AltSignalEngine(signal_source='ut_smart')
    result = engine.process(frame,instrument='NIFTY',timeframe='15min',as_of=clock)
    baseline = engine.process(frame.iloc[:-1],instrument='NIFTY',timeframe='15min',as_of=clock)
    ind = uts.indicators(frame,uts.params_from_settings(chart_settings,'15min'))
    assert uts.run(ind,uts.params_from_settings(chart_settings,'15min'))['signal'][-1] == -1
    assert signal_identity(result) == signal_identity(baseline)
    assert result['state']['position'] == baseline['state']['position']
    assert result['state']['trailing_stop'] == baseline['state']['trailing_stop']
    assert result['trailing_stop'][-1] == pytest.approx(ind['trail'][-1])
    assert result['indicator_overlays'][0]['values'][-1] == pytest.approx(ind['ema_exit'][-1])
    assert result['trailing_stop_colors'][-1] == 'red'
    assert result['uts_evaluation']['last_closed_bar'] == frame.index[-2]


def test_display_never_reads_or_plots_future_candle(chart_settings):
    frame = candles()
    stamp = frame.index[-1]
    clock = stamp+timedelta(minutes=2)
    future = pd.DataFrame({'open':[100.],'high':[110.],'low':[90.],
        'close':[105.],'volume':[1000.]},index=[stamp+timedelta(minutes=15)])
    engine = AltSignalEngine(signal_source='ut_smart')
    baseline = engine.process(frame,instrument='NIFTY',timeframe='15min',as_of=clock)
    result = engine.process(pd.concat([frame,future]),instrument='NIFTY',timeframe='15min',as_of=clock)
    assert signal_identity(result) == signal_identity(baseline)
    assert result['state']['current_price'] == baseline['state']['current_price']
    assert result['trailing_stop'][-2] == baseline['trailing_stop'][-1]
    assert result['trailing_stop'][-1] is None
    assert result['indicator_overlays'][0]['values'][-1] is None


def test_heikin_ashi_trail_colour_uses_ha_source(chart_settings):
    object.__setattr__(chart_settings,'uts_use_heikin_ashi',True)
    frame = candles()
    # A wide upper wick can place HA source above the trail while raw close
    # falls below it. The plotted trail uses HA source when that input is on.
    frame.iloc[-1,frame.columns.get_loc('high')] += 200.
    clock = frame.index[-1]+timedelta(minutes=2)
    ind = uts.indicators(frame,uts.params_from_settings(chart_settings,'15min'))
    result = AltSignalEngine(signal_source='ut_smart').process(
        frame,instrument='NIFTY',timeframe='15min',as_of=clock)
    expected = 'green' if ind['src'][-1] > ind['trail'][-1] else 'red'
    assert frame['close'].iloc[-1] < ind['trail'][-1] < ind['src'][-1]
    assert result['trailing_stop_colors'][-1] == expected
    assert result['trailing_stop'][-1] == pytest.approx(ind['trail'][-1])
