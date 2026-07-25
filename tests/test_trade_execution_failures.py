import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json

import pytest

from trading.order_retry import BrokerOrderAmbiguous, execute_order_with_retry
from trading.trade_manager import IST, TradeManager


INDICES = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr("trading.order_retry.time.sleep", lambda seconds: None)


class ScriptedBroker:
    def __init__(self, entry_failures=(), exit_failures=(), entry_ambiguous=(), exit_ambiguous=()):
        self.entry_failures = set(entry_failures)
        self.exit_failures = set(exit_failures)
        self.entry_ambiguous = set(entry_ambiguous)
        self.exit_ambiguous = set(exit_ambiguous)
        self.entry_calls = []
        self.exit_calls = []
        self.protective_calls = []
        self.cancel_calls = []
        self._lock = threading.Lock()

    def place_order(self, symbol, token, qty, side, product_type, price=0.0):
        with self._lock:
            self.entry_calls.append((symbol, qty, side))
        if symbol in self.entry_ambiguous:
            raise BrokerOrderAmbiguous("timeout after broker submission")
        if symbol in self.entry_failures:
            return None
        return f"ENTRY-{symbol}"

    def close_order(self, symbol, token, qty, side, product_type, price=0.0):
        with self._lock:
            self.exit_calls.append((symbol, qty, side))
        if symbol in self.exit_ambiguous:
            raise BrokerOrderAmbiguous("timeout after exit submission")
        if symbol in self.exit_failures:
            return None
        return f"EXIT-{symbol}"

    def get_positions(self):
        return {"data": []}

    def get_order_status(self, order_id):
        return None

    def cancel_order(self, order_id, variety="NORMAL"):
        self.cancel_calls.append((order_id, variety))
        return True

    def place_protective_stop(self, symbol, token, qty, side, trigger_price, product_type):
        self.protective_calls.append(("place", symbol, qty, side, trigger_price))
        return f"SL-{symbol}"

    def modify_protective_stop(
        self, order_id, symbol, token, qty, side, trigger_price, product_type
    ):
        self.protective_calls.append(("modify", symbol, qty, side, trigger_price))
        return True


def _manager(broker, max_positions=4):
    manager = TradeManager.__new__(TradeManager)
    manager.broker = broker
    manager.ui_logger = None
    manager.max_positions = max_positions
    manager.session_end = manager._parse_time("15:25")
    manager.product_type = "CARRYFORWARD"
    manager.open_trades = {}
    manager.closed_trades = []
    manager._trade_counter = 0
    manager._lock = threading.RLock()
    manager._protection_lock = threading.RLock()
    manager._state_file = ""
    manager._last_mark_to_market_save = 0.0
    manager.orphan_positions = {}
    manager.futures_sl_pct = 0.3
    manager.options_sl_pct = 15.0
    manager.mode = "REAL"
    manager.is_warmup = False
    manager.save_state = lambda: None
    return manager


def _open(manager, instrument):
    return manager.open_trade(
        instrument=instrument,
        timeframe="5min",
        direction="LONG",
        price=100.0,
        trailing_stop=95.0,
        lots=1,
        lot_size=10,
        grade="A",
        target=110.0,
        trading_symbol=f"{instrument}TESTFUT",
        symbol_token=f"TOKEN-{instrument}",
        inst_type="FUT",
        entry_time=IST.localize(datetime(2026, 6, 15, 10, 0)),
    )


def test_open_trade_accepts_explosive_bypass_compatibility_flag(monkeypatch):
    broker = ScriptedBroker()
    manager = _manager(broker)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    trade = manager.open_trade(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        price=100.0,
        trailing_stop=95.0,
        lots=1,
        lot_size=10,
        grade="A",
        target=110.0,
        trading_symbol="NIFTYTESTFUT",
        symbol_token="TOKEN-NIFTY",
        inst_type="FUT",
        is_explosive_bypass=True,
        entry_time=IST.localize(datetime(2026, 6, 15, 10, 0)),
    )

    assert trade is not None
    assert broker.entry_calls == [("NIFTYTESTFUT", 10, "BUY")]


