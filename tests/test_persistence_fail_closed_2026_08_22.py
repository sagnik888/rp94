import sqlite3
import threading

import pytest

import trading.trade_manager as trade_manager_module
from data.sqlite_db import DatabaseManager
from trading.trade_manager import TradeManager


def _isolated_db(tmp_path):
    manager = object.__new__(DatabaseManager)
    manager._init_db(str(tmp_path / "trades.db"))
    return manager


def test_snapshot_write_raises_after_rollback_instead_of_reporting_success(tmp_path):
    manager = _isolated_db(tmp_path)
    try:
        with pytest.raises(sqlite3.Error):
            manager.save_trades([{"id": object(), "status": "OPEN"}])
        assert manager.load_all_trades() == []
    finally:
        manager.conn.close()


def test_trade_store_uses_wal_and_full_durability(tmp_path):
    manager = _isolated_db(tmp_path)
    try:
        assert manager.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert int(manager.conn.execute("PRAGMA synchronous").fetchone()[0]) == 2
        assert int(manager.conn.execute("PRAGMA busy_timeout").fetchone()[0]) >= 5000
    finally:
        manager.conn.close()


def test_proven_invalid_open_trade_is_quarantined_not_deleted(tmp_path):
    manager = _isolated_db(tmp_path)
    try:
        assert manager.save_trade({"id": "OLD", "status": "OPEN", "trading_symbol": "OLDCE"})
        assert manager.quarantine_open_trades(["OLD"], "not in current broker listing") == 1
        assert manager.load_open_trades() == []
        rows = manager.load_all_trades()
        assert len(rows) == 1
        assert rows[0]["status"] == "QUARANTINED"
        assert rows[0]["execution_status"] == "QUARANTINED"
        assert rows[0]["exit_reason"] == "DELISTED_CONTRACT_QUARANTINED"
    finally:
        manager.conn.close()


def test_trade_manager_propagates_checkpoint_failure_and_remains_dirty(monkeypatch):
    class _FailingDb:
        @staticmethod
        def save_trades(_rows):
            raise sqlite3.OperationalError("disk full")

    manager = object.__new__(TradeManager)
    manager._lock = threading.RLock()
    manager._dirty = threading.Event()
    manager.open_trades = {}
    manager.closed_trades = []
    monkeypatch.setattr(trade_manager_module, "db", _FailingDb())

    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        manager.save_state()

    assert manager._dirty.is_set(), "failed state must be retried by the writer"
