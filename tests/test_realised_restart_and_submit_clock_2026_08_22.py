"""Regression coverage for restart-safe realised deltas and the entry submit clock."""

from datetime import datetime, timedelta
from types import SimpleNamespace
import threading

import pytest

from data.sqlite_db import TRADE_PERSISTED_COLUMNS, db
from trading.trade_manager import IST, Trade, TradeManager


SYMBOL = "NIFTY2682524800CE"


def _trade(**overrides) -> Trade:
    values = {
        "id": "T0001_NIFTY_08221000",
        "instrument": "NIFTY",
        "timeframe": "5min",
        "direction": "LONG",
        "entry_price": 100.0,
        "entry_time": datetime.now(IST) - timedelta(minutes=20),
        "trailing_stop": 85.0,
        "current_stop": 85.0,
        "lots": 1,
        "lot_size": 65,
        "grade": "A",
        "inst_type": "OPT",
        "status": "CLOSED",
        "trading_symbol": SYMBOL,
        "symbol_token": "12345",
        "exit_time": datetime.now(IST) - timedelta(minutes=10),
        "exit_price": 102.0,
        "broker_order_id": "ENTRY-1",
        "broker_quantity": 65,
    }
    values.update(overrides)
    return Trade(**values)


def test_latest_same_day_cumulative_total_survives_trade_round_trip():
    first = _trade(
        id="T1",
        exit_time=datetime.now(IST) - timedelta(minutes=15),
        broker_realised_cumulative=120.0,
    )
    # Cumulative realised can decrease after a losing second trade. Latest-by-time,
    # rather than max(value), is therefore the only correct restore rule.
    latest = _trade(
        id="T2",
        exit_time=datetime.now(IST) - timedelta(minutes=5),
        broker_realised_cumulative=75.0,
    )
    restored_latest = Trade.from_dict(latest.to_dict())

    baseline = TradeManager._realised_baseline_from_closed_trades(
        [restored_latest, first],
        datetime.now(IST).date(),
    )

    assert restored_latest.broker_realised_cumulative == pytest.approx(75.0)
    assert baseline == {SYMBOL: pytest.approx(75.0)}


def test_legacy_same_day_rows_infer_the_consumed_gross_delta():
    older = _trade(id="OLD-1", pnl=91.0, charges=9.0)
    newer = _trade(id="OLD-2", pnl=-44.0, charges=4.0)
    prior_day = _trade(
        id="YESTERDAY",
        exit_time=datetime.now(IST) - timedelta(days=1),
        pnl=900.0,
        charges=100.0,
    )

    baseline = TradeManager._realised_baseline_from_closed_trades(
        [older, newer, prior_day],
        datetime.now(IST).date(),
    )

    assert baseline[SYMBOL] == pytest.approx(60.0)


def test_a_legacy_row_after_an_exact_observation_advances_that_baseline():
    exact = _trade(
        id="EXACT",
        exit_time=datetime.now(IST) - timedelta(minutes=15),
        broker_realised_cumulative=100.0,
    )
    later_without_broker_total = _trade(
        id="LEGACY-LATER",
        exit_time=datetime.now(IST) - timedelta(minutes=5),
        broker_realised_cumulative=None,
        pnl=41.0,
        charges=9.0,
    )

    baseline = TradeManager._realised_baseline_from_closed_trades(
        [later_without_broker_total, exact],
        datetime.now(IST).date(),
    )

    assert baseline[SYMBOL] == pytest.approx(150.0)


