"""Regressions from the user's uploaded UT Bot + Smart Peak Exit v6 source.

EMA/RMA expectations use TradingView's official v6 reference recurrences.
These tests validate chart logic, separately from live premium capital stops.
"""
import numpy as np
import pandas as pd
import pytest

from engine import ut_smart_engine as uts


def controlled(**changes):
    close = np.full(4,100.)
    values = dict(src=close.copy(),open=close.copy(),high=np.full(4,101.),
                  low=np.full(4,99.),close=close.copy(),atr=np.full(4,10.),
                  trail=np.array([105.,50.,50.,50.]),rsi=np.full(4,50.),
                  hist=np.ones(4),ema_exit=np.zeros(4))
    values.update(changes)
    return values


def test_ema_uses_official_first_source_seed_not_rma_sma_seed():
    x=np.array([np.nan,10.,20.,30.,np.nan,40.])
    expected=np.array([np.nan,10.,15.,22.5,22.5,31.25])
    np.testing.assert_allclose(uts._ema(x,3),expected,equal_nan=True)
    np.testing.assert_allclose(uts._ema(np.array([10.,20.,30.]),1),[10.,20.,30.])


def test_rma_seed_counts_non_na_samples_across_missing_data():
    x=np.array([1.,np.nan,2.,3.,np.nan,4.])
    expected=np.array([np.nan,np.nan,np.nan,2.,2.,8./3.])
    np.testing.assert_allclose(uts._rma(x,3),expected,equal_nan=True)


def test_rsi_missing_changes_do_not_become_zero_price_moves():
    src=np.array([1.,2.,np.nan,3.,4.,5.])
    out=uts._rsi(src,2)
    assert np.isnan(out[:4]).all()
    np.testing.assert_allclose(out[4:],[100.,100.])


@pytest.mark.parametrize('direction',[1,-1])
def test_pine_chart_seeds_peak_or_trough_from_signal_bar_extreme(direction):
    ind=controlled()
    if direction==1:
        ind['high'][1]=130.
    else:
        ind['trail']=np.array([95.,150.,150.,150.])
        ind['low'][1]=70.
    out=uts.run(ind,uts.UTSParams(arm_atr=2.5,hard_retrace_atr=2.5,
                                confirmations=4,min_bars=2))
    assert out['signal'][1]==direction
    assert out['exit'][2]==0
    assert out['exit'][3]==uts.EXIT_HARD
    assert out['runup_atr'][3]==pytest.approx(3.)
    assert out['state'][3]==0


@pytest.mark.parametrize('direction',[1,-1])
def test_rsi_highest_lowest_ignore_na_in_the_lookback(direction):
    ind=controlled(high=np.array([101.,101.,110.,110.]),
                   close=np.array([100.,100.,105.,105.]),
                   src=np.array([100.,100.,105.,105.]),
                   open=np.array([100.,100.,105.,105.]),
                   rsi=np.array([np.nan,80.,75.,70.]))
    if direction==-1:
        ind.update(trail=np.array([95.,150.,150.,150.]),
            low=np.array([99.,99.,90.,90.]),high=np.full(4,101.),
            close=np.array([100.,100.,95.,95.]),src=np.array([100.,100.,95.,95.]),
            open=np.array([100.,100.,95.,95.]),rsi=np.array([np.nan,20.,25.,30.]),
            ema_exit=np.full(4,200.))
    out=uts.run(ind,uts.UTSParams(arm_atr=.8,retrace_atr=.45,hard_retrace_atr=100.,
                                min_bars=1,confirmations=1,rsi_lookback=5))
    assert out['signal'][1]==direction
    assert out['exit'][2]==uts.EXIT_SMART


