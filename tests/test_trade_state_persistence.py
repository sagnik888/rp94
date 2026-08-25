import sqlite3
import threading
from datetime import datetime

from data.sqlite_db import DatabaseManager
from trading.trade_manager import IST, Trade


def test_entry_submission_timestamp_survives_sqlite_round_trip():
    manager = object.__new__(DatabaseManager)
    manager.conn = sqlite3.connect(":memory:")
    manager.conn.row_factory = sqlite3.Row
    manager.write_lock = threading.Lock()
    manager._create_tables()

    submitted_at = IST.localize(datetime(2026, 6, 15, 10, 1, 2))
    trade = Trade(
        id="T0001_NIFTY",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        entry_time=IST.localize(datetime(2026, 6, 15, 10, 0)),
        trailing_stop=95.0,
        current_stop=99.0,
        initial_stop=95.0,
        lots=1,
        lot_size=10,
        grade="A",
        status="OPEN",
        pending_execution=True,
        execution_status="ENTRY_SUBMITTED",
        entry_submitted_at=submitted_at,
        market_regime="TRENDING_UP",
        data_provenance="LIVE",
        price_action_verdict="FAVORABLE",
        intelligence_score=0.42,
        strategy_revision="ut1-test",
        strategy_direction_mode="LONG_SHORT",
        scalp_lock_mode=True,
        source_lane="B",
    )

    manager.save_trade(trade.to_dict())
    restored = Trade.from_dict(manager.load_open_trades()[0])

    assert restored.entry_submitted_at == submitted_at
    assert restored.trailing_stop == 95.0
    assert restored.current_stop == 99.0
    assert restored.initial_stop == 95.0
    assert restored.scalp_lock_mode is True
    assert restored.market_regime == "TRENDING_UP"
    assert restored.data_provenance == "LIVE"
    assert restored.price_action_verdict == "FAVORABLE"
    assert restored.intelligence_score == 0.42
    assert restored.strategy_revision == "ut1-test"
    assert restored.strategy_direction_mode == "LONG_SHORT"
    assert restored.source_lane == "B"


def test_runner_semantics_and_provenance_survive_sqlite_round_trip():
    """A restart must not silently re-enable the fixed target on a runner."""
    manager = object.__new__(DatabaseManager)
    manager.conn = sqlite3.connect(":memory:")
    manager.conn.row_factory = sqlite3.Row
    manager.write_lock = threading.Lock()
    manager._create_tables()

    activated_at = IST.localize(datetime(2026, 8, 25, 11, 7, 9))
    trade = Trade(
        id="T0042_NIFTY",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        entry_time=IST.localize(datetime(2026, 8, 25, 11, 0)),
        trailing_stop=96.0,
        current_stop=112.0,
        initial_stop=96.0,
        lots=1,
        lot_size=65,
        grade="A",
        status="OPEN",
        lifecycle_id="life-42",
        runner_mode=True,
        runner_activation_timestamp=activated_at,
        runner_activation_reason="TARGET_REACHED_TREND_CONFIRMED",
        runner_stop=112.0,
        runner_stop_basis="INDEX_SPOT",
        stop_provenance="runner",
    )

    manager.save_trade(trade.to_dict())
    restored = Trade.from_dict(manager.load_open_trades()[0])

    assert restored.lifecycle_id == "life-42"
    assert restored.runner_mode is True
    assert restored.runner_activation_timestamp == activated_at
    assert restored.runner_activation_reason == "TARGET_REACHED_TREND_CONFIRMED"
    assert restored.runner_stop == 112.0
    assert restored.runner_stop_basis == "INDEX_SPOT"
    assert restored.stop_provenance == "runner"