def test_restarted_baseline_is_subtracted_from_the_next_kite_cumulative_total():
    prior = _trade(broker_realised_cumulative=100.0)
    current = _trade(
        id="T-NEXT",
        status="OPEN",
        exit_time=None,
        broker_realised_cumulative=None,
    )
    manager = TradeManager.__new__(TradeManager)
    manager.broker = SimpleNamespace()
    manager.open_trades = {current.id: current}
    manager.closed_trades = [prior]
    manager.orphan_positions = {}
    manager._lock = threading.RLock()
    manager._realised_booked_today = manager._realised_baseline_from_closed_trades(
        manager.closed_trades,
        datetime.now(IST).date(),
    )
    manager._flatten_ambiguous_latched = set()
    manager._ledger_owned_quantity = lambda trade_id: None
    manager._mark_dirty = lambda: None
    captured = {}

    def finalize(trade_id, trade, exit_price, reason, **kwargs):
        captured.update(kwargs)
        return True

    manager._finalize_closed_trade = finalize
    result = manager.reconcile_broker_positions(
        {
            "data": [
                {
                    "tradingsymbol": SYMBOL,
                    "netqty": 0,
                    "buyavgprice": 100.0,
                    "sellavgprice": 102.0,
                    "realised": 150.0,
                }
            ]
        }
    )

    assert result["closed"] == 1
    assert captured["realised_pnl"] == pytest.approx(50.0)
    assert captured["broker_realised_cumulative"] == pytest.approx(150.0)
    assert manager._realised_booked_today[SYMBOL] == pytest.approx(150.0)


def test_load_state_rehydrates_the_same_day_baseline(monkeypatch):
    persisted = _trade(broker_realised_cumulative=-85.0).to_dict()
    manager = TradeManager.__new__(TradeManager)
    manager.open_trades = {}
    manager.closed_trades = []
    manager._trade_counter = 0
    manager._lock = threading.RLock()
    manager._mark_dirty = lambda: None
    monkeypatch.setattr(db, "load_open_trades", lambda: [])
    monkeypatch.setattr(db, "load_closed_trades", lambda: [persisted])

    manager.load_state()

    assert manager._realised_booked_today == {SYMBOL: pytest.approx(-85.0)}


def _entry_manager(broker) -> TradeManager:
    manager = TradeManager.__new__(TradeManager)
    manager.broker = broker
    manager.ui_logger = None
    manager.product_type = "CARRYFORWARD"
    manager.open_trades = {}
    manager.closed_trades = []
    manager.orphan_positions = {}
    manager._trade_counter = 0
    manager._lock = threading.RLock()
    manager.mode = "REAL"
    manager.is_warmup = False
    manager.can_open_trade = lambda is_recovery=False: (True, "")
    manager._same_contract_entry_blocked = lambda *args, **kwargs: (False, "")
    manager._entry_mark_admission = lambda **kwargs: (True, "")
    manager._entry_risk_admission = lambda **kwargs: (True, "")
    manager._entry_margin_verdict = lambda *args, **kwargs: None
    manager._live_orders_permitted = lambda: True
    manager.nudge_position_sync = lambda: None
    manager._mark_dirty = lambda: None
    return manager


def test_entry_submit_clock_exists_inside_the_first_broker_call(monkeypatch):
    seen = []
    holder = {}

    class InspectingBroker:
        def place_order(self, **kwargs):
            trade = next(iter(holder["manager"].open_trades.values()))
            seen.append(trade.entry_submitted_at)
            return "ENTRY-ACK-1"

    broker = InspectingBroker()
    manager = _entry_manager(broker)
    holder["manager"] = manager
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    trade = manager.open_trade(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        price=24800.0,
        trailing_stop=24750.0,
        lots=1,
        lot_size=65,
        grade="A",
        trading_symbol="NIFTY26AUGFUT",
        symbol_token="67890",
        inst_type="FUT",
    )

    assert trade is not None
    assert seen[0] is not None, "the submit clock was still empty inside place_order"
    assert trade.entry_submitted_at == seen[0], "the API-return path overwrote the first-attempt time"
    assert trade.execution_status == "ENTRY_SUBMITTED"


def test_realised_cumulative_field_is_in_the_sqlite_contract():
    assert "broker_realised_cumulative" in TRADE_PERSISTED_COLUMNS
    columns = {
        str(row["name"])
        for row in db.conn.execute("PRAGMA table_info(trades)").fetchall()
    }
    assert "broker_realised_cumulative" in columns
