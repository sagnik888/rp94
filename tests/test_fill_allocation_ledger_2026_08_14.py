"""Ownership derived from each order's own fill, instead of split from an aggregate.

Kite reports POSITIONS per tradingsymbol. When several trades share a contract the
broker cannot say which trade owns which quantity, so reconcile split the aggregate by
a rule: confirmed fills first, then oldest-first by intended size. That rule holds on
the way in and fails on the way out — once a sibling exits, the aggregate shrinks and
nothing in it says WHOSE quantity left. broker_quantity SIZES protective stops, so a
wrong guess arms the wrong size.

An ORDER, by contrast, belongs to exactly one trade, and its fill is a fact the broker
states. The ledger records those facts, keyed on the broker order id, and a trade's
holding becomes the signed sum of its own fills. Persisted, so it survives the restart
that would otherwise lose the attribution entirely.

Three invariants this pins:

  ABSENT IS NOT ZERO   owned_quantity returns None when the ledger has no rows for a
                       trade, and 0 only when its own orders net to nothing. Collapsing
                       them would either strip protection from a live trade or fall back
                       to intended size and arm quantity the account does not hold.

  IDEMPOTENT           order_id is the PRIMARY KEY, so a retry, a reconcile re-read and
                       a restart replay all write the same row rather than double-count.

  THE BROKER CAPS      the ledger is authoritative about ATTRIBUTION, the broker about
                       TOTAL exposure. Where they disagree the aggregate wins on size,
                       so nothing is ever armed for more than the account holds.
"""
import os
import tempfile
import threading
import uuid

import pytest

from trading.trade_manager import TradeManager


def _ledger():
    """A DatabaseManager on its own file, isolated from the production ledger."""
    from data.sqlite_db import DatabaseManager, _resolve_db_path

    path = os.path.join(tempfile.gettempdir(), f"fill_ledger_{uuid.uuid4().hex}.db")
    handle = object.__new__(DatabaseManager)
    handle._init_db(path)
    return handle


@pytest.fixture
def led():
    handle = _ledger()
    yield handle
    try:
        handle.conn.close()
    except Exception:
        pass


class _T:
    def __init__(self, tid, quantity, broker_quantity=0, entry_time=""):
        self.id = tid
        self._q = quantity
        self.quantity = quantity
        self.broker_quantity = broker_quantity
        self.entry_time = entry_time


# ── absent is not zero ───────────────────────────────────────────────────────

def test_an_unrecorded_trade_reads_as_no_information(led):
    assert led.owned_quantity("never-seen") is None


def test_a_fully_exited_trade_reads_as_a_real_zero(led):
    led.record_order_fill("O1", "T1", 75, tradingsymbol="X", leg="ENTRY")
    led.record_order_fill("O2", "T1", -75, tradingsymbol="X", leg="FULL_EXIT")

    assert led.owned_quantity("T1") == 0, "a flat trade is 0, not None"


def test_a_blank_trade_id_reads_as_no_information(led):
    assert led.owned_quantity("") is None


# ── idempotence ──────────────────────────────────────────────────────────────

def test_recording_the_same_order_twice_does_not_double_count(led):
    led.record_order_fill("O1", "T1", 75, tradingsymbol="X", leg="ENTRY")
    led.record_order_fill("O1", "T1", 75, tradingsymbol="X", leg="ENTRY")

    assert led.owned_quantity("T1") == 75


def test_a_replayed_fill_can_correct_itself(led):
    """Same order, better information: the row is replaced, not added to."""
    led.record_order_fill("O1", "T1", 75, tradingsymbol="X", leg="ENTRY")
    led.record_order_fill("O1", "T1", 65, tradingsymbol="X", leg="ENTRY")

    assert led.owned_quantity("T1") == 65


def test_a_fill_without_an_order_id_is_refused(led):
    """An unattributable row would poison the sum."""
    assert led.record_order_fill("", "T1", 75) is False
    assert led.owned_quantity("T1") is None


