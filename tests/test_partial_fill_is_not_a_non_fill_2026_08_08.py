"""A partially filled order that was then cancelled came back as a proven non-fill.

`cancel_order_confirmed` returns True only for "a broker-confirmed safe terminal state" —
its contract is explicitly "nothing is resting, nothing will fill". It decided that from
the STATUS alone:

    safe_terminal = {"cancelled", "canceled", "rejected", "expired", "lapsed"}
    if status in safe_terminal:
        return True

Kite reports an order that partially executed and was then cancelled as CANCELLED with a
non-zero filled quantity. `_normalise_order` even carries that value through as
`filledshares`, and nothing on the live path read it.

The stale-entry cancel/replace acts only on True and then places a replacement for the
FULL quantity. A limit that filled 1 of 3 lots and was cancelled came back True and was
topped up with 3 more: a 4-lot position where the sizer approved 3. The protective stop is
then armed on the enlarged netqty, so one stop-out costs about a third more than the
per-trade risk that was approved.

This is the exact race the cancel/replace block was written to survive — only the
FULL-fill half had been covered.
"""
import pytest

from trading.broker import Broker


class _Book(Broker):
    """Minimal Broker with a scripted order book."""

    def __init__(self, row, cancel_ok=True):
        self._row = row
        self._cancel_ok = cancel_ok

    def cancel_order(self, order_id, variety="NORMAL"):
        return self._cancel_ok

    def get_order_status(self, order_id):
        return self._row


def _confirm(row, **kw):
    return Broker.cancel_order_confirmed(_Book(row), "O-1", timeout_seconds=0.0, **kw)


# ── the partial-fill case ────────────────────────────────────────────────────

def test_a_cancelled_order_with_a_partial_fill_is_not_a_non_fill():
    assert _confirm({"status": "CANCELLED", "filledshares": 65}) is False


def test_a_cleanly_cancelled_order_is_still_a_proven_non_fill():
    assert _confirm({"status": "CANCELLED", "filledshares": 0}) is True


@pytest.mark.parametrize(
    "field", ["filledshares", "filled_quantity", "filledQuantity", "filled"]
)
def test_every_filled_quantity_spelling_is_read(field):
    assert _confirm({"status": "CANCELLED", field: 30}) is False


@pytest.mark.parametrize("status", ["cancelled", "canceled", "rejected", "expired", "lapsed"])
def test_the_rule_applies_to_every_terminal_status(status):
    assert _confirm({"status": status, "filledshares": 25}) is False
    assert _confirm({"status": status, "filledshares": 0}) is True


def test_a_missing_filled_field_is_treated_as_unfilled():
    """Absent is the common clean case; it must not become 'unknown'."""
    assert _confirm({"status": "CANCELLED"}) is True


def test_a_junk_filled_value_does_not_raise():
    assert _confirm({"status": "CANCELLED", "filledshares": "n/a"}) is True


def test_a_negative_filled_quantity_still_counts_as_a_fill():
    assert _confirm({"status": "CANCELLED", "filledshares": -65}) is False


# ── the other states are unchanged ───────────────────────────────────────────

def test_a_completed_order_is_still_a_fill():
    assert _confirm({"status": "COMPLETE", "filledshares": 65}) is False


def test_an_unreadable_book_is_still_unknown():
    class _Blind(Broker):
        def cancel_order(self, order_id, variety="NORMAL"):
            return True

    assert Broker.cancel_order_confirmed(_Blind(), "O-1", timeout_seconds=0.0) is None


def test_a_still_open_order_is_unknown_not_safe():
    assert _confirm({"status": "OPEN", "filledshares": 0}) is None


# ── the consequence this protects ────────────────────────────────────────────

def test_the_replace_path_only_acts_on_a_proven_non_fill():
    """If it ever acts on False or None, this fix stops protecting anything."""
    import inspect

    from trading.trade_manager import TradeManager

    src = inspect.getsource(TradeManager)
    assert "if cancelled is True:" in src, (
        "the stale-entry replace no longer requires a PROVEN non-fill"
    )
