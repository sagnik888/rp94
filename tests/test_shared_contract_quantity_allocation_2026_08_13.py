"""One broker position row must not become every local trade's quantity.

Kite reports POSITIONS per tradingsymbol, not per order. reconcile_broker_positions
builds its map keyed by symbol:

    broker_pos = {str(row.get("tradingsymbol") or ""): row for row in rows ...}
    for trade_id, trade in local_snapshot:
        symbol = str(trade.trading_symbol or trade.instrument)
        position = broker_pos.get(symbol)      # SAME row for every trade on the strike
        ...
        trade.broker_quantity = abs(net_qty)   # ...written into EVERY one of them

Three 75-lot trades on NIFTY 24300CE against a broker net of 225 each ended up with
broker_quantity = 225.

That field is authoritative for order SIZING, not just reporting:

    ensure_broker_protection(trade, trade.broker_quantity)   the SL-M quantity
    _confirmed_broker_quantity(trade)                        the flatten quantity
    _request_broker_exit                                     the exit quantity

so each trade armed a 225 stop against the 75 it actually held. On options an
oversized SL-M sells more than the account owns -- a naked written option, the
outcome this file's own comments call the one that must never happen.

A previous test in this repo asserted the shared row "is used for ONE thing: orphan
detection". That was wrong: it checked that _confirmed_broker_quantity READS
trade.broker_quantity and never checked what WRITES it. The reader was faithful; the
value it faithfully returned was corrupt. These tests cover the write.
"""
import pytest

from trading.trade_manager import TradeManager


class _T:
    def __init__(self, quantity, broker_quantity=0, entry_time=""):
        self.quantity = quantity
        self.broker_quantity = broker_quantity
        self.entry_time = entry_time


def _alloc(trades, net):
    return TradeManager._allocate_broker_quantity(list(trades.items()), net)


# ── the reported P0 ──────────────────────────────────────────────────────────

def test_three_trades_sharing_a_strike_do_not_each_claim_the_whole_position():
    """The audit's reproduction: 3 x 75 against a broker net of 225."""
    trades = {
        "t-5min": _T(75, entry_time="09:20"),
        "t-15min": _T(75, entry_time="09:35"),
        "t-1min": _T(75, entry_time="09:50"),
    }

    got = _alloc(trades, 225)

    assert got == {"t-5min": 75, "t-15min": 75, "t-1min": 75}
    assert sum(got.values()) == 225, "the split must still account for the whole position"


def test_no_trade_is_ever_handed_the_aggregate():
    trades = {"a": _T(75, entry_time="1"), "b": _T(75, entry_time="2"), "c": _T(75, entry_time="3")}

    assert max(_alloc(trades, 225).values()) == 75


def test_uneven_sizes_are_allocated_to_their_own_intent():
    trades = {"a": _T(75, entry_time="1"), "b": _T(150, entry_time="2")}

    assert _alloc(trades, 225) == {"a": 75, "b": 150}


# ── single trade: unchanged, including a genuine excess ──────────────────────

def test_a_lone_trade_still_receives_the_row_verbatim():
    """Preserves every existing single-trade path byte-for-byte."""
    assert _alloc({"solo": _T(75)}, 75) == {"solo": 75}


def test_a_lone_trade_still_sees_broker_excess():
    """Callers detect excess by comparing against trade.quantity; capping would hide it."""
    assert _alloc({"solo": _T(75)}, 225) == {"solo": 225}


def test_a_lone_trade_with_a_short_position_gets_the_absolute_value():
    assert _alloc({"solo": _T(75)}, -75) == {"solo": 75}


# ── a confirmed own fill outranks the split ──────────────────────────────────

def test_a_trade_whose_own_fill_was_confirmed_keeps_it():
    """Per-order fills are evidence; a positions-derived split is a guess."""
    trades = {
        "filled": _T(75, broker_quantity=75, entry_time="1"),
        "unconfirmed": _T(150, entry_time="2"),
    }

    assert _alloc(trades, 225) == {"filled": 75, "unconfirmed": 150}


