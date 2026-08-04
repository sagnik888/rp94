"""Bank part of a multi-lot winner without ever over-exiting or going naked.

Live could not scale out at all: partial_derisk / partial_close_trade were 0 occurrences,
and the existing PARTIAL_EXIT states only RECONCILE an accidental broker partial fill.
Built for the multi-lot, multi-index configuration; at one lot it cannot even be called,
since closing every lot is an exit and goes down a different path.

The whole safety property is the ORDER of the broker work:

  1. cancel the full-size protective stop and PROVE it. An unproven cancel means the SL-M
     may still rest for the full size, and a partial exit on top of it exits twice.
  2. place the partial exit. If it fails, RE-ARM the full stop before returning — the
     position is unchanged and must not be left unprotected.
  3. only once POSITIONS prove the reduce, book the realised child and re-arm a stop for
     the remainder. An acknowledgement is not a fill; booking off one writes a phantom
     P&L row for lots that never left.
"""
import threading
from datetime import datetime

import pytest

from trading.order_retry import BrokerOrderAmbiguous, BrokerOrderRejected
from trading.trade_manager import IST, Trade, TradeManager

SYMBOL = "NIFTY2680524500CE"


class _Broker:
    def __init__(self, cancel=True, exit_result="EXIT-1", remaining_qty=130):
        self.cancel = cancel
        self.exit_result = exit_result
        self.remaining_qty = remaining_qty
        self.calls = []

    def cancel_order_confirmed(self, oid, variety="NORMAL", **kw):
        self.calls.append(("cancel", oid))
        if isinstance(self.cancel, Exception):
            raise self.cancel
        return self.cancel

    def cancel_order(self, oid, variety="NORMAL"):
        return True

    def close_order(self, symbol, token, qty, side, product_type="CARRYFORWARD", **kw):
        self.calls.append(("exit", qty))
        if isinstance(self.exit_result, Exception):
            raise self.exit_result
        return self.exit_result

    def place_protective_stop(self, symbol, token, qty, side, trigger, product_type="CARRYFORWARD", **kw):
        self.calls.append(("arm", qty))
        return "SLM-NEW"

    def get_positions(self):
        if self.remaining_qty is None:
            return None
        return {"data": [{"tradingsymbol": SYMBOL, "netqty": str(self.remaining_qty),
                          "buyavgprice": "200", "sellavgprice": "0", "realised": "0"}]}

    def inspect_working_protective_stop(self, *a, **k):
        return {"state": "NONE", "order_id": "", "reason": "none", "matches": []}

    def find_working_protective_stop(self, *a, **k):
        return ""

    def get_fill(self, order_id):
        return {"state": "UNKNOWN", "reason": "stub"}


def _manager(broker, mode="REAL"):
    m = TradeManager.__new__(TradeManager)
    m.broker = broker
    m.ui_logger = None
    m.mode = mode
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


def _trade(lots=3, stop_id="SLM-OLD"):
    t = Trade(
        id="TP1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=200.0, entry_time=datetime.now(IST), trailing_stop=180.0,
        current_stop=180.0, lots=lots, lot_size=65, grade="A", inst_type="OPT",
        trading_symbol=SYMBOL, symbol_token="1", broker_order_id="251003000111111",
    )
    t.status = "OPEN"
    t.execution_status = "OPEN"
    t.broker_quantity = lots * 65
    t.current_price = 260.0
    t.protective_order_id = stop_id
    return t


def _run(broker, trade, close_lots=1, price=260.0):
    manager = _manager(broker)
    manager.open_trades[trade.id] = trade
    manager._run_partial_reduce(trade, close_lots, close_lots * 65, price, "PARTIAL_DERISK", None)
    return manager


# ── refusals ─────────────────────────────────────────────────────────────────

def test_a_single_lot_trade_cannot_scale_out():
    """Closing every lot is an exit and has its own protective handling."""
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade(lots=1)
    manager.open_trades[trade.id] = trade
    assert manager.partial_close_trade(trade.id, 1, 260.0) is False
    assert broker.calls == []


def test_closing_every_lot_is_refused():
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade(lots=3)
    manager.open_trades[trade.id] = trade
    assert manager.partial_close_trade(trade.id, 3, 260.0) is False


def test_a_trade_already_exiting_is_refused():
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade()
    trade.pending_exit = True
    manager.open_trades[trade.id] = trade
    assert manager.partial_close_trade(trade.id, 1, 260.0) is False


def test_only_one_de_risk_per_trade():
    broker = _Broker()
    manager = _manager(broker, mode="HISTORICAL")
    trade = _trade()
    manager.open_trades[trade.id] = trade
    assert manager.partial_close_trade(trade.id, 1, 260.0) is True
    assert manager.partial_close_trade(trade.id, 1, 260.0) is False


