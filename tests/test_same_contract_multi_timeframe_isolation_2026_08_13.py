"""Three timeframes, one strike: each trade must own only its own position.

The scenario: 5m, 15m and 1m each open NIFTY 24300CE at different times. Kite
reports POSITIONS per tradingsymbol, not per order, so reconciliation genuinely
does read one shared row for all three. A 2026-08-13 external audit read that as
"broker quantity is aggregated by symbol and assigned to every local trade
sharing that contract… two trades can each believe they own the full position,
corrupting stops, P&L and exits."

These mechanisms keep the trades apart on the READ side, and this pins them:

  ORDERS     tagged per trade — _broker_order_tag -> tagger(trade.id, leg, attempt)
  QUANTITY   per trade — _confirmed_broker_quantity reads trade.broker_quantity,
             never the shared position row
  EXITS      per trade — _request_broker_exit sends trade.broker_quantity (its own
             fill), falling back to trade.quantity; never the aggregate

CORRECTION (2026-08-13): this file originally concluded from the above that the
audit was wrong and "the shared row is used for ONE thing: orphan detection".
That was a bad inference. Every check here is on the READ path; none of them
asked what WRITES trade.broker_quantity. reconcile_broker_positions did:

    trade.broker_quantity = abs(net_qty)   # the aggregate, for every trade

so the readers pinned above were faithfully returning a corrupted value and these
tests still passed. The audit's P0 was real. The write is now split per trade by
_allocate_broker_quantity — see
test_shared_contract_quantity_allocation_2026_08_13.py, which covers the write
path this file does not.

The lesson worth keeping: proving a reader reads the right FIELD says nothing
about whether the field holds the right VALUE.

What remains true from the audit: with ut_concurrency_guard off (it is off in the
operator's .env) the timeframe/lane reasoning that would normally limit
same-contract stacking does not run, so only max_trades_per_index bounds it.
That is a position-count question, not an ownership one.
"""
import inspect

import pytest

from trading.trade_manager import TradeManager


class _T:
    """A trade with its own fill, as three same-strike trades would each have."""

    def __init__(self, trade_id, broker_quantity, quantity):
        self.id = trade_id
        self.broker_quantity = broker_quantity
        self.quantity = quantity
        self.is_ghost = False


# ── quantity is the trade's own, never the shared position row ───────────────

def test_each_trade_reports_its_own_confirmed_quantity():
    a = _T("t-5min", 75, 75)
    b = _T("t-15min", 150, 150)

    assert TradeManager._confirmed_broker_quantity(a) == 75
    assert TradeManager._confirmed_broker_quantity(b) == 150


def test_an_unfunded_trade_reports_zero_not_its_siblings_fill():
    """The case that would make a trade 'believe it owns the full position'."""
    unfunded = _T("t-1min", 0, 75)

    assert TradeManager._confirmed_broker_quantity(unfunded) == 0


@pytest.mark.parametrize("bad", [None, "junk", -5])
def test_an_unusable_quantity_reads_as_zero(bad):
    assert TradeManager._confirmed_broker_quantity(_T("t", bad, 75)) == 0


# ── the exit sends this trade's fill, not the aggregate ──────────────────────

def test_the_exit_quantity_comes_from_the_trades_own_fill():
    src = inspect.getsource(TradeManager._request_broker_exit)

    assert "trade.broker_quantity" in src, "exit must size on this trade's own fill"
    assert "position" not in src.split("exit_quantity")[1][:400], (
        "exit quantity must not be read from the shared per-symbol position row"
    )


# ── orders are distinguishable at the broker ─────────────────────────────────

def test_each_order_is_tagged_with_its_own_trade_id():
    src = inspect.getsource(TradeManager._broker_order_tag)

    assert "trade.id" in src, "the tag must carry the trade identity"


# ── the shared row cannot close a funded trade on its own ────────────────────

def test_orphan_detection_requires_the_trades_own_quantity_to_be_zero():
    """Reconciliation reads one position row for all three trades. It may only
    act when THIS trade is also unfunded — otherwise a sibling going flat would
    close a trade that holds real quantity."""
    src = inspect.getsource(TradeManager)
    i = src.index("_position_quantity(position) == 0")
    window = src[i: i + 300]

    assert "_confirmed_broker_quantity(trade)" in window, (
        "a flat shared position must not close a trade that has its own fill"
    )