def test_a_fill_without_a_trade_id_is_refused(led):
    assert led.record_order_fill("O1", "", 75) is False


# ── the case the splitting rule could not get right ──────────────────────────

def test_a_sibling_exiting_leaves_the_others_untouched(led):
    """Three trades on one strike; the middle one closes.

    The aggregate drops from 225 to 150 and says nothing about whose quantity left.
    Each trade's own orders do.
    """
    for tid, oid in (("T1", "O1"), ("T2", "O2"), ("T3", "O3")):
        led.record_order_fill(oid, tid, 75, tradingsymbol="NIFTY24300CE", leg="ENTRY")
    led.record_order_fill("O4", "T2", -75, tradingsymbol="NIFTY24300CE", leg="FULL_EXIT")

    assert led.owned_quantity("T1") == 75
    assert led.owned_quantity("T2") == 0
    assert led.owned_quantity("T3") == 75


def test_a_partial_exit_reduces_only_its_own_trade(led):
    led.record_order_fill("O1", "T1", 150, tradingsymbol="X", leg="ENTRY")
    led.record_order_fill("O2", "T2", 75, tradingsymbol="X", leg="ENTRY")
    led.record_order_fill("O3", "T1", -75, tradingsymbol="X", leg="PARTIAL_EXIT")

    assert led.owned_quantity("T1") == 75
    assert led.owned_quantity("T2") == 75


def test_a_scale_in_adds_to_its_own_trade(led):
    led.record_order_fill("O1", "T1", 75, tradingsymbol="X", leg="ENTRY")
    led.record_order_fill("O2", "T1", 75, tradingsymbol="X", leg="ADD")

    assert led.owned_quantity("T1") == 150


def test_the_symbol_view_lists_every_trade_on_a_contract(led):
    led.record_order_fill("O1", "T1", 75, tradingsymbol="NIFTY24300CE", leg="ENTRY")
    led.record_order_fill("O2", "T2", 75, tradingsymbol="NIFTY24300CE", leg="ENTRY")
    led.record_order_fill("O3", "T3", 30, tradingsymbol="BANKNIFTY52000CE", leg="ENTRY")

    assert led.owned_quantities_for_symbol("NIFTY24300CE") == {"T1": 75, "T2": 75}


# ── persistence across a restart ─────────────────────────────────────────────

def test_the_ledger_survives_a_reopen(led):
    """The whole point of persisting: a restart must not lose attribution."""
    from data.sqlite_db import DatabaseManager

    led.record_order_fill("O1", "T1", 75, tradingsymbol="X", leg="ENTRY")
    path = str(led.db_path)
    led.conn.close()

    reopened = object.__new__(DatabaseManager)
    reopened._init_db(path)
    try:
        assert reopened.owned_quantity("T1") == 75
    finally:
        reopened.conn.close()


# ── the allocator prefers the ledger ─────────────────────────────────────────

def _alloc(trades, net, ledger=None):
    return TradeManager._allocate_broker_quantity(list(trades.items()), net, ledger)


def test_the_ledger_decides_when_it_knows():
    """Ledger says T1 holds everything; the oldest-first rule would have split it."""
    trades = {
        "T1": _T("T1", 75, entry_time="2"),
        "T2": _T("T2", 75, entry_time="1"),
    }
    known = {"T1": 75, "T2": 0}

    assert _alloc(trades, 75, known.get) == {"T1": 75, "T2": 0}


def test_a_silent_ledger_falls_back_to_the_previous_rule():
    trades = {
        "T1": _T("T1", 75, entry_time="1"),
        "T2": _T("T2", 75, entry_time="2"),
    }

    assert _alloc(trades, 150, lambda _tid: None) == {"T1": 75, "T2": 75}


def test_a_partially_known_ledger_mixes_both():
    """One trade recorded, one not: the known one is honoured, the rest inferred."""
    trades = {
        "T1": _T("T1", 75, entry_time="1"),
        "T2": _T("T2", 75, entry_time="2"),
    }
    known = {"T1": 0}

    assert _alloc(trades, 75, known.get) == {"T1": 0, "T2": 75}


