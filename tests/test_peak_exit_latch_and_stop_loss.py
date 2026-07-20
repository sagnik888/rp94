"""Fixes: PEAK_THREAT one-shot latch + Trade.stop_loss alias."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trading.trade_manager import Trade


def test_trade_stop_loss_alias_reads_current_stop():
    t = Trade(
        id="t1",
        instrument="NIFTY",
        timeframe="15min",
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime.now(),
        trailing_stop=95.0,
        current_stop=96.0,
        lots=1,
        lot_size=25,
        grade="A",
    )
    assert t.stop_loss == pytest.approx(96.0)
    t.current_stop = 0.0
    assert t.stop_loss == pytest.approx(95.0)  # falls back to trailing


def test_trade_stop_loss_setter_updates_current_stop():
    t = Trade(
        id="t1",
        instrument="NIFTY",
        timeframe="15min",
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime.now(),
        trailing_stop=0.0,
        current_stop=0.0,
        lots=1,
        lot_size=25,
        grade="A",
    )
    t.stop_loss = 97.5
    assert t.current_stop == pytest.approx(97.5)
    assert t.trailing_stop == pytest.approx(97.5)


def test_trade_quantity_is_lots_times_lot_size():
    t = Trade(
        id="t1",
        instrument="NIFTY",
        timeframe="15min",
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime.now(),
        trailing_stop=95.0,
        current_stop=95.0,
        lots=2,
        lot_size=25,
        grade="A",
    )
    assert t.quantity == 50


def test_manual_exit_latches_pending_exit_no_resend():
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    sc.auto_mode = False
    sc.mode = "HISTORICAL"
    sc._reset_daily_counters = lambda: None
    sc._close_and_record_internal = MagicMock()

    trade = Trade(
        id="n1",
        instrument="NIFTY",
        timeframe="15min",
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime.now(),
        trailing_stop=90.0,
        current_stop=90.0,
        lots=1,
        lot_size=25,
        grade="A",
        target=120.0,
        rr_ratio=1.5,
    )
    sc.trades = SimpleNamespace(open_trades={"n1": trade}, emergency_flatten_trade=MagicMock())

    # First call: latch + log (notification may fail without loop — still latches)
    sc._close_and_record("n1", 110.0, "PEAK_THREAT_EXIT")
    assert trade.pending_exit is True
    assert trade.exit_reason == "PEAK_THREAT_EXIT"
    assert str(getattr(trade, "execution_status", "")).upper() == "EXIT_PENDING"
    sc._close_and_record_internal.assert_not_called()

    # Second call same reason: no re-process
    sc._close_and_record("n1", 111.0, "PEAK_THREAT_EXIT")
    sc._close_and_record_internal.assert_not_called()

    # Different reason after latch: still blocked (one-shot for any exit)
    sc._close_and_record("n1", 112.0, "MAJOR_WIN_GUARD")
    sc._close_and_record_internal.assert_not_called()
    assert trade.exit_reason == "PEAK_THREAT_EXIT"  # first reason kept


def test_update_active_trades_skips_pending_exit():
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    sc.auto_mode = False
    sc.mode = "HISTORICAL"
    sc._reset_daily_counters = lambda: None
    sc.candles = SimpleNamespace(get_latest_price=lambda *_: 100.0)
    trade = Trade(
        id="n1",
        instrument="NIFTY",
        timeframe="15min",
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime.now(),
        trailing_stop=90.0,
        current_stop=90.0,
        lots=1,
        lot_size=25,
        grade="A",
        pending_exit=True,
        exit_reason="PEAK_THREAT_EXIT",
        execution_status="EXIT_PENDING",
    )
    sc.trades = SimpleNamespace(open_trades={"n1": trade})
    # Should not raise and should not touch trade price if skipped early
    trade.current_price = 0.0
    sc._update_active_trades()
    assert trade.current_price == 0.0  # skipped before price update