def test_ambiguous_broker_acknowledgement_does_not_retry(monkeypatch):
    calls = 0

    def ambiguous_order():
        nonlocal calls
        calls += 1
        raise BrokerOrderAmbiguous("timeout after submit")

    order_id, error, attempts = execute_order_with_retry(
        ambiguous_order,
        lambda result: str(result or ""),
        "Entry order NIFTYTESTFUT",
    )

    assert order_id == ""
    assert "timeout after submit" in error
    assert attempts == 1
    assert calls == 1


def test_ambiguous_entry_ack_keeps_trade_open_for_recovery(monkeypatch):
    symbol = "NIFTYTESTFUT"
    broker = ScriptedBroker(entry_ambiguous={symbol})
    manager = _manager(broker)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    trade = _open(manager, "NIFTY")

    assert trade.id in manager.open_trades
    assert manager.closed_trades == []
    assert trade.execution_status == "RECOVERY_REQUIRED"
    assert trade.pending_execution is True
    assert "ambiguous" in trade.execution_error.lower()
    assert len(broker.entry_calls) == 1


def _position_row(trade, qty=None):
    return {
        "tradingsymbol": trade.trading_symbol,
        "netqty": trade.quantity if qty is None else qty,
        "buyavgprice": trade.entry_price,
        "sellavgprice": 0,
        "realised": 0,
    }


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_concurrent_entries_isolate_failures_across_one_to_four_indices(monkeypatch, count):
    selected = INDICES[:count]
    failures = {selected[0]} if count == 1 else set(selected[1::2])
    broker = ScriptedBroker(entry_failures={f"{name}TESTFUT" for name in failures})
    manager = _manager(broker, max_positions=4)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        trades = list(pool.map(lambda name: _open(manager, name), selected))

    assert all(trade is not None for trade in trades)
    expected_calls = (count - len(failures)) + (len(failures) * 7)
    assert len(broker.entry_calls) == expected_calls
    assert len(manager.open_trades) == count - len(failures)
    assert len(manager.closed_trades) == len(failures)
    assert {trade.instrument for trade in manager.closed_trades} == failures
    assert all(trade.execution_status == "REJECTED" for trade in manager.closed_trades)
    assert all(trade.pending_execution for trade in manager.open_trades.values())
    assert all(
        trade.execution_status == "ENTRY_SUBMITTED"
        for trade in manager.open_trades.values()
    )

    rows = [_position_row(trade) for trade in manager.open_trades.values()]
    result = manager.reconcile_broker_positions({"data": rows})
    assert result["confirmed"] == count - len(failures)
    assert all(
        trade.execution_status == "OPEN" and not trade.pending_execution
        for trade in manager.open_trades.values()
    )


def test_submitted_entry_cannot_exit_before_fill_confirmation(monkeypatch):
    broker = ScriptedBroker()
    manager = _manager(broker)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    trade = _open(manager, "NIFTY")
    assert trade.execution_status == "ENTRY_SUBMITTED"
    assert trade.pending_execution is True
    assert manager.close_trade(trade.id, 99.0, "STOP_BEFORE_FILL") is False
    assert broker.exit_calls == []

    manager.reconcile_broker_positions({"data": [_position_row(trade)]})
    assert manager.close_trade(trade.id, 99.0, "STOP_AFTER_FILL") is True
    assert len(broker.exit_calls) == 1


def test_submitted_entry_close_reconciles_confirmed_broker_position(monkeypatch):
    broker = ScriptedBroker()
    manager = _manager(broker)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    trade = _open(manager, "NIFTY")
    broker.get_positions = lambda: {"data": [_position_row(trade)]}

    assert manager.close_trade(trade.id, 99.0, "STOP_DURING_FILL_SYNC") is True
    assert trade.execution_status == "EXIT_PENDING"
    assert broker.protective_calls
    assert broker.cancel_calls[-1] == ("SL-NIFTYTESTFUT", "STOPLOSS")
    assert broker.exit_calls == [("NIFTYTESTFUT", trade.quantity, "SELL")]


def test_reconciliation_rejects_opposite_side_broker_position(monkeypatch):
    broker = ScriptedBroker()
    manager = _manager(broker)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    trade = _open(manager, "NIFTY")
    result = manager.reconcile_broker_positions({
        "data": [{**_position_row(trade), "netqty": -trade.quantity}]
    })

    assert result["confirmed"] == 0
    assert trade.execution_status == "RECOVERY_REQUIRED"
    assert trade.pending_execution is True
    assert "direction mismatch" in trade.execution_error


