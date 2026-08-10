"""Reconcile dropped a live position because CANCELLED was read as "nothing happened".

`reconcile_broker_positions` has a `position is None` branch that fetches a live order
status and, on rejected/cancelled/canceled, booked the trade CLOSED at exit_price ==
entry_price (pnl 0), popped it out of open_trades and armed no stop.

It read the status STRING alone. Kite reports an order that partially executed and was
then cancelled as CANCELLED **with a non-zero filled quantity** — the codebase already
documents this in `cancel_order_confirmed`, which extracts the fill correctly. Reconcile
was the one place that did not.

`position is None` is not proof of flat either: the positions snapshot is taken ONCE at
the top of the pass while get_order_status is called live per trade, so the order book
can be strictly newer than the position book. First trade of the day on a contract →
no position row yet → this branch.

Result: 75 quantity of long premium live at the broker with no stop, no local row, and
the system believing it is flat. With force_exit_flattens_orphans defaulting False the
orphan is only logged and carried overnight in NRML.

FIX: read the fill before concluding. A partial fill keeps the row, marks it PARTIAL,
records the confirmed quantity and arms protection. The extraction is now one shared
helper (`trading.broker.order_filled_quantity`) rather than a second copy of the loop.
"""
import inspect
from datetime import datetime
from types import SimpleNamespace

import pytest

from trading.broker import order_filled_quantity


# ── the shared extractor ─────────────────────────────────────────────────────

@pytest.mark.parametrize("row,expected", [
    ({"filledshares": 75}, 75.0),
    ({"filled_quantity": 30}, 30.0),
    ({"filledQuantity": 20}, 20.0),
    ({"filled": 120}, 120.0),
    ({"filledshares": "75"}, 75.0),
    ({"filledshares": -75}, 75.0),
])
def test_every_field_name_kite_uses_is_read(row, expected):
    assert order_filled_quantity(row) == expected


@pytest.mark.parametrize("row", [
    {}, None, "nope", {"filledshares": 0}, {"filledshares": None},
    {"filledshares": "junk"}, [],
])
def test_no_evidence_of_a_fill_reads_as_zero(row):
    assert order_filled_quantity(row) == 0.0


def test_the_cancel_confirmation_uses_the_same_helper():
    """One extractor, not two copies that can drift."""
    from trading.broker import Broker

    src = inspect.getsource(Broker.cancel_order_confirmed)
    assert "order_filled_quantity(" in src


def test_a_partial_fill_still_answers_false_to_cancel_confirmed():
    """The premise this whole family rests on: a partial fill IS a fill."""
    from trading.broker import Broker

    b = Broker.__new__(Broker)
    b.cancel_order = lambda *a, **k: True
    b.get_order_status = lambda oid: {"status": "CANCELLED", "filledshares": 25}
    assert b.cancel_order_confirmed("O1", timeout_seconds=0.01, poll_seconds=0.001) is False


def test_a_clean_cancel_still_answers_true():
    from trading.broker import Broker

    b = Broker.__new__(Broker)
    b.cancel_order = lambda *a, **k: True
    b.get_order_status = lambda oid: {"status": "CANCELLED", "filledshares": 0}
    assert b.cancel_order_confirmed("O1", timeout_seconds=0.01, poll_seconds=0.001) is True


# ── reconcile keeps a partially filled position ──────────────────────────────

def _reconcile_src():
    from trading.trade_manager import TradeManager

    src = inspect.getsource(TradeManager.reconcile_broker_positions)
    marker = 'if status_text in {"rejected", "cancelled", "canceled"}:'
    assert marker in src, "the terminal-status branch moved"
    return src[src.index(marker):]


def test_the_terminal_branch_reads_the_fill_before_closing():
    branch = _reconcile_src()
    close = branch.index('trade.status = "CLOSED"')
    read = branch.index("order_filled_quantity(order_status)")
    assert read < close, (
        "the row is still booked CLOSED without ever reading the filled quantity"
    )


def test_a_partial_fill_does_not_close_the_row():
    branch = _reconcile_src()
    guard = branch.index("if filled_qty > 0:")
    close = branch.index('trade.status = "CLOSED"')
    assert guard < close
    partial = branch[guard:close]
    assert "continue" in partial, "a partial fill still falls through to the close"


def test_a_partial_fill_records_the_confirmed_quantity():
    branch = _reconcile_src()
    partial = branch[branch.index("if filled_qty > 0:"):branch.index('trade.status = "CLOSED"')]
    assert "trade.broker_quantity = int(filled_qty)" in partial, (
        "P&L and risk would still be sized on the INTENDED quantity"
    )
    assert 'trade.execution_status = "PARTIAL"' in partial


def test_a_partial_fill_arms_protection():
    branch = _reconcile_src()
    partial = branch[branch.index("if filled_qty > 0:"):branch.index('trade.status = "CLOSED"')]
    assert "ensure_broker_protection" in partial, (
        "the surviving quantity is left with no stop"
    )


def test_a_failure_to_arm_protection_escalates_rather_than_passing():
    branch = _reconcile_src()
    partial = branch[branch.index("if filled_qty > 0:"):branch.index('trade.status = "CLOSED"')]
    assert "_mark_recovery" in partial


def test_a_genuinely_unfilled_cancel_still_closes_the_row():
    """The fix must not strand rows for orders that truly never filled."""
    branch = _reconcile_src()
    assert 'trade.exit_reason = "BROKER_REJECTED"' in branch
    assert 'trade.execution_status = "REJECTED"' in branch


def test_the_position_snapshot_premise_still_holds():
    """`position is None` is not proof of flat: the snapshot is older than the order
    status. If reconcile ever starts re-reading positions per trade, this branch's
    reasoning changes."""
    from trading.trade_manager import TradeManager

    src = inspect.getsource(TradeManager.reconcile_broker_positions)
    assert "if position is None:" in src
