"""Restart hygiene: stale ghost trades must not rehydrate as RECOVERY_REQUIRED.

2026-07-10 restart replayed 111 previous-session ghost/shadow rows (engine-lab
and test artifacts, entry_price=100, no trading symbol) into open_trades as
RECOVERY_REQUIRED — ghosts never had broker orders, so "reconciliation" can
never resolve them and the exit engine spammed PEAK_THREAT/CRITICAL alerts in
a loop. Ghosts from a previous session now close as STALE_SESSION_END on load;
real previous-session trades keep the reconciliation path; the scanner's
active-trade loop skips RECOVERY_REQUIRED rows entirely.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytz

from trading import trade_manager as tm_module
from trading.trade_manager import TradeManager

IST = pytz.timezone("Asia/Kolkata")


def _record(tid: str, *, is_ghost: bool, days_ago: int, exec_status: str = "OPEN"):
    entry = datetime.now(IST) - timedelta(days=days_ago)
    return {
        "id": tid,
        "instrument": "NIFTY",
        "timeframe": "5min",
        "direction": "LONG",
        "inst_type": "FUT",
        "entry_price": 100.0,
        "current_price": 100.0,
        "lots": 1,
        "lot_size": 10,
        "status": "OPEN",
        "execution_status": exec_status,
        "is_ghost": is_ghost,
        "entry_timestamp": entry.timestamp(),
    }


def _manager(monkeypatch, open_records):
    manager = TradeManager.__new__(TradeManager)
    manager.open_trades = {}
    manager.closed_trades = []
    manager._trade_counter = 0
    monkeypatch.setattr(tm_module.db, "load_open_trades", lambda: open_records)
    monkeypatch.setattr(tm_module.db, "load_closed_trades", lambda: [])
    monkeypatch.setattr(tm_module.db, "save_trades", lambda rows: None)
    import threading

    manager._lock = threading.RLock()
    return manager


def test_previous_session_ghost_closes_as_stale_on_load(monkeypatch):
    manager = _manager(
        monkeypatch,
        [
            _record("G1", is_ghost=True, days_ago=1),
            _record("G2", is_ghost=True, days_ago=1, exec_status="EXIT_PENDING"),
        ],
    )
    manager.load_state()
    assert manager.open_trades == {}
    assert len(manager.closed_trades) == 2
    for t in manager.closed_trades:
        assert t.status == "CLOSED"
        assert t.exit_reason == "STALE_SESSION_END"
        assert t.pending_exit is False


def test_previous_session_real_trade_still_requires_reconciliation(monkeypatch):
    manager = _manager(monkeypatch, [_record("R1", is_ghost=False, days_ago=1)])
    manager.load_state()
    assert "R1" in manager.open_trades
    assert manager.open_trades["R1"].execution_status == "RECOVERY_REQUIRED"
    assert manager.closed_trades == []


def test_same_day_ghost_stays_open(monkeypatch):
    manager = _manager(monkeypatch, [_record("G3", is_ghost=True, days_ago=0)])
    manager.load_state()
    assert "G3" in manager.open_trades
    assert manager.open_trades["G3"].execution_status == "OPEN"


def test_active_trade_loop_skips_recovery_required():
    """The scanner never re-fires strategy exits on RECOVERY_REQUIRED rows."""
    from scanner import Scanner

    src = inspect.getsource(Scanner._update_active_trades)
    skip_block = src.split("continue")[1] if "continue" in src else src
    assert "RECOVERY_REQUIRED" in src.split("price = self.candles")[0]
