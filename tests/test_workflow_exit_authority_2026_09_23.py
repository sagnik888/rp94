"""Decision ownership regressions from the September 23 workflow audit.

Exercise the whole scanner monitor pass and manager update; mock only market inputs
and unrelated discretionary exit policies. No broker or persistence is instantiated.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pandas as pd
import pytest

from config.settings import get_settings
from scanner import Scanner
from trading.trade_manager import Trade, TradeManager


def _trade(direction="LONG", *, stop=90.0):
    return Trade(
        id="audit-option", instrument="NIFTY", timeframe="15min", direction=direction,
        inst_type="OPT", option_type="CE" if direction == "LONG" else "PE",
        entry_price=100.0, entry_spot=25_000.0,
        entry_time=datetime.now() - timedelta(seconds=30),
        trailing_stop=stop, initial_stop=stop, current_stop=stop,
        lots=1, lot_size=65, grade="A+", target=0.0,
        instrument_multiplier=0.5 if direction == "LONG" else -0.5,
        current_price=116.0, peak_pnl=1040.0, scalp_lock_mode=True,
    )


def _scanner(monkeypatch, trade, *, mark=116.0, spot=25_000.0):
    monkeypatch.setattr(get_settings(), "ut_intel_early_exit", False)
    sc = Scanner.__new__(Scanner)
    sc.mode, sc.auto_mode = "REAL", True
    sc.candles = NS(
        get_latest_price=lambda *_: spot,
        get_max_timestamp=lambda: datetime.now(),
        get_candles=lambda *_: pd.DataFrame(),
    )
    sc.mtf = NS(engines={})
    sc.trades = NS(open_trades={trade.id: trade}, update_trade=MagicMock())
    sc._reset_daily_counters = lambda: None
    sc._trade_contract_tick_path = lambda *_: []
    sc._commit_trade_contract_tick_path = lambda *_: None
    sc._get_live_premium_cached = lambda *a, **kw: mark
    sc._active_option_decision_mark = lambda *a: (mark, "contract_ltp", True)
    sc._get_trade_regime = lambda *_: "RANGING"
    sc._fifteen_min_one_min_support_exit_decision = lambda *a, **kw: (False, "")
    sc._maybe_arm_open_distress_reversion = lambda *a, **kw: None
    sc._trade_one_way_cascade_context = lambda *a: {}
    sc._adaptive_hold_step = lambda *a, **kw: NS(action="NONE")
    sc._trade_candle_extreme_price = lambda *a: 0.0
    sc._profit_lock_floor = lambda *a, **kw: (110.432, "SMART_PROFIT_LOCK", 0.652)
    sc._apply_option_stop_breathing_room = lambda t, stop, **kw: (stop, 0.0, False)
    sc._should_defer_trend_option_profit_lock = lambda *a, **kw: (False, "")
    sc._peak_giveback_cap_exit = lambda *a, **kw: (False, "")
    sc._loss_guard_decision = lambda *a, **kw: (False, "deferred", 0.8)
    sc._rupee_loss_band_guard_decision = lambda *a, **kw: (False, "", "")
    sc._peak_threat_exit_decision = lambda *a, **kw: (False, "", "")
    sc._smart_profit_retention_eligible = lambda **kw: False
    sc._peak_giveback_or_reversal_exit = lambda *a, **kw: (False, "", "")
    sc._eod_loss_minimize_exit = lambda *a, **kw: (False, "")
    sc._stagnation_exit_decision = lambda *a, **kw: (False, "")
    sc._loss_recovery_watch_decision = lambda *a, **kw: ("NONE", "")
    sc._major_win_guard_arm_rs = lambda *a: 100_000.0
    sc._close_and_record = MagicMock()
    sc.log_event = MagicMock()
    return sc


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_option_lock_does_not_exit_while_premium_is_above_floor(monkeypatch, direction):
    trade = _trade(direction)
    sc = _scanner(monkeypatch, trade)
    sc._update_active_trades()
    sc._close_and_record.assert_not_called()
    assert sc.trades.update_trade.call_args.args[2] == pytest.approx(110.432)


@pytest.mark.parametrize("direction,spot", [("LONG", 25_100.0), ("SHORT", 24_900.0)])
def test_premium_breach_exits_even_if_index_did_not_cross_converted_stop(monkeypatch, direction, spot):
    trade = _trade(direction)
    sc = _scanner(monkeypatch, trade, mark=109.0, spot=spot)
    sc._update_active_trades()
    assert sc._close_and_record.call_args.args == (trade.id, 109.0, "SMART_PROFIT_LOCK")


def test_tighter_existing_stop_is_not_mislabelled_as_profit_lock(monkeypatch):
    trade = _trade(stop=115.0)
    sc = _scanner(monkeypatch, trade, mark=114.0)
    sc._update_active_trades()
    sc._close_and_record.assert_not_called()
    # The existing premium stop remains authoritative at the manager's stop boundary.
    assert sc.trades.update_trade.call_args.args[2] == pytest.approx(115.0)


def test_structural_trail_keeps_manager_stop_authority(monkeypatch):
    trade = _trade()
    sc = _scanner(monkeypatch, trade, mark=114.0)
    sc.candles.get_candles = lambda *a: pd.DataFrame({
        "low": [25_030.0] * 3, "high": [25_040.0] * 3,
    })
    sc.trades = _manager(trade)
    sc._update_active_trades()
    sc._close_and_record.assert_not_called()
    assert sc.trades.close_trade.call_args.args == (trade.id, 114.0, "TRAILING_STOP")


def test_unverified_premium_cannot_trigger_or_tighten_lock(monkeypatch):
    trade = _trade()
    sc = _scanner(monkeypatch, trade, mark=105.0)
    sc._active_option_decision_mark = lambda *a: (105.0, "synthetic", False)
    sc._update_active_trades()
    sc._close_and_record.assert_not_called()
    assert sc.trades.update_trade.call_args.args[2] == 90.0
    assert sc.trades.update_trade.call_args.kwargs["quote_verified"] is False


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
@pytest.mark.parametrize("existing_stop,expected", [(90.0, 103.0), (108.0, 108.0)])
def test_adaptive_hold_owns_this_cycle_trail_without_loosening_prior_stop(
    monkeypatch, direction, existing_stop, expected
):
    trade = _trade(direction, stop=existing_stop)
    sc = _scanner(monkeypatch, trade)
    sc._adaptive_hold_step = lambda *a, **kw: NS(action="HOLD", stop=103.0)
    sc.candles.get_candles = lambda *a: pd.DataFrame({
        "low": [25_030.0 if direction == "LONG" else 24_960.0] * 3,
        "high": [25_040.0 if direction == "LONG" else 24_970.0] * 3,
    })
    sc._update_active_trades()
    sc._close_and_record.assert_not_called()
    assert sc.trades.update_trade.call_args.args[2] == pytest.approx(expected)


def test_scanner_passes_only_current_cycle_loss_guard_verdict(monkeypatch):
    trade = _trade()
    sc = _scanner(monkeypatch, trade)
    sc._update_active_trades()
    assert sc.trades.update_trade.call_args.kwargs["early_loss_cut_decision"] is False

    def failed_evaluation(*a, **kw):
        raise RuntimeError("temporary intelligence failure")

    sc._loss_guard_decision = failed_evaluation
    sc._update_active_trades()
    assert sc.trades.update_trade.call_args.kwargs["early_loss_cut_decision"] is None


def _manager(trade):
    tm = TradeManager.__new__(TradeManager)
    tm.open_trades = {trade.id: trade}
    tm.mode = "HISTORICAL"
    tm._last_mark_to_market_save = 0.0
    tm._sample_pnl_path = lambda *a: None
    tm._record_trade_forensics = lambda *a: None
    tm._mark_dirty = lambda: None
    tm.close_trade = MagicMock()
    return tm


@pytest.mark.parametrize("peak,risk,pnl", [(0.0, 80.0, -75.0), (900.0, 1000.0, -900.0)])
def test_scanner_deferral_reaches_manager_without_weakening_hard_stop(monkeypatch, peak, risk, pnl):
    trade = _trade(stop=100.0 - risk / 65)
    trade.peak_pnl = peak
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc._trade_like_age_seconds = lambda *a: 30.0
    sc._round_trip_cost_rs = lambda *a: 80.0
    sc._profit_lock_min_peak = lambda *a, **kw: 200.0
    decision, _, _ = sc._loss_guard_decision(
        trade, current_pnl_rs=pnl, initial_risk_rs=risk, latest_time=datetime.now()
    )
    assert decision is False
    tm = _manager(trade)
    trade.current_price = 100.0 + pnl / 65
    assert tm._early_loss_cut_hit(trade) is True
    tm.update_trade(
        trade.id, 25_000.0, real_premium=100.0 + pnl / 65, quote_verified=True,
        early_loss_cut_decision=decision,
    )
    tm.close_trade.assert_not_called()
    tm.update_trade(
        trade.id, 25_000.0, real_premium=trade.current_stop - 1.0, quote_verified=True,
        early_loss_cut_decision=decision,
    )
    assert tm.close_trade.call_args.args[2] == "TRAILING_STOP"


def test_loss_guard_verdict_is_not_reused_by_next_manager_call():
    trade = _trade()
    trade.peak_pnl = 0.0
    tm = _manager(trade)
    tm.update_trade(
        trade.id, 25_000.0, real_premium=91.0, quote_verified=True,
        early_loss_cut_decision=False,
    )
    tm.close_trade.assert_not_called()
    tm.update_trade(trade.id, 25_000.0, real_premium=91.0, quote_verified=True)
    assert tm.close_trade.call_args.args[2] == "EARLY_LOSS_CUT"


def test_absent_cycle_verdict_does_not_reuse_stale_acceleration():
    trade = _trade()
    trade.loss_guard_cut_r = 0.35
    tm = _manager(trade)
    tm.update_trade(trade.id, 25_000.0, real_premium=95.0, quote_verified=True)
    tm.close_trade.assert_not_called()


def test_deferred_loss_guard_cannot_mask_observed_stop_touch():
    trade = _trade()
    tm = _manager(trade)
    tm.update_trade(
        trade.id, 25_000.0, real_premium=99.0, quote_verified=True,
        early_loss_cut_decision=False,
        observed_contract_ticks=[(89.0, datetime.now()), (99.0, datetime.now())],
    )
    assert tm.close_trade.call_args.args == (trade.id, 89.0, "TRAILING_STOP")


@pytest.mark.parametrize("direction,mark,stop,lock", [("LONG", 109.0, 90.0, 110.0), ("SHORT", 91.0, 110.0, 90.0)])
def test_futures_profit_lock_keeps_existing_contract_space_behavior(monkeypatch, direction, mark, stop, lock):
    trade = _trade(direction, stop=stop)
    trade.inst_type = "FUT"
    trade.entry_spot = 0.0
    trade.instrument_multiplier = 1.0
    sc = _scanner(monkeypatch, trade, spot=mark)
    sc._get_live_fut_ltp_cached = lambda *a, **kw: mark
    sc._profit_lock_floor = lambda *a, **kw: (lock, "SMART_PROFIT_LOCK", 0.65)
    sc._update_active_trades()
    assert sc._close_and_record.call_args.args == (trade.id, lock, "SMART_PROFIT_LOCK")