def test_syminfo_mintick_floors_rejection_wick_ratio():
    ind=controlled(open=np.array([100.,100.,109.,100.01]),
        high=np.array([101.,101.,110.,100.02]),low=np.array([99.,99.,109.,100.]),
        close=np.array([100.,100.,109.,100.005]),src=np.array([100.,100.,109.,100.005]))
    p=uts.UTSParams(arm_atr=.8,retrace_atr=.45,hard_retrace_atr=100.,
                    min_bars=2,confirmations=1)
    out=uts.run({**ind,'mintick':.05},p)
    assert out['exit'][3]==0  # Upper wick .01 / tick floor .05 = .20, below .35.
    tight=uts.run({**ind,'mintick':.01},p)
    assert tight['exit'][3]==uts.EXIT_SMART  # .01 / actual range .02 = .50.


def test_ut_direction_persists_after_smart_exit_clears_trade_state():
    close=np.array([100.,110.,112.,100.])
    ind=controlled(src=close,close=close,open=close,
        high=np.array([101.,111.,115.,101.]),trail=np.array([105.,90.,90.,90.]))
    out=uts.run(ind,uts.UTSParams(arm_atr=.4,hard_retrace_atr=1.,confirmations=4))
    assert out['signal'][1]==1 and out['exit'][3]==uts.EXIT_HARD
    assert out['state'][3]==0
    assert out['ut_position'][3]==1


def test_ut_direction_uses_strict_previous_stop_crossing():
    src=np.array([100.,100.,101.])
    ind=dict(src=src,open=src,high=src+1,low=src-1,close=src,
        atr=np.full(3,10.),trail=np.array([105.,100.,99.]),
        rsi=np.full(3,50.),hist=np.ones(3),ema_exit=np.zeros(3))
    out=uts.run(ind,uts.UTSParams())
    assert out['signal'][2]==1  # ta.crossover permits equality on the previous bar.
    assert out['ut_position'][2]==0  # Pine utPos explicitly requires strict inequality.


def test_ut_signal_has_priority_over_simultaneous_hard_peak_exit():
    close=np.array([100.,100.,110.,90.])
    ind=controlled(src=close,close=close,open=close,
        high=np.array([101.,130.,131.,101.]),trail=np.array([105.,50.,50.,150.]))
    out=uts.run(ind,uts.UTSParams(arm_atr=.8,hard_retrace_atr=1.1,min_bars=2))
    assert out['signal'][3]==-1
    assert out['exit'][3]==0 and out['state'][3]==-1


def test_trail_initialization_uses_nz_zero_and_pine_branch_order():
    src=np.array([2.,3.,4.])
    loss=np.array([np.nan,np.nan,20.])
    actual=uts._trailing_stop(src,loss)
    np.testing.assert_allclose(actual,[np.nan,np.nan,0.],equal_nan=True)


def test_ha_changes_source_but_keeps_real_ohlc_for_exit_wicks_and_entry():
    df=pd.DataFrame(dict(open=[100.,102.],high=[110.,105.],low=[99.,98.],
                         close=[105.,99.]),index=pd.date_range('2026-09-30 09:15',periods=2,freq='5min'))
    df.attrs['mintick']=.01
    ind=uts.indicators(df,uts.UTSParams(use_heikin_ashi=True))
    np.testing.assert_allclose(ind['src'],[103.5,101.])
    np.testing.assert_allclose(ind['close'],[105.,99.])
    assert ind['mintick']==.01


def test_session_filter_uses_market_timezone_without_changing_input_index():
    local=pd.date_range('2026-09-30 08:55','2026-09-30 15:35',freq='5min',tz='Asia/Kolkata')
    df=pd.DataFrame({'close':np.arange(len(local))},index=local)
    utc=df.tz_convert('UTC')
    expected=uts.drop_off_session_bars(df)
    actual=uts.drop_off_session_bars(utc)
    pd.testing.assert_frame_equal(actual.tz_convert('Asia/Kolkata'),expected)
    assert str(actual.index.tz)=='UTC'
    assert expected.index[0].strftime('%H:%M')=='09:15'
    assert expected.index[-1].strftime('%H:%M')=='15:10'
