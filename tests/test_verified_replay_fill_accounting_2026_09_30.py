"""Verified option replay fills must survive the legacy dashboard adapter."""
from datetime import timedelta
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from engine.signal_processor import SignalProcessor
from engine.trade_row_adapter import accounting_pnl, hygiene_closed_rows, reconcile_legacy_trade_row
from trading.trade_dashboard import _serialize_trade_row
from trading.trade_manager import Trade


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    value = NS(uts_lane_enabled=True, options_sl_pct=15., opt_cost=80., fut_cost=200.,
               ut_force_exit_time='15:25', uts_lane_lock_arm_r=1.,
               uts_lane_lock_giveback_pct=30., replay_s1_native_exits=False)
    monkeypatch.setattr('config.settings.get_settings', lambda: value)
    monkeypatch.setattr('engine.signal_processor.get_settings', lambda: value)
    monkeypatch.setattr('engine.trade_row_adapter.get_settings', lambda: value)
    return value


def helper_trade(rows, *, lane='UTS', stored_charges=91.07):
    frame = pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'],
                         index=pd.date_range('2026-09-29 10:00', periods=len(rows), freq='1min'))
    result = SignalProcessor.__new__(SignalProcessor)._simulate_actual_option_exit(
        frame, frame.index[0], frame.index[-1] + timedelta(minutes=1), 90., 3.,
        qty=65, replay_source_lane=lane, live_exits=False,
        tf15_instrument='NIFTY', tf15_timeframe='5min',
        replay_setup=NS(instrument='NIFTY', timeframe='5min', direction='LONG'))
    assert result is not None
    trade = Trade(
        id='H_VERIFIED', instrument='NIFTY', timeframe='5min', direction='LONG',
        entry_price=result['entry'], entry_time=result['entry_time'],
        trailing_stop=result['stop'], current_stop=result['stop'], initial_stop=result['stop'],
        lots=1, lot_size=65, grade='C (Hist)', inst_type='OPT', status='CLOSED',
        exit_price=result['exit'], exit_time=result['exit_time'], exit_reason=result['reason'],
        target=999., data_provenance='BACKTEST', charges=stored_charges,
        pnl=round((result['exit'] - result['entry']) * 65 - stored_charges, 2),
        replay_exit_trace=result['exit_trace'], replay_price_path=result['price_path'])
    return result, Trade.from_dict(trade.to_dict())


@pytest.mark.parametrize('rows,lane,expected_exit,expected_reason', [
    ([(100, 120, 99, 116), (116, 117, 113, 115)], 'UTS', 114., 'SL HIT'),
    ([(100, 109, 99, 105), (80, 90, 79, 85)], 'UTS', 80., 'SL HIT'),
    ([(100, 140, 99, 135)], 'S1', 130., 'TARGET_HIT'),
])
@pytest.mark.parametrize('serialized_shape', ['persisted_trace', 'dashboard_input'])
def test_actual_helper_fill_survives_trade_roundtrip_and_adapter(
        rows, lane, expected_exit, expected_reason, serialized_shape):
    result, trade = helper_trade(rows, lane=lane)
    assert result['reason'] == expected_reason
    assert result['exit'] == pytest.approx(expected_exit)
    row = (trade.to_dict() if serialized_shape == 'persisted_trace'
           else _serialize_trade_row(NS(options_sl_pct=15.), trade))
    original = dict(row)
    fixed = reconcile_legacy_trade_row(row)
    assert fixed['exit_price'] == pytest.approx(result['exit'])
    assert fixed['current_price'] == pytest.approx(result['exit'])
    assert fixed['pnl'] == pytest.approx((result['exit'] - result['entry']) * 65 - 91.07)
    assert fixed['charges'] == 91.07
    assert row == original


@pytest.mark.parametrize('evidence,provenance,inst_type', [
    ({'guard': 'replay_input', 'state': 'SYNTHETIC_OPTION_ESTIMATE'}, 'BACKTEST', 'OPT'),
    ({'guard': 'replay_input', 'state': 'VERIFIED_PRICE_PATH'}, 'LIVE', 'OPT'),
    ({'guard': 'replay_input', 'state': 'VERIFIED_PRICE_PATH'}, 'BACKTEST', 'FUT'),
    ({'guard': 'SL HIT', 'state': 'VERIFIED_PRICE_PATH'}, 'BACKTEST', 'OPT'),
])
def test_unverified_legacy_stop_fallback_is_unchanged(evidence, provenance, inst_type):
    row = dict(inst_type=inst_type, direction='LONG', entry_price=100., exit_price=80.,
               current_stop=90., exit_reason='SL HIT', lots=1, lot_size=65,
               data_provenance=provenance, replay_input=evidence, pnl=777., charges=91.07)
    repaired = reconcile_legacy_trade_row(row)
    assert repaired['exit_price'] == 90.
    assert repaired['pnl'] == (-650. - (80. if inst_type == 'OPT' else 200.))


def verified_row(**overrides):
    return dict(inst_type='OPT', entry_price=100., exit_price=80., current_stop=90.,
                exit_reason='SL HIT', lots=1, lot_size=65, data_provenance='BACKTEST',
                replay_input={'guard': 'replay_input', 'state': 'VERIFIED_PRICE_PATH'},
                **overrides)


def test_missing_pnl_uses_observed_fill_and_recorded_charges():
    row = reconcile_legacy_trade_row(verified_row(charges=91.07))
    assert row['exit_price'] == 80.
    assert row['pnl'] == -1391.07
    assert row['charges'] == 91.07


def test_missing_pnl_and_charges_uses_legacy_flat_cost_without_changing_fill():
    row = reconcile_legacy_trade_row(verified_row())
    assert row['exit_price'] == 80.
    assert row['pnl'] == -1380.
    assert row['charges'] == 80.


def test_zero_recorded_pnl_and_charges_are_preserved():
    row = reconcile_legacy_trade_row(verified_row(pnl=0., charges=0.))
    assert row['pnl'] == 0. and row['charges'] == 0.
    assert row['exit_price'] == 80.


def test_missing_charges_does_not_change_recorded_pnl():
    row = reconcile_legacy_trade_row(verified_row(pnl=-1391.07))
    assert row['pnl'] == -1391.07 and row['exit_price'] == 80.


def test_headline_hygiene_uses_the_same_preserved_replay_accounting_as_rows():
    rows = [verified_row(id='H_ONE', pnl=-1391.07, charges=91.07),
            {**verified_row(id='H_TWO', pnl=808.93, charges=91.07), 'exit_price': 114.}]
    repaired = [reconcile_legacy_trade_row(row) for row in rows]
    headline, _ = hygiene_closed_rows(repaired)
    assert len(headline) == 2
    assert sum(accounting_pnl(row) for row in headline) == pytest.approx(-582.14)


@pytest.mark.parametrize('provenance,expected', [('BACKTEST', 818.93),
                                              ('ENGINE_REPLAY', 818.93), ('LIVE', 818.93)])
def test_paise_survive_both_serializers_for_replay_and_live(provenance, expected):
    # The October 1 live-fill audit also preserves paise for LIVE rows.
    _, trade = helper_trade([(100, 120, 99, 116), (116, 117, 113, 115)])
    trade.data_provenance = provenance
    assert trade.to_dict()['pnl'] == expected
    assert _serialize_trade_row(NS(options_sl_pct=15.), trade)['pnl'] == expected