def test_a_persisted_in_flight_reduce_blocks_a_second_one():
    """After a restart the previous reduce must be reconciled first."""
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade()
    trade.partial_reduce_order_id = "EXIT-PREV"
    manager.open_trades[trade.id] = trade
    assert manager.partial_close_trade(trade.id, 1, 260.0) is False


# ── the ordering, which is the whole safety property ─────────────────────────

def test_the_happy_path_cancels_then_exits_then_re_arms():
    broker = _Broker(remaining_qty=130)
    trade = _trade(lots=3)
    _run(broker, trade)
    kinds = [c[0] for c in broker.calls]
    assert kinds == ["cancel", "exit", "arm"], f"wrong broker ordering: {kinds}"
    assert broker.calls[1][1] == 65, "exited the wrong quantity"


def test_an_unproven_stop_cancel_aborts_and_changes_nothing():
    """The SL-M may still rest for the full size; a partial exit doubles the exit."""
    broker = _Broker(cancel=None)
    trade = _trade()
    _run(broker, trade)
    assert [c[0] for c in broker.calls] == ["cancel"], "exited despite an unproven cancel"
    assert trade.lots == 3
    assert trade.partial_derisk_done is False


def test_a_refused_stop_cancel_also_aborts():
    broker = _Broker(cancel=BrokerOrderRejected("lease held", terminal=True))
    trade = _trade()
    _run(broker, trade)
    assert [c[0] for c in broker.calls] == ["cancel"]
    assert trade.lots == 3


def test_a_failed_partial_exit_re_arms_the_full_size_stop():
    """The position is unchanged, so it must not be left unprotected."""
    broker = _Broker(exit_result=BrokerOrderRejected("no funds", terminal=True))
    trade = _trade(lots=3)
    _run(broker, trade)
    kinds = [c[0] for c in broker.calls]
    assert kinds == ["cancel", "exit", "arm"], f"stop not restored after a failed exit: {kinds}"
    assert broker.calls[2][1] == 195, "re-armed for the reduced size, not the full position"
    assert trade.lots == 3


def test_an_ambiguous_partial_exit_books_nothing_and_escalates():
    broker = _Broker(exit_result=BrokerOrderAmbiguous("read timed out"))
    trade = _trade(lots=3)
    _run(broker, trade)
    assert trade.lots == 3, "reduced the parent on an unacknowledged exit"
    assert trade.execution_status == "RECOVERY_REQUIRED"
    assert "arm" not in [c[0] for c in broker.calls], (
        "re-armed a full-size stop on top of an exit that may be live"
    )


def test_an_unconfirmable_reduce_does_not_book_or_re_arm():
    """Positions unreadable: the real quantity is unknown, so nothing may be assumed."""
    broker = _Broker(remaining_qty=None)
    trade = _trade(lots=3)
    _run(broker, trade)
    assert trade.lots == 3
    assert trade.partial_derisk_done is False
    assert trade.execution_status == "RECOVERY_REQUIRED"


def test_an_acknowledgement_alone_never_books_the_child():
    """Booking off an ack writes a phantom P&L row for lots that never left."""
    broker = _Broker(remaining_qty=195)      # position did NOT reduce
    trade = _trade(lots=3)
    manager = _run(broker, trade)
    assert manager.closed_trades == []
    assert trade.lots == 3


# ── what gets booked ─────────────────────────────────────────────────────────

def test_the_child_carries_the_realised_lots_and_the_parent_shrinks():
    broker = _Broker(remaining_qty=130)
    trade = _trade(lots=3)
    manager = _run(broker, trade, close_lots=1, price=260.0)

    assert trade.lots == 2, "parent was not reduced"
    assert len(manager.closed_trades) == 1
    child = manager.closed_trades[0]
    assert child.lots == 1
    assert child.status == "CLOSED"
    assert child.exit_reason == "PARTIAL_DERISK"
    assert child.exit_price == 260.0
    # Rs.60 of premium on 65 units, less charges.
    assert child.pnl == pytest.approx(60.0 * 65 - float(child.charges or 0.0))
    assert child.protective_order_id == "", "child inherited the parent's stop id"


def test_the_remainder_is_re_protected_for_its_new_size():
    broker = _Broker(remaining_qty=130)
    trade = _trade(lots=3)
    _run(broker, trade)
    arms = [c for c in broker.calls if c[0] == "arm"]
    assert arms and arms[-1][1] == 130, "re-armed for the wrong remaining quantity"


def test_a_simulated_book_is_reduced_synchronously():
    broker = _Broker()
    manager = _manager(broker, mode="HISTORICAL")
    trade = _trade(lots=3)
    manager.open_trades[trade.id] = trade
    assert manager.partial_close_trade(trade.id, 1, 260.0) is True
    assert trade.lots == 2 and len(manager.closed_trades) == 1
    assert broker.calls == [], "a simulated book touched the broker"
