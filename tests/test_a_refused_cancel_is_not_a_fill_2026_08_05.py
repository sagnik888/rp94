"""A cancel the broker refused is unread, not filled.

cancel_order_confirmed is a tri-state and its docstring is explicit about what each
value claims:

    True  - broker-confirmed safe terminal state; nothing is resting, nothing will fill
    False - the order FILLED
    None  - final state unknown

Line 148 read `if not self.cancel_order(...): return False`, so every refused cancel
asserted "the order filled". A refused cancel is usually a network error, a rate limit,
an expired session or a blocked IP — all of which mean the order's state is simply
unread. And the one reading that was flatly backwards: a broker also refuses to cancel an
order that has ALREADY gone terminal, so "someone else cancelled it" reported as "it
filled".

Both current callers happen to treat False and None alike (`is not True` aborts), so this
is a latent contract defect rather than a live loss — but it is also a capability the
system was throwing away, because the order book could have answered in cases where the
cancel API could not.
"""
import pytest

from trading.broker import Broker


class _Base(Broker):
    """Broker with a scriptable cancel API and order book."""

    def __init__(self, cancel_ok, statuses):
        self.cancel_ok = cancel_ok
        self.statuses = list(statuses)
        self.cancel_calls = 0
        self.status_calls = 0

    def cancel_order(self, order_id, variety="NORMAL"):
        self.cancel_calls += 1
        return self.cancel_ok

    def get_order_status(self, order_id):
        self.status_calls += 1
        if not self.statuses:
            return None
        row = self.statuses.pop(0)
        return {"orderid": order_id, "orderstatus": row} if row else None


def _confirm(cancel_ok, statuses, **kw):
    b = _Base(cancel_ok, statuses)
    kw.setdefault("timeout_seconds", 0.05)
    kw.setdefault("poll_seconds", 0.01)
    return b.cancel_order_confirmed("OID-1", **kw), b


# ── the defect ───────────────────────────────────────────────────────────────

def test_a_refused_cancel_no_longer_claims_the_order_filled():
    verdict, _ = _confirm(False, [])
    assert verdict is None, (
        "a refused cancel returned False, which under this contract is the specific "
        "claim 'the order filled'"
    )


def test_a_refused_cancel_on_an_already_cancelled_order_reads_true():
    """The backwards case: the broker refuses BECAUSE it is already terminal."""
    verdict, _ = _confirm(False, ["cancelled"])
    assert verdict is True, (
        "an order the broker had already cancelled was reported as filled"
    )


def test_a_refused_cancel_on_a_rejected_order_reads_true():
    verdict, _ = _confirm(False, ["rejected"])
    assert verdict is True


def test_a_refused_cancel_still_reports_a_genuine_fill():
    """False must keep meaning what it says."""
    verdict, _ = _confirm(False, ["complete"])
    assert verdict is False


def test_a_refused_cancel_consults_the_order_book_at_all():
    _verdict, broker = _confirm(False, ["cancelled"])
    assert broker.status_calls >= 1, (
        "returned a verdict without ever reading the authority on the question"
    )


# ── the accepted-cancel path is unchanged ────────────────────────────────────

def test_an_accepted_cancel_still_needs_proof():
    """An API acknowledgement alone was never enough and still is not."""
    verdict, _ = _confirm(True, [None, None, None])
    assert verdict is None


def test_an_accepted_cancel_confirmed_terminal_is_true():
    assert _confirm(True, ["cancelled"])[0] is True


def test_an_accepted_cancel_that_lost_the_race_is_false():
    """The fill beat the cancel — the case this whole function exists for."""
    assert _confirm(True, ["complete"])[0] is False


def test_an_order_still_open_stays_unknown_until_the_deadline():
    assert _confirm(True, ["open", "open", "open", "open"])[0] is None


def test_a_late_terminal_status_is_still_caught():
    assert _confirm(True, ["open", "trigger pending", "cancelled"])[0] is True


# ── degenerate inputs ────────────────────────────────────────────────────────

def test_no_order_id_is_trivially_safe():
    assert _Base(False, []).cancel_order_confirmed("") is True


def test_no_order_book_reader_is_unknown_not_filled():
    class _Blind(Broker):
        def cancel_order(self, order_id, variety="NORMAL"):
            return False

    blind = _Blind()
    blind.get_order_status = None
    assert blind.cancel_order_confirmed("OID-1", timeout_seconds=0.05) is None


def test_a_refused_cancel_with_an_unreadable_book_returns_promptly():
    """Nothing will change by waiting; do not burn the caller's timeout."""
    import time as _time

    started = _time.monotonic()
    verdict, broker = _confirm(False, [], timeout_seconds=5.0, poll_seconds=0.5)
    assert verdict is None
    assert _time.monotonic() - started < 1.0
    assert broker.status_calls == 1


def test_an_accepted_cancel_with_an_unreadable_book_still_waits():
    """A cancel in flight CAN change the book; that wait is worth having."""
    import time as _time

    started = _time.monotonic()
    verdict, broker = _confirm(True, [], timeout_seconds=0.20, poll_seconds=0.05)
    assert verdict is None
    assert _time.monotonic() - started >= 0.15
    assert broker.status_calls > 1


# ── the callers that consume the tri-state ───────────────────────────────────

def test_partial_derisk_still_aborts_on_an_unproven_cancel():
    """The caller reads `is not True`, so None must keep aborting the de-risk.

    A full-size SL-M that may still be resting, plus a partial exit on top of it,
    exits the position twice.
    """
    import threading
    from datetime import datetime

    from trading.trade_manager import IST, TradeManager

    manager = TradeManager.__new__(TradeManager)
    manager.broker = _Base(False, [])
    manager.product_type = "CARRYFORWARD"
    manager._lock = threading.RLock()
    closed = []
    manager.broker.close_order = lambda *a, **k: closed.append(a) or "X"

    trade = type("T", (), {})()
    trade.protective_order_id = "SL-1"
    trade.trading_symbol = "NIFTY2680524500CE"
    trade.symbol_token = "1"

    manager._run_partial_reduce(trade, 1, 75, 100.0, "TEST", datetime.now(IST))
    assert closed == [], "reduced on top of an unproven full-size stop"
    assert trade.protective_order_id == "SL-1"
