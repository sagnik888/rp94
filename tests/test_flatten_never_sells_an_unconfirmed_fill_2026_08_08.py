"""The EOD force exit could sell a fill that never happened.

`emergency_flatten_trade` sized its order with:

    qty = int(trade.broker_quantity or trade.quantity)

`broker_quantity` is 0 on every row whose entry the broker did not confirm — an ambiguous
acknowledgement, an entry not found in the order book within the timeout, a stale entry
that could not be safely cancelled, and every prior-session row on restart.
`check_session_end` routes all of those straight to this function. So it fell back to the
INTENDED quantity.

Options are always BOUGHT here, so the flatten side is SELL. Selling a fill that never
happened does not close anything — it OPENS a naked written option in CARRYFORWARD/NRML,
the product chosen precisely so the broker will not square it off, at 15:25 with no
session left to correct it, and it carries overnight.

The sibling `_maybe_auto_flatten_unprotected` already refuses on exactly this test:

    if self._confirmed_broker_quantity(trade) <= 0:
        return False

The fix asks the positions book once rather than guessing, and honours the invariant this
file follows everywhere else: an unreadable book is NOT a flat book.
"""
import threading
from types import SimpleNamespace

import pytest

from trading.trade_manager import TradeManager

SYMBOL = "NIFTY2680524500CE"


class _Broker:
    def __init__(self, positions=None, raises=False):
        self._positions = positions
        self._raises = raises
        self.orders = []

    def get_positions(self):
        if self._raises:
            raise RuntimeError("positions endpoint down")
        return self._positions

    def close_order(self, symbol, token, qty, side, product_type="CARRYFORWARD", **kw):
        self.orders.append((symbol, qty, side))
        return "EXIT-1"


def _manager(broker):
    m = TradeManager.__new__(TradeManager)
    m.broker = broker
    m.mode = "REAL"
    m.product_type = "CARRYFORWARD"
    m.open_trades = {}
    m.closed_trades = []
    m.orphan_positions = {}
    m._lock = threading.RLock()
    m.save_state = lambda: None
    m._log_ui = lambda *a, **k: None
    m._mark_recovery = lambda t, note: recorded.append(note)
    m._release_gtt_fallback = lambda t, why: True
    m._withhold_flatten = lambda t, note: False
    m._protective_exit_side = lambda t: "SELL"
    m._tag_kwargs = lambda fn, t, kind: {}
    m._unowned_protective_stop_blocks_flatten = lambda *a, **k: False
    return m


recorded = []


def _trade(broker_quantity=0, quantity=75):
    return SimpleNamespace(
        id="T1", trading_symbol=SYMBOL, symbol_token="1",
        broker_quantity=broker_quantity, quantity=quantity,
        inst_type="OPT", direction="LONG", is_ghost=False,
        execution_status="RECOVERY_REQUIRED", protective_order_id="",
    )


def _positions(netqty):
    return {"data": [{"tradingsymbol": SYMBOL, "netqty": str(netqty)}]}


@pytest.fixture(autouse=True)
def _clear():
    recorded.clear()
    yield


# ── the naked-short case ─────────────────────────────────────────────────────

def test_an_unconfirmed_entry_on_a_flat_book_sends_no_order():
    """The headline: nothing filled, so there is nothing to sell."""
    broker = _Broker(positions={"data": []})
    assert _manager(broker).emergency_flatten_trade(_trade(), "force exit") is False
    assert broker.orders == [], "sold a position that was never opened"


def test_it_does_not_fall_back_to_the_intended_quantity():
    """quantity=75 must never reach the broker when broker_quantity is 0."""
    broker = _Broker(positions={"data": []})
    _manager(broker).emergency_flatten_trade(_trade(quantity=75), "force exit")
    assert not any(q == 75 for _, q, _ in broker.orders)


def test_an_unreadable_book_is_not_treated_as_flat_and_sends_nothing():
    """The invariant this file follows everywhere: a FAILED read is not a state."""
    broker = _Broker(raises=True)
    assert _manager(broker).emergency_flatten_trade(_trade(), "force exit") is False
    assert broker.orders == []
    assert recorded, "an unreadable book silently withheld with no escalation"
    assert "manually" in recorded[0].lower()


# ── a real position must still be flattened ──────────────────────────────────

def test_a_confirmed_fill_is_still_flattened_on_its_confirmed_size():
    broker = _Broker(positions=_positions(75))
    _manager(broker).emergency_flatten_trade(_trade(broker_quantity=75), "force exit")
    assert broker.orders == [(SYMBOL, 75, "SELL")]


def test_an_unrecorded_real_position_is_recovered_from_the_book():
    """broker_quantity was never written, but the book shows a live position."""
    broker = _Broker(positions=_positions(150))
    _manager(broker).emergency_flatten_trade(_trade(broker_quantity=0), "force exit")
    assert broker.orders == [(SYMBOL, 150, "SELL")]


def test_a_short_book_row_is_sized_on_magnitude():
    broker = _Broker(positions=_positions(-75))
    _manager(broker).emergency_flatten_trade(_trade(broker_quantity=0), "force exit")
    assert broker.orders and broker.orders[0][1] == 75


def test_a_junk_netqty_is_not_guessed_at():
    broker = _Broker(positions={"data": [{"tradingsymbol": SYMBOL, "netqty": "abc"}]})
    assert _manager(broker).emergency_flatten_trade(_trade(), "force exit") is False
    assert broker.orders == []


# ── the sibling contract this was aligned to ─────────────────────────────────

def test_the_sibling_guard_still_exists():
    """If _maybe_auto_flatten_unprotected drops its guard, re-read this reasoning."""
    import inspect

    src = inspect.getsource(TradeManager._maybe_auto_flatten_unprotected)
    assert "_confirmed_broker_quantity(trade) <= 0" in src


def test_the_flatten_no_longer_reads_the_intended_quantity():
    import inspect

    src = inspect.getsource(TradeManager.emergency_flatten_trade)
    assert "trade.broker_quantity or trade.quantity" not in src, (
        "still falls back to the intended quantity when no fill was confirmed"
    )
    assert "_confirmed_broker_quantity" in src