def test_a_known_zero_is_honoured_and_never_becomes_intended_size():
    """The reviewer's specific requirement, and the reason None != 0."""
    trades = {
        "T1": _T("T1", 75, entry_time="1"),
        "T2": _T("T2", 75, entry_time="2"),
    }
    known = {"T1": 0, "T2": 0}

    assert _alloc(trades, 150, known.get) == {"T1": 0, "T2": 0}


def test_the_broker_aggregate_still_caps_the_ledger():
    """Attribution is the ledger's; total exposure is the broker's."""
    trades = {
        "T1": _T("T1", 75, entry_time="1"),
        "T2": _T("T2", 75, entry_time="2"),
    }
    known = {"T1": 75, "T2": 75}  # ledger claims 150

    got = _alloc(trades, 75, known.get)  # broker holds only 75

    assert sum(got.values()) <= 75, "armed for more than the account holds"


def test_a_lone_trade_is_unaffected_by_the_ledger():
    """Single-trade behaviour, including broker excess, must not shift."""
    assert _alloc({"solo": _T("solo", 75)}, 225, lambda _t: 75) == {"solo": 225}


# ── wiring ───────────────────────────────────────────────────────────────────

def test_reconcile_passes_the_ledger_to_the_allocator():
    import inspect

    src = inspect.getsource(TradeManager.reconcile_broker_positions)

    assert "_ledger_owned_quantity" in src


@pytest.mark.parametrize(
    "method,leg",
    [
        ("on_broker_postback", "ENTRY"),
        ("reconcile_broker_positions", "ENTRY"),
        ("_book_scale_in", "ADD"),
        ("_run_partial_reduce", "PARTIAL_EXIT"),
        ("_finalize_closed_trade", "FULL_EXIT"),
    ],
)
def test_every_leg_that_changes_a_holding_records_a_fill(method, leg):
    """A leg that moves quantity without a ledger row silently breaks the sum."""
    import inspect

    fn = getattr(TradeManager, method, None)
    if fn is None:
        pytest.skip(f"{method} not present under that name")
    src = inspect.getsource(fn)

    recorded = "_record_fill(" in src or "_record_leg_fill_from_order(" in src
    assert recorded, f"{method} changes a holding without recording it"
    assert leg in src


def test_exit_and_add_legs_are_recorded_from_the_ORDER_not_the_aggregate():
    """The guess must not be written INTO the ledger.

    _confirmed_reduced_quantity and _confirmed_scaled_quantity read the
    per-tradingsymbol AGGREGATE. With two 75s on one strike and this trade reducing 75,
    the aggregate falls 150 -> 75 and `broker_quantity - confirmed` computes as zero --
    the exact ambiguity the ledger exists to remove. These legs must read the order's
    own executions instead.
    """
    import inspect

    for method in ("_run_partial_reduce", "_book_scale_in"):
        src = inspect.getsource(getattr(TradeManager, method))
        assert "_record_leg_fill_from_order(" in src, (
            f"{method} still records from a positions-derived number"
        )


def test_an_unknown_order_fill_records_nothing_rather_than_zero():
    """Silence falls back to the older rule; a wrong row looks authoritative."""
    import inspect

    src = inspect.getsource(TradeManager._record_leg_fill_from_order)

    assert "if not qty:" in src and "return False" in src


def test_the_order_fill_lookup_keeps_unknown_distinct_from_no_fill():
    import inspect

    src = inspect.getsource(TradeManager._order_filled_quantity)

    assert '"NO_FILL"' in src and "return 0" in src
    assert "return None" in src


def test_recording_never_raises_into_an_order_path():
    """The ledger improves attribution; it must not be able to break execution."""
    import inspect

    src = inspect.getsource(TradeManager._record_fill)

    assert "except Exception" in src