def test_partial_entry_can_exit_confirmed_broker_quantity(monkeypatch):
    broker = ScriptedBroker()
    manager = _manager(broker)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    trade = _open(manager, "NIFTY")
    manager.reconcile_broker_positions({
        "data": [{**_position_row(trade), "netqty": trade.quantity - 1}]
    })

    assert trade.execution_status == "PARTIAL"
    assert trade.pending_execution is True
    assert trade.broker_quantity == trade.quantity - 1
    assert manager.close_trade(trade.id, 101.0, "PARTIAL_ENTRY_EXIT") is True
    assert broker.exit_calls == [("NIFTYTESTFUT", trade.quantity - 1, "SELL")]

    assert trade.execution_status == "EXIT_PENDING"
    assert trade.pending_exit is True


def test_reconciliation_accepts_large_legitimate_option_fill(monkeypatch):
    broker = ScriptedBroker()
    manager = _manager(broker)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    trade = manager.open_trade(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        price=50.0,
        trailing_stop=42.5,
        lots=1,
        lot_size=75,
        grade="A",
        target=150.0,
        trading_symbol="NIFTY26JUN24000CE",
        symbol_token="TOKEN-OPT",
        inst_type="OPT",
        option_type="CE",
        atm_strike=24000.0,
        entry_spot=24000.0,
        entry_time=IST.localize(datetime(2026, 6, 15, 10, 0)),
    )
    row = {
        "tradingsymbol": trade.trading_symbol,
        "netqty": trade.quantity,
        "buyavgprice": 250.0,
        "sellavgprice": 0.0,
        "realised": 0.0,
    }

    manager.reconcile_broker_positions({"data": [row]})

    assert trade.entry_price == 250.0


def test_unconfirmed_submitted_entry_requires_recovery(monkeypatch):
    broker = ScriptedBroker()
    manager = _manager(broker)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    trade = _open(manager, "NIFTY")
    trade.entry_submitted_at = datetime.now(IST) - timedelta(seconds=31)
    result = manager.reconcile_broker_positions({"data": []})

    assert result["confirmed"] == 0
    assert trade.id in manager.open_trades
    assert trade.execution_status == "RECOVERY_REQUIRED"
    assert trade.pending_execution is True
    assert "not confirmed" in trade.execution_error


def test_concurrent_entry_reservations_never_exceed_position_limit(monkeypatch):
    broker = ScriptedBroker()
    manager = _manager(broker, max_positions=3)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        trades = list(pool.map(lambda name: _open(manager, name), INDICES))

    assert sum(trade is not None for trade in trades) == 3
    assert len(manager.open_trades) == 3
    assert len(broker.entry_calls) == 3


def test_real_execution_queue_processes_four_orders_with_isolated_failures():
    from engine.async_queue import execution_queue

    failures = {"BANKNIFTYTESTFUT", "MIDCPNIFTYTESTFUT"}
    broker = ScriptedBroker(entry_failures=failures)
    manager = _manager(broker)

    trades = [_open(manager, name) for name in INDICES]
    execution_queue.task_queue.join()

    assert all(trade is not None for trade in trades)
    assert len(broker.entry_calls) == 16
    assert len(manager.open_trades) == 2
    assert len(manager.closed_trades) == 2
    assert {
        trade.trading_symbol for trade in manager.closed_trades
    } == failures


def test_exit_failures_remain_open_and_can_be_retried(monkeypatch):
    symbol = "NIFTYTESTFUT"
    broker = ScriptedBroker(exit_failures={symbol})
    manager = _manager(broker)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )
    monkeypatch.setattr("trading.trade_manager.time.sleep", lambda seconds: None)

    trade = _open(manager, "NIFTY")
    manager.reconcile_broker_positions({"data": [_position_row(trade)]})

    assert manager.close_trade(trade.id, 105.0, "MANUAL") is True
    assert trade.id in manager.open_trades
    assert trade.execution_status == "EXIT_FAILED"
    assert trade.pending_exit is False
    assert len(broker.exit_calls) == 7

    broker.exit_failures.clear()
    assert manager.close_trade(trade.id, 105.0, "MANUAL_RETRY") is True
    assert trade.execution_status == "EXIT_PENDING"
    manager.reconcile_broker_positions({
        "data": [{**_position_row(trade), "netqty": 0, "sellavgprice": 105.0}]
    })
    assert trade.id not in manager.open_trades
    assert trade.execution_status == "CLOSED"
    assert trade.exit_reason == "MANUAL_RETRY"


