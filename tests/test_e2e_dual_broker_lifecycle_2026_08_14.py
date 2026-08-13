"""E2E: the position row -> protective order loop, with data and orders separated.

This system runs two brokers: Kite places orders, Fyers supplies data. The external
review's description of the ownership defect ended at the order, not the field:

    "Three 75-quantity trades sharing one contract and broker net quantity 225 each
     became broker_quantity=225; three 225-quantity protection requests followed."

The matrix test proves the field is now per-trade. This one closes the loop to the
ORDER, because that is where the damage happened. ensure_broker_protection sizes
straight off the field:

    qty = int(quantity or trade.broker_quantity or trade.quantity)

so it runs the real reconcile, then the real ensure_broker_protection, and asserts on
the quantity the broker was actually handed. An oversized SL-M on options sells more
than the account owns.

It also pins the separation the dual-broker setup depends on: the DATA broker must
never be asked to transact. A data feed with an order method reachable from the
trading path is a wiring accident away from being used.

SAFETY: fake brokers only, no network, no database. Mode is REAL for the protection
path (it returns early otherwise) which is the established pattern in this suite --
_live_account_send_blocked stays inert because real_broker is never set.
"""
import threading
from datetime import datetime, timedelta

import pytest

from trading.trade_manager import Trade, TradeManager


SYMBOL = "NIFTY2681424300CE"
LOT = 65


class _OrderBroker:
    """Kite's role: records every order it is asked to send."""

    def __init__(self):
        self.protective = []
        self.exits = []
        self.cancels = []

    def place_protective_stop(self, symbol, token, qty, side, trigger, product, **kw):
        self.protective.append(
            {"symbol": symbol, "qty": qty, "side": side, "trigger": trigger, "tag": kw.get("tag")}
        )
        return f"SL-{len(self.protective)}"

    def close_order(self, symbol, token, qty, side, product, **kw):
        self.exits.append({"symbol": symbol, "qty": qty, "side": side})
        return f"EX-{len(self.exits)}"

    def cancel_order(self, order_id, variety="NORMAL"):
        self.cancels.append(order_id)
        return True

    def order_tag(self, trade_id, leg="", attempt=0):
        return f"{trade_id}|{leg}|{attempt}"[:20]

    def get_order_status(self, _oid):
        return {}

    def find_protective_stop(self, *a, **k):
        return ""


class _DataBroker:
    """Fyers' role: quotes only. Any order attempt is a wiring bug."""

    def __init__(self):
        self.quotes = 0

    def get_ltp(self, _symbol):
        self.quotes += 1
        return 100.0

    def __getattr__(self, name):
        if name.startswith(("place", "close", "cancel", "modify", "exit", "square")):
            def _refuse(*a, **k):
                raise AssertionError(f"the DATA broker was asked to transact: {name}")
            return _refuse
        raise AttributeError(name)


class _Sink:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _trade(tid, timeframe, opened_minute, lots=1):
    t = Trade(
        id=tid,
        instrument="NIFTY",
        timeframe=timeframe,
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime(2026, 8, 14, 9, 20) + timedelta(minutes=opened_minute),
        trailing_stop=90.0,
        current_stop=90.0,
        lots=lots,
        lot_size=LOT,
        grade="A",
    )
    t.trading_symbol = SYMBOL
    t.symbol_token = "12345"
    t.status = "OPEN"
    t.execution_status = "OPEN"
    t.broker_quantity = 0
    t.protective_order_id = ""
    t.broker_order_id = ""
    t.pending_execution = False
    t.inst_type = "OPT"
    t.is_ghost = False
    return t


def _manager(trades, mode="REAL"):
    mgr = TradeManager.__new__(TradeManager)
    mgr.ui_logger = _Sink()
    mgr.mode = mode
    mgr.broker = _OrderBroker()
    mgr.data_broker = _DataBroker()
    mgr._lock = threading.RLock()
    mgr.open_trades = {t.id: t for t in trades}
    mgr.closed_trades = []
    mgr.orphan_positions = {}
    mgr.product_type = "CARRYFORWARD"
    mgr._partial_exit_unknown_logged = set()
    mgr._last_protective_sync = {}
    mgr.save_state = lambda *a, **k: None
    return mgr