def test_confirmed_fills_are_honoured_before_intent_even_when_short():
    """Net covers only the confirmed fill: the unconfirmed trade gets nothing, not a guess."""
    trades = {
        "filled": _T(75, broker_quantity=75, entry_time="1"),
        "unconfirmed": _T(75, entry_time="2"),
    }

    assert _alloc(trades, 75) == {"filled": 75, "unconfirmed": 0}


# ── under-allocation is the safe direction ───────────────────────────────────

def test_a_short_position_underfills_the_youngest_trade():
    """Broker holds 150 but three trades claim 75 each -- one of them is not really there.

    Every unfunded path in TradeManager is fail-safe on 0 (it defers rather than
    exits), so allocating 0 makes the system hold off instead of arming a stop for
    quantity the account does not hold.
    """
    trades = {"a": _T(75, entry_time="1"), "b": _T(75, entry_time="2"), "c": _T(75, entry_time="3")}

    got = _alloc(trades, 150)

    assert got == {"a": 75, "b": 75, "c": 0}
    assert sum(got.values()) == 150


def test_the_total_allocated_never_exceeds_the_broker_position():
    """The invariant that keeps a stop from selling what the account does not hold."""
    trades = {"a": _T(75, entry_time="1"), "b": _T(75, entry_time="2"), "c": _T(75, entry_time="3")}

    for net in (0, 1, 74, 75, 149, 150, 224, 225):
        assert sum(_alloc(trades, net).values()) <= net


def test_a_flat_row_allocates_nothing_to_anyone():
    trades = {"a": _T(75, entry_time="1"), "b": _T(75, entry_time="2")}

    assert _alloc(trades, 0) == {"a": 0, "b": 0}


# ── determinism and robustness ───────────────────────────────────────────────

def test_allocation_is_deterministic_regardless_of_dict_order():
    a = {"x": _T(75, entry_time="09:20"), "y": _T(75, entry_time="09:35")}
    b = {"y": _T(75, entry_time="09:35"), "x": _T(75, entry_time="09:20")}

    assert _alloc(a, 75) == _alloc(b, 75)


def test_the_oldest_trade_is_served_first():
    trades = {"younger": _T(75, entry_time="10:00"), "older": _T(75, entry_time="09:00")}

    assert _alloc(trades, 75) == {"older": 75, "younger": 0}


@pytest.mark.parametrize("bad", [None, "junk", ""])
def test_an_unusable_net_quantity_allocates_nothing(bad):
    trades = {"a": _T(75, entry_time="1"), "b": _T(75, entry_time="2")}

    assert _alloc(trades, bad) == {"a": 0, "b": 0}


@pytest.mark.parametrize("bad", [None, "junk"])
def test_an_unusable_trade_quantity_is_treated_as_zero(bad):
    trades = {"a": _T(bad, entry_time="1"), "b": _T(75, entry_time="2")}

    assert _alloc(trades, 150) == {"a": 0, "b": 75}


def test_no_trades_allocates_nothing():
    assert TradeManager._allocate_broker_quantity([], 225) == {}


# ── the reconcile pass actually uses the split ───────────────────────────────

def test_reconcile_writes_the_allocation_not_the_row():
    """Pinned at source: both write sites must use the per-trade share.

    `trade.broker_quantity = abs(net_qty)` is the defect itself; it must not return.
    """
    import inspect

    src = inspect.getsource(TradeManager.reconcile_broker_positions)

    assert "_allocate_broker_quantity(" in src, "reconcile no longer splits the row"
    assert "allocated_qty = broker_allocation.get(" in src
    assert src.count("trade.broker_quantity = allocated_qty") == 2, (
        "both the direction-mismatch and the normal write must use the allocation"
    )
    assert "trade.broker_quantity = abs(net_qty)" not in src, (
        "the aggregate is being written into a trade again"
    )
