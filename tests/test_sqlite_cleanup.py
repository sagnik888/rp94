import sqlite3
import threading

import pytest

from data.sqlite_db import DatabaseManager


def _cleanup_manager():
    manager = object.__new__(DatabaseManager)
    manager.conn = sqlite3.connect(":memory:")
    manager.write_lock = threading.Lock()
    manager.conn.executescript(
        """
        CREATE TABLE session_signals (date TEXT);
        CREATE TABLE signal_decisions (date TEXT);
        CREATE TABLE trades (
            exit_timestamp TEXT,
            exit_time TEXT,
            status TEXT
        );
        INSERT INTO session_signals VALUES ('2000-01-01'), (date('now'));
        INSERT INTO signal_decisions VALUES ('2000-01-01'), (date('now'));
        INSERT INTO trades VALUES ('2000-01-01', NULL, 'CLOSED');
        INSERT INTO trades VALUES (datetime('now'), NULL, 'CLOSED');
        INSERT INTO trades VALUES ('2000-01-01', NULL, 'OPEN');
        """
    )
    return manager


def test_cleanup_old_records_keeps_recent_and_open_rows():
    manager = _cleanup_manager()

    manager.cleanup_old_records(30)

    assert manager.conn.execute("SELECT COUNT(*) FROM session_signals").fetchone()[0] == 1
    assert manager.conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 1
    assert manager.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2


def test_cleanup_old_records_rejects_non_numeric_retention():
    manager = _cleanup_manager()

    with pytest.raises(ValueError):
        manager.cleanup_old_records("30'); DROP TABLE trades; --")

    assert manager.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3
