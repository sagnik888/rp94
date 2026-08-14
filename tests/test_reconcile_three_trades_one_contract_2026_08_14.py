"""Behavioural: drive the REAL reconcile pass with three trades on one contract.

An external review made a fair criticism of the first pass at this fix: its tests
called the allocation helper in isolation and inspected source strings, so "none
exercises the real three-trade reconciliation". This one does — it builds three real
Trade objects on one tradingsymbol, hands reconcile_broker_positions a genuine
Kite-shaped positions payload with a net quantity of 225, and asserts on the state
the real function leaves behind.

The defect it pins: reconcile keys broker positions by tradingsymbol, so all three
trades read ONE row, and the row's net quantity was assigned to each of them —

    trade.broker_quantity = abs(net_qty)   # 225, for all three

broker_quantity SIZES orders (ensure_broker_protection, the flatten path,
_request_broker_exit), so each trade then armed a 225 stop against the 75 it held.

Nothing here touches a network or a real broker: the manager is built with __new__,
the broker is a fake that records calls and would raise if asked to transact, and
mode is left non-REAL.
"""
import threading
from datetime import datetime

import pytest

from trading.trade_manager import Trade, TradeManager


SYMBOL = "NIFTY2681424300CE"


class _FakeBroker:
    """Records calls. Any order attempt is a test failure, not a network call."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _refuse(*a, **k):
            raise AssertionError(f"reconcile must not call broker.{name} here")
        return _refuse


def _trade(tid, lots, entry_time):
    """quantity is a read-only property (lots x lot_size), so size via lots."""
    t = Trade(
        id=tid,
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        entry_time=entry_time,
        trailing_stop=90.0,
        current_stop=90.0,
        lots=lots,
        lot_size=75,
        grade="A",
    )
    assert t.quantity == lots * 75
    t.trading_symbol = SYMBOL
    t.symbol_token = "12345"
    t.status = "OPEN"
    t.execution_status = "OPEN"
    t.broker_quantity = 0
    t.protective_order_id = ""
    t.broker_order_id = ""
    t.pending_execution = False
    t.inst_type = "OPT"
    return t


class _Sink:
    """Swallows any logging/notification call the pass makes."""

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _manager(trades):
    mgr = TradeManager.__new__(TradeManager)
    mgr.ui_logger = _Sink()
    mgr.mode = "HISTORICAL"
    mgr.broker = _FakeBroker()
    mgr._lock = threading.RLock()
    mgr.open_trades = {t.id: t for t in trades}
    mgr.closed_trades = []
    mgr.orphan_positions = {}
    mgr.product_type = "CARRYFORWARD"
    mgr._partial_exit_unknown_logged = set()
    mgr.save_state = lambda *a, **k: None
    mgr.ensure_broker_protection = lambda *a, **k: True
    return mgr


def _positions(net_quantity):
    """A Kite positions payload: ONE row for the contract, as the broker really sends."""
    return {
        "data": [
            {
                # `netqty` is the key reconcile reads (_position_quantity); the broker
                # adapter normalizes Kite's payload to this AngelOne-era name.
                "tradingsymbol": SYMBOL,
                "netqty": net_quantity,
                "sellavgprice": 0.0,
                "buyavgprice": 100.0,
            }
        ]
    }


# ── the reported scenario, through the real function ─────────────────────────

def test_three_trades_on_one_strike_each_keep_their_own_quantity():
    """3 x 75 against a broker net of 225: the reported P0."""
    trades = [
        _trade("t-5min", 1, datetime(2026, 8, 14, 9, 20)),
        _trade("t-15min", 1, datetime(2026, 8, 14, 9, 35)),
        _trade("t-1min", 1, datetime(2026, 8, 14, 9, 50)),
    ]
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(225))

    got = {t.id: t.broker_quantity for t in trades}
    assert got == {"t-5min": 75, "t-15min": 75, "t-1min": 75}, (
        f"aggregate leaked into per-trade quantity: {got}"
    )


def test_no_trade_ends_up_holding_the_aggregate():
    trades = [
        _trade("a", 1, datetime(2026, 8, 14, 9, 20)),
        _trade("b", 1, datetime(2026, 8, 14, 9, 35)),
        _trade("c", 1, datetime(2026, 8, 14, 9, 50)),
    ]
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(225))

    assert max(t.broker_quantity for t in trades) == 75


def test_the_protection_quantity_each_trade_would_request_is_its_own():
    """What the defect actually cost: three 225-quantity protection requests."""
    trades = [
        _trade("a", 1, datetime(2026, 8, 14, 9, 20)),
        _trade("b", 1, datetime(2026, 8, 14, 9, 35)),
        _trade("c", 1, datetime(2026, 8, 14, 9, 50)),
    ]
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(225))

    requested = [TradeManager._confirmed_broker_quantity(t) for t in trades]
    assert requested == [75, 75, 75]
    assert sum(requested) == 225, "the split must still add up to the real position"


# ── a single trade is untouched by the change ────────────────────────────────

def test_a_lone_trade_still_takes_the_whole_row():
    solo = _trade("solo", 1, datetime(2026, 8, 14, 9, 20))
    mgr = _manager([solo])

    mgr.reconcile_broker_positions(_positions(75))

    assert solo.broker_quantity == 75


def test_a_lone_trade_still_sees_a_broker_excess():
    """Excess detection compares against trade.quantity; capping would hide it."""
    solo = _trade("solo", 1, datetime(2026, 8, 14, 9, 20))
    mgr = _manager([solo])

    mgr.reconcile_broker_positions(_positions(225))

    assert solo.broker_quantity == 225


# ── a short broker position under-fills rather than over-arming ──────────────

def test_a_short_position_does_not_over_arm_anyone():
    """Broker holds 150 while three trades claim 75 each."""
    trades = [
        _trade("a", 1, datetime(2026, 8, 14, 9, 20)),
        _trade("b", 1, datetime(2026, 8, 14, 9, 35)),
        _trade("c", 1, datetime(2026, 8, 14, 9, 50)),
    ]
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(150))

    total = sum(t.broker_quantity for t in trades)
    assert total <= 150, "allocated more than the broker actually holds"
    assert max(t.broker_quantity for t in trades) <= 75


def test_uneven_trade_sizes_get_their_own_intent():
    trades = [
        _trade("small", 1, datetime(2026, 8, 14, 9, 20)),
        _trade("big", 2, datetime(2026, 8, 14, 9, 35)),
    ]
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(225))

    assert {t.id: t.broker_quantity for t in trades} == {"small": 75, "big": 150}


# ── the pass does not transact ───────────────────────────────────────────────

def test_reconcile_sends_no_orders_for_healthy_trades():
    """_FakeBroker raises on any order method; reaching one fails the test."""
    trades = [
        _trade("a", 1, datetime(2026, 8, 14, 9, 20)),
        _trade("b", 1, datetime(2026, 8, 14, 9, 35)),
    ]
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(150))  # would raise if it transacted


# ── proof that this harness detects the defect ───────────────────────────────

def test_the_harness_catches_the_original_defect(monkeypatch):
    """Restore the pre-fix behaviour and confirm this setup exposes it.

    Without this, a passing suite proves only that the tests run — not that they
    would have caught the bug they were written for.
    """
    trades = [
        _trade("a", 1, datetime(2026, 8, 14, 9, 20)),
        _trade("b", 1, datetime(2026, 8, 14, 9, 35)),
        _trade("c", 1, datetime(2026, 8, 14, 9, 50)),
    ]
    mgr = _manager(trades)

    # exactly what the code did before: the row's net for every trade sharing it
    monkeypatch.setattr(
        TradeManager,
        "_allocate_broker_quantity",
        staticmethod(lambda group, net: {str(tid): abs(int(net or 0)) for tid, _ in group}),
    )

    mgr.reconcile_broker_positions(_positions(225))

    assert [t.broker_quantity for t in trades] == [225, 225, 225], (
        "the harness no longer reproduces the defect, so the tests above prove nothing"
    )


def test_the_allocation_fallback_is_never_the_aggregate():
    """A trade missing from the map must not silently inherit the whole row.

    The lookup default is unreachable as written, but it was abs(net_qty) — the exact
    defect the split removes. A silent default that restores a fixed bug is how the bug
    comes back, so the fallback is this trade's own size capped by the row.
    """
    trades = [
        _trade("a", 1, datetime(2026, 8, 14, 9, 20)),
        _trade("b", 1, datetime(2026, 8, 14, 9, 35)),
        _trade("c", 1, datetime(2026, 8, 14, 9, 50)),
    ]
    mgr = _manager(trades)

    # Force the fallback: an allocator that maps nothing at all.
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(TradeManager, "_allocate_broker_quantity", staticmethod(lambda group, net: {}))
        mgr.reconcile_broker_positions(_positions(225))
    finally:
        mp.undo()

    for t in trades:
        assert t.broker_quantity <= 75, (
            f"{t.id} fell back to the aggregate: {t.broker_quantity}"
        )
