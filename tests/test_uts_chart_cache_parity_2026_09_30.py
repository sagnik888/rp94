"""UTS display caches follow settings, candle close and ticks without trade-cache writes."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from config.settings import Settings
from engine import ut_smart_engine as uts
from engine.scanner_chart_mixin import ScannerChartMixin


class Host(ScannerChartMixin):
    def __init__(self,frame):
        self.frame=frame.copy()
        self.mode='REAL'
        self.config={'indices':{'NIFTY':{'lot_size':65}}}
        self.simulation_id=1
        self.chart_stream_enabled=True
        self.active_chart_instrument,self.active_chart_tf='NIFTY','15min'
        self.candles=NS(get_candles=lambda *args:self.frame,
                        latest_candle_time=lambda *args:self.frame.index[-1])
        self.trades=NS(open_trades={})
        self.intel_snapshots=NS(history=lambda *args,**kwargs:[])
        self.authority={'source_engine':'ut_smart','signals':[],
                        'trailing_stop':[999.]*len(frame),'state':{'position':999}}
        self._cached_results={'NIFTY_15min':self.authority}
        self.mtf=NS(_result_cache={'NIFTY_15min':self.authority},
                    engines={'NIFTY_15min':NS(process=self.forbidden_live_process)})
        self._chart_metadata_cache={}
        self.builds=0

    @staticmethod
    def forbidden_live_process(*args,**kwargs):
        raise AssertionError('A chart must never call the live order-authoritative engine')

    def _hydrate_chart_frame_from_disk(self,instrument,timeframe,frame):
        return frame

    def _chart_result_for_display(self,instrument,timeframe,frame,result):
        self.builds+=1
        return super()._chart_result_for_display(instrument,timeframe,frame,result)


@pytest.fixture
def setup(monkeypatch):
    import engine.scanner_chart_mixin as chart
    value=Settings(_env_file=None)
    object.__setattr__(value,'scanner_engine','ut_smart')
    object.__setattr__(value,'uts_lane_allow_intrabar_entries',False)
    object.__setattr__(value,'chart_rebuild_min_interval_seconds',2.)
    monkeypatch.setattr(chart,'get_settings',lambda:value)
    monkeypatch.setattr('config.settings.get_settings',lambda:value)
    clock=[pd.Timestamp('2026-09-30 15:30')]
    original=uts.evaluation_clock
    monkeypatch.setattr(uts,'evaluation_clock',lambda as_of=None:clock[0] if as_of is None else original(as_of))
    fixture=Path(__file__).with_name('fixtures')/'uts_nifty_15min_2026_09_30.csv'
    frame=pd.read_csv(fixture,parse_dates=['timestamp']).set_index('timestamp')
    return value,clock,frame


def snapshot(host):
    return host.get_chart_snapshot('NIFTY','15min')['chart']['15min']


def entries(payload):
    return [(pd.Timestamp(m['time'],unit='s',tz='UTC').tz_convert('Asia/Kolkata').strftime('%H:%M'),
             m['text'].split()[0]) for m in payload['markers']
            if m['text'].startswith(('BUY','SELL')) and
            pd.Timestamp(m['time'],unit='s',tz='UTC').tz_convert('Asia/Kolkata').date()==pd.Timestamp('2026-09-30').date()]


def test_private_chart_ignores_old_live_results_and_preserves_authoritative_caches(setup):
    _,_,frame=setup
    host=Host(frame)
    original=deepcopy(host.authority)
    payload=snapshot(host)
    assert entries(payload)==[('09:15','BUY'),('10:30','SELL'),('11:45','BUY'),('13:30','SELL')]
    assert payload['state']['position']!=999
    assert host._cached_results['NIFTY_15min'] is host.authority
    assert host.mtf._result_cache['NIFTY_15min'] is host.authority
    assert host.authority==original
    assert snapshot(host) is payload
    assert host.builds==1  # Unchanged fingerprint avoids another private calculation.


def test_same_bar_parameter_change_rebuilds_immediately_and_updates_markers(setup):
    settings,_,frame=setup
    object.__setattr__(settings,'chart_rebuild_min_interval_seconds',10000.)
    host=Host(frame)
    before=snapshot(host)
    timestamp=host.frame.index[-1]
    for key,value in dict(uts_key_value=2.,uts_atr_period=14,uts_use_heikin_ashi=True,
        uts_arm_atr=2.5,uts_retrace_atr=.7,uts_hard_retrace_atr=2.5,uts_confirmations=4).items():
        object.__setattr__(settings,key,value)
    after=snapshot(host)
    assert host.frame.index[-1]==timestamp
    assert host.builds==2 and after is not before
    assert entries(after)==[('10:00','BUY'),('14:15','SELL')]


def test_terminal_bar_clock_close_rebuilds_without_new_candle_or_waiting_for_floor(setup):
    settings,clock,frame=setup
    object.__setattr__(settings,'chart_rebuild_min_interval_seconds',10000.)
    clock[0]=pd.Timestamp('2026-09-30 09:29:59')
    host=Host(frame.loc[:'2026-09-30 09:15'])
    before=snapshot(host)
    assert entries(before)==[]
    clock[0]=pd.Timestamp('2026-09-30 09:30')
    after=snapshot(host)
    assert host.builds==2
    assert entries(after)==[('09:15','BUY')]
    assert len(before['candles'])==len(after['candles'])


def test_intrabar_policy_toggle_refreshes_same_forming_bar_immediately(setup):
    settings,clock,frame=setup
    object.__setattr__(settings,'chart_rebuild_min_interval_seconds',10000.)
    clock[0]=pd.Timestamp('2026-09-30 09:20')
    host=Host(frame.loc[:'2026-09-30 09:15'])
    before=snapshot(host)
    assert entries(before)==[]
    object.__setattr__(settings,'uts_lane_allow_intrabar_entries',True)
    object.__setattr__(settings,'live_intrabar_signal_entries',True)
    object.__setattr__(settings,'ut_signal_mode','realtime')
    after=snapshot(host)
    assert host.builds==2 and after is not before
    assert entries(after)==[('09:15','BUY')]
    assert host.authority['state']['position']==999


def test_forming_wick_tick_updates_overlay_after_floor_without_new_entries(setup):
    _,clock,frame=setup
    clock[0]=pd.Timestamp('2026-09-30 09:20')
    host=Host(frame.loc[:'2026-09-30 09:15'])
    before=snapshot(host)
    original_close,original_volume=host.frame.iloc[-1][['close','volume']]
    host.frame.loc[host.frame.index[-1],'high']+=50.
    host.frame.loc[host.frame.index[-1],'low']-=50.
    assert snapshot(host) is before and host.builds==1
    host.chart_cache_NIFTY_15min['built_at']-=3.
    after=snapshot(host)
    assert host.builds==2 and after is not before
    assert host.frame.iloc[-1]['close']==original_close
    assert host.frame.iloc[-1]['volume']==original_volume
    assert before['trailing_stop'][-1]['value']!=after['trailing_stop'][-1]['value']
    assert entries(before)==entries(after)==[]
    assert before['state']==after['state']  # Preview never changes confirmed trading state.


def test_closed_candle_correction_rebuilds_same_timestamp_immediately(setup):
    settings,_,frame=setup
    object.__setattr__(settings,'chart_rebuild_min_interval_seconds',10000.)
    host=Host(frame)
    before=snapshot(host)
    host.frame.loc[pd.Timestamp('2026-09-30 09:15'),'close']=22660.
    after=snapshot(host)
    assert host.builds==2 and before is not after
    assert entries(before)!=entries(after)


def test_frozen_final_session_bar_is_excluded_without_endless_forced_rebuild(setup):
    _,_,frame=setup
    host=Host(frame)
    assert host.frame.index[-1]==pd.Timestamp('2026-09-30 15:15')
    before=snapshot(host)
    last=pd.Timestamp(before['candles'][-1]['time'],unit='s',tz='UTC').tz_convert('Asia/Kolkata')
    assert last.strftime('%H:%M')=='15:00'
    assert snapshot(host) is before and host.builds==1


def test_utc_frame_uses_same_chart_session_and_markers(setup):
    _,_,frame=setup
    utc=frame.copy()
    utc.index=utc.index.tz_localize('Asia/Kolkata').tz_convert('UTC')
    assert entries(snapshot(Host(utc)))==entries(snapshot(Host(frame)))
