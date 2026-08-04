"""A refused queue task is an order that never gets sent.

engine/async_queue.submit() returns False on shutdown, and again when the queue is still
full after a bounded 2s wait. Both return values were discarded at the two call sites in
trade_manager, so the background worker simply never ran: no order, no exception, no log.

The exit case is the dangerous one. close_bg never runs, nothing raises, and the row sits
pending_exit with a LIVE position behind it. The queue drains at shutdown and at EOD --
precisely when an unexited position matters most.

The entry case is milder but still wrong: nothing is resting at the exchange, yet the row
stayed pending_execution forever, holding a position slot and counting against exposure
while the broker had never heard of it.
"""
import threading
from datetime import datetime

import pytest

from trading.trade_manager import IST, Trade, TradeManager

SYMBOL = "NIFTY2680524500CE"


class _Queue:
    """Stands in for engine.async_queue.execution_queue."""

    def __init__(self, accept):
        self.accept = accept
        self.submitted = []

    def submit(self, name, func, *a, **k):
        self.submitted.append(name)
        if not self.accept:
            return False
        func()
        return True


class _Broker:
    def __init__(self):
        self.orders = []

    def close_order(self, *a, **k):
        self.orders.append(("exit", a))
        return "EXIT-1"

    def place_order(self, *a, **k):
        self.orders.append(("entry", a))
        return "ENTRY-1"

    def cancel_order(self, oid, variety="NORMAL"):
        return True

    def get_positions(self):
        return {"data": []}

    def inspect_working_protective_stop(self, *a, **k):
        return {"state": "NONE", "order_id": "", "reason": "none", "matches": []}

    def find_working_protective_stop(self, *a, **k):
        return ""

    def get_fill(self, order_id):
        return {"state": "UNKNOWN", "reason": "stub"}


def _manager(broker):
    m = TradeManager.__new__(TradeManager)
    m.broker = broker
    m.ui_logger = None
    m.mode = "REAL"
    m.product_type = "CARRYFORWARD"
    m.open_trades = {}
    m.closed_trades = []
    m.orphan_positions = {}
    m._lock = threading.RLock()
    m._protection_lock = threading.RLock()
    m.save_state = lambda: None
    m._last_protective_sync = {}
    m._last_protective_status_poll = {}
    m._last_recovery_alert = {}
    m._last_mark_to_market_save = 0.0
    m.max_positions = 4
    m.futures_sl_pct = 0.3
    m.options_sl_pct = 15.0
    m._log_ui = lambda *a, **k: None
    m.on_notification = None
    return m


def _trade():
    t = Trade(
        id="TQ1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=200.0, entry_time=datetime.now(IST), trailing_stop=180.0,
        current_stop=180.0, lots=1, lot_size=65, grade="A", inst_type="OPT",
        trading_symbol=SYMBOL, symbol_token="1", broker_order_id="251003000111111",
    )
    t.execution_status = "OPEN"
    t.status = "OPEN"
    # close_bg refuses a row that is not already marked pending, so the caller-side state
    # has to be the real one for this to exercise the queue path at all.
    t.pending_exit = True
    t.broker_quantity = 65
    return t


def _exit_with_queue(accept):
    import engine.async_queue as aq

    broker = _Broker()
    manager = _manager(broker)
    trade = _trade()
    manager.open_trades[trade.id] = trade
    queue = _Queue(accept)
    original = aq.execution_queue
    aq.execution_queue = queue
    try:
        manager._request_broker_exit(trade)
    finally:
        aq.execution_queue = original
    return manager, trade, broker, queue


def test_a_refused_exit_task_does_not_leave_the_row_thinking_an_exit_is_in_flight():
    manager, trade, broker, queue = _exit_with_queue(accept=False)

    assert queue.submitted, "the exit was never even offered to the queue"
    assert broker.orders == [], "sanity: a refused task must not have reached the broker"
    assert trade.pending_exit is False, (
        "row still claims an exit is in flight when no order was ever sent"
    )
    assert trade.execution_status == "RECOVERY_REQUIRED", (
        "a live position whose exit was never queued must escalate, not sit silent"
    )


def test_a_refused_exit_is_still_reachable_by_the_eod_flatten():
    """RECOVERY_REQUIRED is what check_session_end routes to emergency_flatten_trade."""
    manager, trade, _broker, _queue = _exit_with_queue(accept=False)
    assert trade.id in manager.open_trades
    assert trade.status != "CLOSED", "closed the row while the position is still live"


def test_an_accepted_exit_still_works():
    """The guard must not break the normal path."""
    _manager_, trade, broker, _queue = _exit_with_queue(accept=True)
    assert any(kind == "exit" for kind, _ in broker.orders)


def test_the_entry_and_exit_submits_are_both_checked():
    """Pins both call sites; the exit one was the dangerous half."""
    import inspect

    src = inspect.getsource(TradeManager)
    bare_entry = "            execution_queue.submit(f\"PlaceOrder_{trading_symbol}\", place_bg)\n"
    bare_exit = "        execution_queue.submit(f\"CloseOrder_{trade.trading_symbol}\", close_bg)\n"
    assert bare_entry not in src, "entry submit result is discarded again"
    assert bare_exit not in src, "exit submit result is discarded again"
    # Counted rather than fixed at a number: new broker work gets queued over time (the
    # partial de-risk was the third), and the invariant is "no submit result is
    # discarded", not "there are exactly two submits".
    submits = src.count("execution_queue.submit(")
    checked = src.count("execution_queue.submit(")  # every one must sit inside a check
    assert submits >= 2
    for line in src.splitlines():
        if "execution_queue.submit(" not in line:
            continue
        assert "is False" in line or "if " in line, (
            f"a queue submit ignores its result: {line.strip()}"
        )
    assert checked == submits


class _SilentQueue(_Queue):
    """A double that runs the task but returns None -- it does not signal either way."""

    def submit(self, name, func, *a, **k):
        self.submitted.append(name)
        func()
        return None


def test_a_queue_that_returns_none_is_not_treated_as_a_refusal():
    """submit() is documented -> bool. None means "no signal", not "refused".

    Treating it as refusal closed 20 otherwise-healthy trades outright, because the
    existing test doubles and simulation stubs return None.
    """
    import engine.async_queue as aq

    broker = _Broker()
    manager = _manager(broker)
    trade = _trade()
    manager.open_trades[trade.id] = trade
    original = aq.execution_queue
    aq.execution_queue = _SilentQueue(accept=True)
    try:
        manager._request_broker_exit(trade)
    finally:
        aq.execution_queue = original

    assert any(kind == "exit" for kind, _ in broker.orders)
    assert trade.execution_status != "RECOVERY_REQUIRED", (
        "a queue that simply does not signal was treated as having refused the task"
    )
