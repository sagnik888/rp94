"""Confirmed UTS arrows and observed option candles share an execution clock."""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from engine.signal_processor import SignalProcessor


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    value = NS(uts_lane_enabled=True, options_sl_pct=15., ut_force_exit_time='15:25')
    monkeypatch.setattr('config.settings.get_settings', lambda: value)
    monkeypatch.setattr('engine.signal_processor.get_settings', lambda: value)
    return value


def arrow(at, *, exit_at=None, reason='', timeframe='5min'):
    return NS(timestamp=at, source_lane='UTS', timeframe=timeframe,
              direction='LONG', uts_exit_time=exit_at, uts_exit_reason=reason)


def path(start, minutes):
    frame = pd.DataFrame({'open':100., 'high':104., 'low':99., 'close':102.},
        index=pd.date_range(start, periods=minutes, freq='1min'))
    return frame


@pytest.mark.parametrize('tf,minutes', [('1min',1), ('5min',5), ('15min',15)])
def test_paired_reversal_waits_for_opposite_candle_close(tf, minutes):
    first = arrow(datetime(2026,9,29,10), exit_at=datetime(2026,9,29,10,30),
                  reason='UT_REVERSE', timeframe=tf)
    opposite = arrow(first.uts_exit_time, timeframe=tf)
    session_end = datetime(2026,9,29,15,25)
    start,end,reason = SignalProcessor._uts_replay_window(
        first, tf, session_end, next_signal=opposite)
    assert start == first.timestamp + timedelta(minutes=minutes)
    assert end == opposite.timestamp + timedelta(minutes=minutes)
    assert reason == 'UT_REVERSE'
    # Replay sees the option movement in the reversal candle, which the old
    # raw-open fallback cut off before the chart exit was available.
    frame = path(start, int((end-start).total_seconds()/60))
    frame.iloc[-1] = [102.,104.,101.,103.]
    replay = SignalProcessor.__new__(SignalProcessor)._simulate_actual_option_exit(
        frame, start, end, 90., 3., replay_source_lane='UTS', replay_setup=first,
        chart_exit_reason=reason, tf15_timeframe=tf)
    assert replay['exit_time'] == end
    assert replay['reason'] == 'UTS_REVERSE'
    assert replay['exit'] == 103.


def test_paired_window_is_idempotent_for_shifted_arrows():
    first = arrow(datetime(2026,9,29,10), exit_at=datetime(2026,9,29,10,20), reason='UT_REVERSE')
    opposite = arrow(first.uts_exit_time)
    end = datetime(2026,9,29,15,25)
    expected = SignalProcessor._uts_replay_window(first,'5min',end,next_signal=opposite)
    shifted_first = SignalProcessor._historical_signal_for_replay(first,'5min')
    shifted_opposite = SignalProcessor._historical_signal_for_replay(opposite,'5min')
    assert SignalProcessor._uts_replay_window(
        shifted_first,'5min',end,next_signal=shifted_opposite) == expected


@pytest.mark.parametrize('opposite_at', [datetime(2026,9,29,15,15), datetime(2026,9,30,9,15)])
def test_paired_replay_keeps_session_cap(opposite_at):
    first = arrow(datetime(2026,9,29,15), exit_at=opposite_at,
                  reason='UT_REVERSE', timeframe='15min')
    start,end,reason = SignalProcessor._uts_replay_window(first,'15min',
        datetime(2026,9,29,15,25),next_signal=arrow(opposite_at,timeframe='15min'))
    assert start == datetime(2026,9,29,15,15)
    assert end == datetime(2026,9,29,15,25)
    assert reason == ''  # Unclosed/next-day reversal cannot beat session end.


def test_earlier_smart_chart_exit_beats_later_opposite_signal():
    first = arrow(datetime(2026,9,29,10), exit_at=datetime(2026,9,29,10,10), reason='SMART_EXIT')
    start,end,reason = SignalProcessor._uts_replay_window(first,'5min',
        datetime(2026,9,29,15,25),next_signal=arrow(datetime(2026,9,29,10,30)))
    assert (start,end,reason) == (datetime(2026,9,29,10,5),datetime(2026,9,29,10,15),'SMART_EXIT')


def test_missing_option_bar_before_confirmed_reversal_abstains():
    first = arrow(datetime(2026,9,29,10), exit_at=datetime(2026,9,29,10,20), reason='UT_REVERSE')
    start,end,reason = SignalProcessor._uts_replay_window(first,'5min',
        datetime(2026,9,29,15,25),next_signal=arrow(first.uts_exit_time))
    frame = path(start,20).drop(datetime(2026,9,29,10,22))
    replay = SignalProcessor.__new__(SignalProcessor)._simulate_actual_option_exit(
        frame,start,end,90.,3.,replay_source_lane='UTS',replay_setup=first,
        chart_exit_reason=reason)
    assert replay is None


@pytest.mark.parametrize('tf,regime,stop', [('15min','RANGING',85.),('5min','RANGING',90.),
                                         ('15min','UNKNOWN',90.)])
def test_replay_fills_missing_lane_and_frame_without_grade_leakage(tf,regime,stop):
    legacy = NS(timeframe='', grade='C', confidence=.1, direction='LONG')
    frame = path(datetime(2026,9,29,10),2)
    replay = SignalProcessor.__new__(SignalProcessor)._simulate_actual_option_exit(
        frame,frame.index[0],frame.index[-1]+timedelta(minutes=1),50.,3.,
        replay_source_lane='UTS',replay_setup=legacy,tf15_timeframe=tf,
        replay_regime=regime,replay_grade='C',replay_confidence=.1)
    assert replay['stop'] == stop
    assert legacy.timeframe == '' and not hasattr(legacy,'source_lane')


def test_explicit_setup_frame_and_frozen_cap_survive_replay_adapter():
    frozen = arrow(datetime(2026,9,29,10),timeframe='15min')
    frozen.quality_verdict = {'policy_version':'uts_native.v1','checks':{'uts_premium_cap_pct':5.}}
    frame = path(frozen.timestamp,2)
    replay = SignalProcessor.__new__(SignalProcessor)._simulate_actual_option_exit(
        frame,frame.index[0],frame.index[-1]+timedelta(minutes=1),50.,3.,
        replay_source_lane='UTS',replay_setup=frozen,tf15_timeframe='5min',
        replay_regime='RANGING')
    assert replay['stop'] == 95.
    assert frozen.timeframe == '15min'


@pytest.mark.parametrize('field', ['high', 'close'])
def test_nonfinite_observed_option_price_abstains(field):
    frame = path(datetime(2026,9,29,10),2)
    frame.loc[frame.index[-1],field] = float('inf')
    if field == 'close':
        frame.loc[frame.index[-1],'high'] = float('inf')
    replay = SignalProcessor.__new__(SignalProcessor)._simulate_actual_option_exit(
        frame,frame.index[0],frame.index[-1]+timedelta(minutes=1),90.,3.,
        qty=65,replay_source_lane='UTS')
    assert replay is None
