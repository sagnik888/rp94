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
