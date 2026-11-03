"""UTS confirmation, result validity, entry policy, and option-exit parity."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from engine import ut_smart_engine as uts, uts_lane
from engine.alt_scanners import AltSignalEngine
from tests.test_ut_smart_live_path_2026_09_26 import _session_frame


@pytest.fixture
def closed_flip(monkeypatch):
    frame = _session_frame(days=12, freq='5min')
    last_day = frame.index[-1].normalize()
    frame = frame.loc[:last_day + pd.Timedelta(hours=10)].copy()
    for i, push in [(-2, -1000), (-1, 1000)]:
        value = frame['close'].iloc[i] + push
        frame.iloc[i, frame.columns.get_loc('close')] = value
        frame.iloc[i, frame.columns.get_loc('high')] = max(value, frame['high'].iloc[i])
        frame.iloc[i, frame.columns.get_loc('low')] = min(value, frame['low'].iloc[i])
    start = frame.index[-1].to_pydatetime()
    settings = Settings()
    object.__setattr__(settings, 'uts_use_heikin_ashi', False)
    object.__setattr__(settings, 'uts_key_value_5min', 1.)
    monkeypatch.setattr('config.settings.get_settings', lambda: settings)
    return frame, start, settings


def test_closed_final_bar_emits_flip_without_next_bar(closed_flip):
    frame, start, _ = closed_flip
    result = AltSignalEngine(signal_source='ut_smart').process(
        frame, instrument='NIFTY', timeframe='5min', as_of=start + timedelta(minutes=5, seconds=3))
    hit = [s for s in result['signals'] if s.timestamp == start]
    assert len(hit) == 1 and hit[0].signal_type == 'BUY' and not hit[0].uts_forming


def test_explicit_uts_opt_out_still_blocks_forming_signals(closed_flip):
    frame, start, settings = closed_flip
    settings.set_signal_fire_mode('INTRACANDLE')
    object.__setattr__(settings, 'uts_lane_allow_intrabar_entries', False)
    result = AltSignalEngine(signal_source='ut_smart').process(
        frame, instrument='NIFTY', timeframe='5min', as_of=start + timedelta(seconds=30))
    assert not any(s.timestamp == start for s in result['signals'])
    object.__setattr__(settings, 'uts_lane_allow_intrabar_entries', True)
    result = AltSignalEngine(signal_source='ut_smart').process(
        frame, instrument='NIFTY', timeframe='5min', as_of=start + timedelta(seconds=30))
    assert any(s.timestamp == start and s.uts_forming for s in result['signals'])


@pytest.mark.parametrize('result', [
    {'error': 'Insufficient UT SMART EXIT data', 'signals': [], 'state': None},
    {'signals': [], 'state': {'uts_state_bar_time': datetime(2026, 9, 30, 9, 30)}},
])
def test_unknown_result_never_authorizes_repaint_exit(result):
    trade = SimpleNamespace(source_engine='ut_smart', timeframe='15min', direction='LONG',
                            signal_time=datetime(2026, 9, 30, 10), entered_intracandle=True)
    assert uts_lane.repaint_exit(trade, result, datetime(2026, 9, 30, 10, 5), Settings()) == ''
    assert not getattr(trade, 'uts_confirmed', False)


def test_pine_chart_exit_retains_signal_candle_high():
    close = np.full(4, 100.)
    ind = dict(src=close, open=close, high=np.array([101., 130., 101., 101.]),
               low=np.full(4, 99.), close=close, atr=np.full(4, 10.),
               trail=np.array([105., 50., 50., 50.]), rsi=np.full(4, 50.),
               hist=np.ones(4), ema_exit=np.zeros(4))
    out = uts.run(ind, uts.UTSParams(arm_atr=2.5, hard_retrace_atr=2.5, confirmations=4))
    # Pine initializes peakPrice=high on the signal bar. This is an index
    # indicator state, not the observed option premium used by the profit lock.
    assert out['signal'][1] == 1 and out['state'][3] == 0
    assert out['exit'][3] == uts.EXIT_HARD and out['runup_atr'][3] == pytest.approx(3.)


def test_live_and_delayed_manual_quality_are_binding(monkeypatch):
    from scanner import Scanner
    from tests.test_s1_lane_2026_09_23 import _gate_scanner, _candidate

    calls = []
    _, scanner = _gate_scanner(monkeypatch, calls)
    candidate = _candidate(source_engine='ut_smart', source_lane='UTS')
    assert not scanner._passes_live_trade_ready_gate(candidate)
    assert calls == [candidate]
    scanner._passes_candidate_grade_preference = lambda c: (True, '')
    scanner._current_quality_verdict_for_candidate = lambda c, **kw: SimpleNamespace(
        allowed=False, reason='quality failed', reason_code='C_GRADE')
    allowed, reason, source = Scanner._current_manual_admission_decision(scanner, candidate, lane='MANUAL')
    assert not allowed and 'C_GRADE' in reason and source == 'manual_quality_gate'
    candidate.action = 'EXIT'
    assert scanner._passes_live_trade_ready_gate(candidate)
    assert calls == [candidate]  # Entry quality never delays a UTS exit.
    assert Scanner._current_manual_admission_decision(scanner,candidate,lane='MANUAL') == (True,'','')


@pytest.mark.parametrize('stop,allowed', [(96., False), (87.5, True)])
def test_option_stop_must_cover_uts_mapped_risk_only(stop, allowed):
    from engine.entry_policy import evaluate_final_entry_policy
    from tests.test_final_entry_policy_2026_08_27 import _geometry, _forecast

    now = datetime(2026, 9, 30, 10)
    geometry = _geometry(now, premium_stop=stop, contract_stop=stop,
                         premium_target=150., contract_target=150.)
    arguments = dict(min_rr=.5, min_rr_override=.5, expected_move_floor_pct=.2,
                     max_underlying_stop_pct=1., require_forecast=False,
                     max_forecast_age_seconds=90., max_geometry_age_seconds=10.,
                     checked_at=now, phase='actual_fill', lane='UTS', supported_instruments=['NIFTY'])
    verdict = evaluate_final_entry_policy(geometry, _forecast(now), require_premium_stop_coverage=True, **arguments)
    assert verdict.allowed is allowed, verdict.reason
    assert dict(verdict.checks)['option_stop_coverage'] is allowed
    if not allowed:
        assert 'delta-mapped' in verdict.reason
    generic = evaluate_final_entry_policy(geometry, _forecast(now), **arguments)
    assert generic.allowed, generic.reason


def test_option_profit_lock_arms_before_chart_and_keeps_nominal_target_disabled():
    trade = SimpleNamespace(inst_type='OPT', entry_price=100., initial_stop=90.)
    assert uts_lane.exit_decision(trade, 23000., 109.).premium_stop == 0.
    decision = uts_lane.exit_decision(trade, 23000., 120.)
    assert decision.runner and not decision.exit and decision.premium_stop == pytest.approx(114.)
    decision = uts_lane.exit_decision(trade, 23000., 113.)
    assert decision.exit and decision.reason == 'UTS_PROFIT_LOCK'
    assert decision.premium_stop == pytest.approx(114.)


def _option_frame(rows):
    return pd.DataFrame(rows, columns=['open','high','low','close'],
                        index=pd.date_range('2026-09-29 10:00', periods=len(rows), freq='1min'))


def test_option_replay_reuses_lock_without_same_bar_lookahead():
    from engine.signal_processor import SignalProcessor
    frame = _option_frame([(100,121,99,120), (120,120,112,113)])
    replay = SignalProcessor.__new__(SignalProcessor)._simulate_actual_option_exit(
        frame, frame.index[0], frame.index[-1] + timedelta(minutes=1), 90., 3.,
        natural_sl_distance=10., qty=65, replay_source_lane='UTS', required_stop_distance=10.)
    assert replay['reason'] == 'SL HIT' and replay['exit'] == pytest.approx(114.7)
    assert replay['price_path'][0]['active_stop'] == 90.
    assert replay['price_path'][0]['next_stop'] == pytest.approx(114.7)
    assert replay['price_path'][1]['active_target'] == 0.
    assert replay['target'] == pytest.approx(130.)  # nominal geometry remains valid


def test_option_replay_accepts_cap_before_chart_stop_but_requires_complete_path():
    from engine.signal_processor import SignalProcessor
    frame = _option_frame([(100,105,99,104), (104,105,103,104)])
    processor = SignalProcessor.__new__(SignalProcessor)
    arguments = dict(start_ts=frame.index[0], end_ts=frame.index[-1]+timedelta(minutes=1),
                     sl_price=90., rr=3., natural_sl_distance=10., replay_source_lane='UTS')
    capped = processor._simulate_actual_option_exit(frame, required_stop_distance=55., **arguments)
    assert capped is not None and capped['stop'] == pytest.approx(90.)
    assert processor._simulate_actual_option_exit(frame.iloc[:1], required_stop_distance=10., **arguments) is None
    # A stale estimate must not block a compatible, higher observed option premium.
    actual = _option_frame([(500,505,495,500), (500,505,495,500)])
    result = processor._simulate_actual_option_exit(
        actual, actual.index[0], actual.index[-1]+timedelta(minutes=1), 80., 3.,
        natural_sl_distance=20., replay_source_lane='UTS', required_stop_distance=40.,
        replay_regime='RANGING', replay_grade='A', replay_confidence=.8)
    assert result is not None and result['stop'] == pytest.approx(425.)


def test_chart_replay_times_are_bar_closes_and_chart_exit_has_priority():
    from engine.signal_processor import SignalProcessor
    signal = SimpleNamespace(timestamp=datetime(2026,9,29,10),
                             uts_exit_time=datetime(2026,9,29,10,20), uts_exit_reason='SMART_EXIT')
    start, end, reason = SignalProcessor._uts_replay_window(signal, '5min', datetime(2026,9,29,11))
    assert (start,end,reason) == (datetime(2026,9,29,10,5),datetime(2026,9,29,10,25),'SMART_EXIT')
    shifted = SignalProcessor._historical_signal_for_replay(signal, '5min')
    assert SignalProcessor._uts_replay_window(shifted, '5min', datetime(2026,9,29,11)) == (start,end,reason)
    trade = SimpleNamespace(inst_type='OPT', entry_price=100., initial_stop=90., s1_peak_premium=140.)
    assert uts_lane.exit_decision(trade,0.,100.,engine_exit={'reason':'SMART_EXIT'}).reason == 'UTS_SMART_EXIT'


def test_repaint_requires_fresh_matching_frame_coverage():
    ts = datetime(2026,9,30,10)
    trade = SimpleNamespace(source_engine='ut_smart', instrument='NIFTY', timeframe='5min', direction='LONG',
                            signal_time=ts, entered_intracandle=True)
    now = ts + timedelta(minutes=2)
    result = dict(source_engine='ut_smart', instrument='NIFTY', timeframe='5min', signals=[],
                  uts_evaluation=dict(observed_at=now, first_bar=ts-timedelta(days=1),
                                      last_closed_bar=ts-timedelta(minutes=5), forming_bar=ts))
    result['uts_evaluation']['observed_at'] = now-timedelta(seconds=31)
    assert uts_lane.repaint_exit(trade,result,now) == ''
    result['uts_evaluation']['observed_at'] = now
    result['instrument'] = 'BANKNIFTY'
    assert uts_lane.repaint_exit(trade,result,now) == ''
    result['instrument'] = 'NIFTY'
    assert uts_lane.repaint_exit(trade,result,now) == 'REPAINT'


def test_confirmation_and_entry_kind_survive_sqlite_restore(tmp_path):
    from data.sqlite_db import DatabaseManager
    from trading.trade_manager import Trade

    database = object.__new__(DatabaseManager)
    database._init_db(str(tmp_path/'isolated.db'))
    try:
        trade = Trade(id='UTS-restore', instrument='NIFTY', timeframe='5min', direction='LONG',
                      entry_price=100., entry_time=datetime(2026,9,30,10,5), trailing_stop=90.,
                      current_stop=114., lots=1, lot_size=65, grade='A', inst_type='OPT',
                      entered_intracandle=False, uts_confirmed=True, source_lane='UTS', s1_peak_premium=120.)
        assert database.save_trade(trade.to_dict())
        restored = Trade.from_dict(database.load_all_trades()[0])
        assert not restored.entered_intracandle and restored.uts_confirmed
        assert restored.s1_peak_premium == 120. and restored.current_stop == 114.
        assert uts_lane.repaint_exit(restored,{'signals':[]},datetime(2026,9,30,10,6)) == ''
        legacy = trade.to_dict()
        legacy.update(entered_intracandle=None, uts_confirmed=None, signal_time='2026-09-30T10:00:00+05:30')
        for fill, forming in [('10:05:00',False),('10:04:59',True)]:
            legacy['entry_filled_at'] = '2026-09-30T'+fill+'+05:30'
            recovered = Trade.from_dict(legacy)
            assert recovered.entered_intracandle is forming and not recovered.uts_confirmed
    finally:
        database.conn.close()


@pytest.mark.parametrize('shadow',[False,True])
def test_close_boundary_recomputes_unchanged_ohlc_in_both_engines(monkeypatch,closed_flip,shadow):
    from engine import engine_lab
    from engine.multi_timeframe import MultiTimeframeEngine

    frame,start,_ = closed_flip
    clock = [pd.Timestamp(start)+timedelta(seconds=30)]
    original_clock = uts.evaluation_clock
    monkeypatch.setattr(uts,'evaluation_clock',lambda as_of=None: original_clock(as_of) if as_of is not None else clock[0])
    calls = []
    class Engine:
        signal_source = 'ut_smart'
        signal_mode = 'realtime'
        def process(self,**arguments):
            calls.append(arguments['as_of'])
            return {'signals':[], 'state':{'position':0}}

    if shadow:
        monkeypatch.setattr(engine_lab,'ENGINE_LAB_MODES',('ut_smart',))
        monkeypatch.setattr(engine_lab,'build_shadow_engine',lambda *a: Engine())
        engine = engine_lab.EngineLab()
        run = lambda: engine.process(candles_by_instrument={'NIFTY':{'5min':frame}})
    else:
        engine = MultiTimeframeEngine.__new__(MultiTimeframeEngine)
        engine.engines = {'NIFTY_5min':Engine()}
        engine._data_cache, engine._result_cache, engine._data_hash = {}, {}, {}
        engine._input_metadata, engine._result_metadata, engine._fresh_result_required_after = {}, {}, {}
        engine.tf_weights = {'1min':.15,'5min':.40,'15min':.45}
        engine.update_data('NIFTY','5min',frame)
        run = lambda: engine.process_instrument('NIFTY')
    run();run()
    assert len(calls) == 1
    clock[0] = pd.Timestamp(start)+timedelta(minutes=5,seconds=1)
    run();run()
    assert calls == [pd.Timestamp(start)+timedelta(seconds=30),clock[0]]
