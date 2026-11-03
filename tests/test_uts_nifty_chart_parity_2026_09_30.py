"""Observed NIFTY candles distinguish Pine settings parity from feed-tail corruption.

Fixture: read-only dashboard raw standard candles, captured 2026-09-30; 137bars.
Expected BUY/SELL bar labels were supplied from the user's TradingView chart.
The vendor's previous15:15bar is retained in the fixture to prove its effect.
"""
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from config.settings import Settings
from engine.alt_scanners import AltSignalEngine
from engine import ut_smart_engine as uts


TODAY=pd.Timestamp('2026-09-30').date()
EXPECTED=[('09:15','BUY'),('10:30','SELL'),('11:45','BUY'),('13:30','SELL')]


@pytest.fixture
def candles():
    fixture=Path(__file__).with_name('fixtures')/'uts_nifty_15min_2026_09_30.csv'
    frame=pd.read_csv(fixture,parse_dates=['timestamp']).set_index('timestamp')
    assert len(frame)==137
    return frame


@pytest.fixture
def settings(monkeypatch):
    value=Settings(_env_file=None)
    value.set_signal_fire_mode('CANDLE_CLOSE')
    monkeypatch.setattr('config.settings.get_settings',lambda:value)
    return value


def process(candles,as_of='2026-09-30 15:30'):
    return AltSignalEngine(signal_source='ut_smart').process(candles,
        instrument='NIFTY',timeframe='15min',as_of=pd.Timestamp(as_of))


def today_signals(result):
    assert not result.get('error')
    return [(pd.Timestamp(s.timestamp).strftime('%H:%M'),s.signal_type)
            for s in result['signals'] if pd.Timestamp(s.timestamp).date()==TODAY]


def test_default_settings_reproduce_all_four_chart_entry_labels_and_smart_exit(candles,settings):
    p=uts.params_from_settings(settings,'15min')
    assert p.key_value==1. and p.atr_period==10 and not p.use_heikin_ashi
    assert (p.arm_atr,p.retrace_atr,p.hard_retrace_atr,p.confirmations)==(.8,.45,1.1,2)
    result=process(candles)
    assert today_signals(result)==EXPECTED
    long=next(s for s in result['signals'] if pd.Timestamp(s.timestamp)==pd.Timestamp('2026-09-30 11:45'))
    assert long.uts_exit_reason=='SMART_EXIT'
    assert pd.Timestamp(long.uts_exit_time)==pd.Timestamp('2026-09-30 13:15')
    assert long.uts_exit_price==pytest.approx(22774.75)


def test_terminal_nine_fifteen_bar_needs_its_close_not_the_next_bar(candles,settings):
    terminal=candles.loc[:'2026-09-30 09:15']
    before=process(terminal,'2026-09-30 09:29:59')
    assert today_signals(before)==[]
    after=process(terminal,'2026-09-30 09:30:00')
    assert today_signals(after)==[('09:15','BUY')]
    assert after['uts_evaluation']['last_closed_bar']==pd.Timestamp('2026-09-30 09:15')


def test_aware_utc_clock_matches_local_confirmed_close(candles,settings):
    terminal=candles.loc[:'2026-09-30 09:15']
    local=process(terminal,'2026-09-30 09:30')
    utc=process(terminal,pd.Timestamp('2026-09-30 04:00',tz='UTC'))
    assert today_signals(local)==today_signals(utc)==[('09:15','BUY')]
    assert local['state']['position']==utc['state']['position']
    assert local['state']['trailing_stop']==pytest.approx(utc['state']['trailing_stop'])


def test_old_tuned_parameters_explain_dashboard_late_entries(candles,settings):
    for key,value in dict(uts_key_value=2.,uts_atr_period=14,uts_use_heikin_ashi=True,
        uts_arm_atr=2.5,uts_retrace_atr=.7,uts_hard_retrace_atr=2.5,
        uts_confirmations=4).items():
        object.__setattr__(settings,key,value)
    assert today_signals(process(candles))==[('10:00','BUY'),('14:15','SELL')]


def test_vendor_tail_creates_previous_day_buy_and_suppresses_morning_label(candles,settings):
    # The previous day15:15aggregate consists of14 frozen one-minute bars and
    # one closing-print jump in stored FYERS data. Including it fabricates a UT
    # flip before today's morning. This is feed evidence, not a Pine session rule.
    filtered=uts.drop_off_session_bars(candles,'15:15')
    p=uts.params_from_settings(settings,'15min')
    out=uts.run(uts.indicators(filtered,p),p)
    labels=[(t.strftime('%H:%M'),'BUY' if out['signal'][i]==1 else 'SELL')
            for i,t in enumerate(filtered.index) if t.date()==TODAY and out['signal'][i]]
    assert labels==EXPECTED
    unfiltered=uts.drop_off_session_bars(candles,'15:30')
    p=replace(p,session_end='15:30')
    out=uts.run(uts.indicators(unfiltered,p),p)
    previous=unfiltered.index.get_loc(pd.Timestamp('2026-09-29 15:15'))
    morning=unfiltered.index.get_loc(pd.Timestamp('2026-09-30 09:15'))
    assert out['signal'][previous]==1
    assert out['signal'][morning]==0
    assert p.session_end=='15:30'  # Independent Pine-compatible comparison frame.
