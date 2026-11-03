"""UTS replay must retain observed option highs without inventing OHLC order."""
from datetime import timedelta
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from engine.signal_processor import SignalProcessor


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    value = NS(uts_lane_enabled=True, options_sl_pct=15., ut_force_exit_time='15:25',
               uts_lane_lock_arm_r=1., uts_lane_lock_giveback_pct=30.)
    monkeypatch.setattr('config.settings.get_settings', lambda: value)
    monkeypatch.setattr('engine.signal_processor.get_settings', lambda: value)
    return value


def replay(rows, *, instrument='NIFTY', timeframe='5min', regime='UNKNOWN',
           lane='UTS', chart_reason=''):
    frame = pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'],
                         index=pd.date_range('2026-09-29 10:00', periods=len(rows), freq='1min'))
    return SignalProcessor.__new__(SignalProcessor)._simulate_actual_option_exit(
        frame, frame.index[0], frame.index[-1] + timedelta(minutes=1), 50., 3.,
        qty=65, replay_source_lane=lane,
        replay_setup=NS(instrument=instrument, timeframe=timeframe, direction='LONG'),
        tf15_instrument=instrument, tf15_timeframe=timeframe, replay_regime=regime,
        chart_exit_reason=chart_reason, live_exits=False)


def test_safe_high_then_close_cross_exits_at_observed_close():
    result = replay([(100, 120, 99, 105), (105, 106, 89, 90)])
    assert result['stop'] == 90.
    assert result['reason'] == 'UTS_PROFIT_LOCK'
    assert result['exit'] == 105.  # The trigger is 114; its fill is not observed.
    assert result['exit_time'] == pd.Timestamp('2026-09-29 10:01')
    assert result['price_path'][0]['next_stop'] == pytest.approx(114.)
    assert len(result['price_path']) == 1


def test_original_resting_stop_beats_same_bar_high_and_close_cross():
    result = replay([(100, 120, 89, 105)])
    assert result['reason'] == 'SL HIT' and result['exit'] == 90.
    assert 'next_stop' not in result['price_path'][0]


def test_high_arms_lock_carried_to_next_bar():
    result = replay([(100, 120, 99, 116), (116, 117, 113, 115)])
    assert result['price_path'][0]['active_stop'] == 90.
    assert result['price_path'][0]['next_stop'] == pytest.approx(114.)
    assert result['price_path'][1]['active_stop'] == pytest.approx(114.)
    assert result['exit'] == pytest.approx(114.)


def test_low_alone_cannot_claim_new_lock_hit_with_unknown_order():
    result = replay([(100, 120, 99, 116)])
    assert result['reason'] == 'HISTORICAL' and result['exit'] == 116.
    assert result['price_path'][0]['next_stop'] == pytest.approx(114.)


def test_later_higher_high_tightens_lock_without_same_bar_low_guess():
    result = replay([(100, 120, 99, 116), (116, 130, 115, 126)])
    assert result['reason'] == 'HISTORICAL' and result['exit'] == 126.
    assert result['price_path'][1]['active_stop'] == pytest.approx(114.)
    assert result['price_path'][1]['next_stop'] == pytest.approx(121.)


def test_gap_below_previous_profit_lock_uses_observed_open():
    result = replay([(100, 120, 99, 116), (110, 116, 109, 115)])
    assert result['exit'] == 110.
    assert result['exit_time'] == pd.Timestamp('2026-09-29 10:01')


def test_unarmed_high_does_not_create_floor():
    result = replay([(100, 109, 99, 102), (102, 103, 89, 90)])
    assert result['reason'] == 'SL HIT' and result['exit'] == 90.
    assert result['price_path'][0]['next_stop'] == 90.


@pytest.mark.parametrize('instrument,timeframe,regime,initial', [
    ('NIFTY', '1min', 'UNKNOWN', 95.),
    ('NIFTY', '5min', 'UNKNOWN', 90.),
    ('BANKNIFTY', '15min', 'VOLATILE', 85.),
    ('SENSEX', '15min', 'RANGING', 85.),
    ('MIDCPNIFTY', '1min', 'UNKNOWN', 96.5),
    ('MIDCPNIFTY', '5min', 'UNKNOWN', 93.),
    ('MIDCPNIFTY', '15min', 'VOLATILE', 90.),
])
def test_cap_profile_survives_observed_high_lock(instrument, timeframe, regime, initial):
    result = replay([(100, 120, 99, 105)], instrument=instrument,
                    timeframe=timeframe, regime=regime)
    assert result['stop'] == pytest.approx(initial)
    assert result['reason'] == 'UTS_PROFIT_LOCK' and result['exit'] == 105.


def test_chart_reason_does_not_overwrite_earlier_premium_lock():
    result = replay([(100, 120, 99, 105)], chart_reason='SMART_EXIT')
    assert result['reason'] == 'UTS_PROFIT_LOCK'


def test_chart_reason_keeps_terminal_close_without_premium_lock():
    result = replay([(100, 109, 99, 105)], chart_reason='SMART_EXIT')
    assert result['reason'] == 'UTS_SMART_EXIT' and result['exit'] == 105.


def test_terminal_session_boundary_does_not_invent_chart_reason():
    frame = pd.DataFrame([(100, 109, 99, 105)], columns=['open', 'high', 'low', 'close'],
                         index=pd.DatetimeIndex(['2026-09-29 15:24']))
    result = SignalProcessor.__new__(SignalProcessor)._simulate_actual_option_exit(
        frame, frame.index[0], pd.Timestamp('2026-09-29 15:30'), 90., 3.,
        qty=65, replay_source_lane='UTS', tf15_instrument='NIFTY', tf15_timeframe='5min')
    assert result['exit_time'] == pd.Timestamp('2026-09-29 15:25')
    assert result['reason'] == 'HISTORICAL'  # Caller maps a capped day-end to SESSION_END.


def test_replay_trace_names_conservative_uts_ohlc_model():
    result = replay([(100, 120, 99, 105)])
    model = result['exit_trace'][0]['fill_model']
    assert 'new_uts_lock_cross_at_observed_close' in model
    assert 'raised_uts_lock_applies_next_bar' in model


def test_other_lanes_retain_original_ohlc_model_and_stop_logic():
    # S1 is replayed with its own lane exits since 2026-10-08 (replay_s1_native_exits);
    # the generic (QP) lane keeps the original OHLC stop/target model.
    result = replay([(100, 120, 99, 105), (105, 106, 84, 90)], lane='QP')
    assert result['reason'] == 'SL HIT' and result['exit'] == 90.
    assert 'new_uts_lock_cross_at_observed_close' not in result['exit_trace'][0]['fill_model']