def _positions(netqty):
    return {
        "data": [
            {
                "tradingsymbol": SYMBOL,
                "netqty": netqty,
                "sellavgprice": 0.0,
                "buyavgprice": 100.0,
            }
        ]
    }


def _three_trades():
    return [
        _trade("t-1min", "1min", 0),
        _trade("t-5min", "5min", 15),
        _trade("t-15min", "15min", 30),
    ]


# ── the loop the defect actually travelled ───────────────────────────────────

def test_no_protection_request_carries_the_aggregate_quantity():
    """The reported consequence: three 225-quantity protection requests."""
    trades = _three_trades()
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(LOT * 3))
    for t in trades:
        mgr.ensure_broker_protection(t)

    sent = [o["qty"] for o in mgr.broker.protective]
    assert sent, "no protective order was placed at all"
    assert all(q == LOT for q in sent), f"oversized SL-M reached the broker: {sent}"
    assert LOT * 3 not in sent


def test_each_protective_order_matches_its_own_trade():
    trades = _three_trades()
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(LOT * 3))
    for t in trades:
        mgr.ensure_broker_protection(t)

    assert len(mgr.broker.protective) == 3
    assert sum(o["qty"] for o in mgr.broker.protective) == LOT * 3


def test_protective_orders_are_individually_tagged():
    """Three stops on one strike are only tellable apart by their tags."""
    trades = _three_trades()
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(LOT * 3))
    for t in trades:
        mgr.ensure_broker_protection(t)

    tags = [o["tag"] for o in mgr.broker.protective]
    assert all(tags), f"an untagged stop was placed: {tags}"
    assert len(set(tags)) == 3, f"two stops share an identity: {tags}"


def test_a_multi_lot_trade_is_protected_for_its_own_size():
    trades = [
        _trade("t-1min", "1min", 0, lots=1),
        _trade("t-5min", "5min", 15, lots=2),
    ]
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(LOT * 3))
    for t in trades:
        mgr.ensure_broker_protection(t)

    by_qty = sorted(o["qty"] for o in mgr.broker.protective)
    assert by_qty == [LOT, LOT * 2]


# ── proof this closes the loop the review described ──────────────────────────

def test_the_lifecycle_reproduces_the_oversized_stop(monkeypatch):
    """With the pre-fix allocation, three 225-quantity requests really do follow."""
    trades = _three_trades()
    mgr = _manager(trades)

    monkeypatch.setattr(
        TradeManager,
        "_allocate_broker_quantity",
        staticmethod(lambda group, net: {str(tid): abs(int(net or 0)) for tid, _ in group}),
    )

    mgr.reconcile_broker_positions(_positions(LOT * 3))
    for t in trades:
        mgr.ensure_broker_protection(t)

    sent = [o["qty"] for o in mgr.broker.protective]
    assert sent == [LOT * 3] * 3, (
        "the lifecycle no longer reproduces the oversized stop, so it certifies nothing"
    )


# ── data and orders stay separated ───────────────────────────────────────────

def test_the_data_broker_is_never_asked_to_transact():
    """_DataBroker raises on any order-shaped attribute."""
    trades = _three_trades()
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(LOT * 3))
    for t in trades:
        mgr.ensure_broker_protection(t)

    assert mgr.broker.protective, "sanity: the ORDER broker should have been used"


def test_the_data_broker_refuses_order_methods_outright():
    """Pins the fixture's own guarantee, so the test above cannot pass vacuously."""
    data = _DataBroker()

    for name in ("place_order", "close_order", "cancel_order"):
        with pytest.raises(AssertionError):
            getattr(data, name)()


# ── mode still governs the whole lifecycle ───────────────────────────────────

def test_no_protection_is_placed_outside_real_mode():
    trades = _three_trades()
    mgr = _manager(trades, mode="HISTORICAL")

    mgr.reconcile_broker_positions(_positions(LOT * 3))
    for t in trades:
        mgr.ensure_broker_protection(t)

    assert mgr.broker.protective == [], "an SL-M was placed outside REAL mode"


def test_reconciliation_still_runs_outside_real_mode():
    """The read half must keep working while the system is not trading."""
    trades = _three_trades()
    mgr = _manager(trades, mode="HISTORICAL")

    mgr.reconcile_broker_positions(_positions(LOT * 3))

    assert [t.broker_quantity for t in trades] == [LOT, LOT, LOT]