def test_ambiguous_exit_ack_blocks_replacement_until_reconciliation(monkeypatch):
    symbol = "NIFTYTESTFUT"
    broker = ScriptedBroker(exit_ambiguous={symbol})
    manager = _manager(broker)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    trade = _open(manager, "NIFTY")
    manager.reconcile_broker_positions({"data": [_position_row(trade)]})

    assert manager.close_trade(trade.id, 105.0, "MANUAL") is True
    assert trade.id in manager.open_trades
    assert trade.execution_status == "RECOVERY_REQUIRED"
    assert trade.pending_exit is True
    assert manager.close_trade(trade.id, 105.0, "MANUAL_RETRY") is False
    assert len(broker.exit_calls) == 1


def test_partial_exit_waits_for_broker_completion_instead_of_replacing_order(monkeypatch):
    broker = ScriptedBroker()
    manager = _manager(broker)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    trade = _open(manager, "BANKNIFTY")
    manager.reconcile_broker_positions({"data": [_position_row(trade)]})
    assert manager.close_trade(trade.id, 102.0, "TARGET_HIT") is True

    manager.reconcile_broker_positions({
        "data": [{**_position_row(trade), "netqty": 4}]
    })
    assert trade.execution_status == "PARTIAL_EXIT"
    assert trade.pending_exit is True
    assert trade.broker_quantity == 4

    assert manager.close_trade(trade.id, 102.0, "PARTIAL_EXIT_RETRY") is False
    assert len(broker.exit_calls) == 1


def test_four_simultaneous_exits_isolate_partial_failures(monkeypatch):
    failed = {"BANKNIFTYTESTFUT", "MIDCPNIFTYTESTFUT"}
    broker = ScriptedBroker(exit_failures=failed)
    manager = _manager(broker)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )
    monkeypatch.setattr("trading.trade_manager.time.sleep", lambda seconds: None)

    trades = [_open(manager, name) for name in INDICES]
    manager.reconcile_broker_positions({
        "data": [_position_row(trade) for trade in trades]
    })

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda trade: manager.close_trade(trade.id, 103.0, "KILL_ALL_TEST"),
            trades,
        ))
    assert results == [True, True, True, True]

    zero_rows = [
        {**_position_row(trade), "netqty": 0, "sellavgprice": 103.0}
        for trade in trades
        if trade.trading_symbol not in failed
    ]
    remaining_rows = [
        _position_row(trade)
        for trade in trades
        if trade.trading_symbol in failed
    ]
    manager.reconcile_broker_positions({"data": zero_rows + remaining_rows})

    assert len(manager.closed_trades) == 2
    assert len(manager.open_trades) == 2
    assert all(
        trade.execution_status == "EXIT_FAILED"
        for trade in manager.open_trades.values()
    )


def test_stale_queued_exit_does_not_sell_after_reconciliation_closed_trade(monkeypatch):
    broker = ScriptedBroker()
    manager = _manager(broker)
    queued = {}

    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: queued.update(
            {"func": func, "args": args, "kwargs": kwargs}
        ),
    )

    trade = _open(manager, "NIFTY")
    queued["func"](*queued["args"], **queued["kwargs"])
    manager.reconcile_broker_positions({"data": [_position_row(trade)]})
    queued.clear()

    assert manager.close_trade(trade.id, 105.0, "TARGET_HIT") is True
    assert trade.execution_status == "EXIT_PENDING"
    manager.reconcile_broker_positions({
        "data": [{**_position_row(trade), "netqty": 0, "sellavgprice": 105.0}]
    })

    assert trade.id not in manager.open_trades
    queued["func"](*queued["args"], **queued["kwargs"])
    assert broker.exit_calls == []


def test_real_trade_without_symbol_token_requires_recovery(monkeypatch):
    broker = ScriptedBroker()
    manager = _manager(broker)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda task_name, func, *args, **kwargs: func(*args, **kwargs),
    )

    trade = _open(manager, "NIFTY")
    manager.reconcile_broker_positions({"data": [_position_row(trade)]})
    trade.symbol_token = ""

    assert manager.close_trade(trade.id, 105.0, "TARGET_HIT") is False
    assert trade.id in manager.open_trades
    assert trade.execution_status == "RECOVERY_REQUIRED"
    assert broker.exit_calls == []


def test_recovery_required_exit_still_marks_to_market(monkeypatch):
    broker = ScriptedBroker()
    manager = _manager(broker)
    trade = _open(manager, "NIFTY")
    trade.execution_status = "RECOVERY_REQUIRED"
    trade.pending_execution = False
    trade.pending_exit = True

    manager.update_trade(
        trade.id,
        current_spot=105.0,
        real_fut_price=105.0,
    )

    assert trade.current_price == 105.0
    assert trade.pnl == trade.unrealized_pnl
    assert trade.pnl < 50.0


def test_ghost_watch_does_not_consume_live_position_capacity():
    manager = _manager(ScriptedBroker(), max_positions=1)
    manager.mode = "HISTORICAL"

    ghost = manager.open_trade(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        price=100.0,
        trailing_stop=95.0,
        lots=1,
        lot_size=10,
        grade="A",
        target=110.0,
        is_ghost=True,
    )
    live = manager.open_trade(
        instrument="BANKNIFTY",
        timeframe="5min",
        direction="LONG",
        price=100.0,
        trailing_stop=95.0,
        lots=1,
        lot_size=10,
        grade="A",
        target=110.0,
        is_recovery=True,
    )

    assert ghost is not None
    assert live is not None


def test_real_ghost_watch_never_becomes_broker_unavailable_trade():
    manager = _manager(ScriptedBroker())

    ghost = manager.open_trade(
        instrument="BANKNIFTY",
        timeframe="15min",
        direction="SHORT",
        price=842.95,
        trailing_stop=716.51,
        lots=1,
        lot_size=30,
        grade="A+",
        target=1169.20,
        inst_type="OPT",
        option_type="PE",
        is_ghost=True,
    )
    payload = manager.get_dashboard_payload(is_historical=False)

    assert ghost is not None
    assert ghost.status == "OPEN"
    assert ghost.execution_status == "OPEN"
    assert ghost.exit_reason == ""
    assert manager.closed_trades == []
    assert payload["open"] == []
    assert payload["closed"] == []
    assert payload["summary"]["total_trades"] == 0


def test_reset_pnl_writes_backup_before_delete(tmp_path, monkeypatch):
    class FakeConn:
        def __init__(self):
            self.deleted = False
            self.committed = False

        def execute(self, sql):
            if sql == "DELETE FROM trades":
                self.deleted = True

        def commit(self):
            self.committed = True

    class FakeDB:
        def __init__(self):
            self.write_lock = threading.Lock()
            self.conn = FakeConn()

        def load_all_trades(self):
            return [{"id": "T1", "status": "CLOSED", "pnl": 123.0}]

    fake_db = FakeDB()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("trading.trade_manager.db", fake_db)

    manager = _manager(ScriptedBroker())
    manager.mode = "HISTORICAL"
    manager.open_trades = {"T-open": object()}
    manager.closed_trades = [object()]

    assert manager.reset_pnl() is True
    backups = list((tmp_path / "data_store" / "reset_backups").glob("trades_reset_backup_*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))[0]["id"] == "T1"
    assert fake_db.conn.deleted is True
    assert fake_db.conn.committed is True
    assert manager.open_trades == {}
    assert manager.closed_trades == []


def test_reconciliation_prefixes_follow_instrument_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "instruments.json").write_text(
        json.dumps(
            {
                "indices": {
                    "NEWIDX": {
                        "symbol": "NEWIDX",
                        "option_symbol_prefix": "NIDX",
                    }
                }
            }
        ),
        encoding="ascii",
    )
    monkeypatch.chdir(tmp_path)

    prefixes = TradeManager._reconciliation_symbol_prefixes()

    assert "NEWIDX" in prefixes
    assert "NIDX" in prefixes
