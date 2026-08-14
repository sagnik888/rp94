"""A sibling's quantity must not confirm another trade's order.

_confirmed_scaled_quantity and _confirmed_reduced_quantity proved a fill by watching the
per-tradingsymbol POSITION. That row describes the whole contract, so on a shared strike
it answers a different question than the caller asks:

  SCALE-IN, the dangerous direction. T1 holds 75 and adds 75, so expected_total is 150.
  Sibling T2 already holds 75, so the aggregate is ALREADY 150 before T1's order fills --
  150 >= 150 confirms instantly. The add is booked, the average entry price moves for
  lots that never arrived, and every R and P&L figure downstream is computed from it.

  REDUCE, the stuck direction. T1 reduces, but the aggregate still carries T2's lot and
  never falls to T1's expected remainder. A genuine reduce is never confirmed, so the
  protective stop is never re-armed on the remainder.

The ORDER answers directly and is asked first. The aggregate is still used when this
trade is alone on the contract, where it is unambiguous and is the better evidence --
it reflects the exchange rather than one order's view of itself.
"""
import threading
from types import SimpleNamespace

import pytest

from trading.trade_manager import TradeManager


SYMBOL = "NIFTY2681424300CE"


def _t(tid, qty, symbol=SYMBOL, ghost=False, **kw):
    t = SimpleNamespace(
        id=tid, trading_symbol=symbol, broker_quantity=qty, quantity=qty,
        direction="LONG", inst_type="OPT", is_ghost=ghost,
        partial_reduce_order_id="", scale_in_order_id="",
    )
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def _mgr(trades, fills=None, position_qty=None):
    m = TradeManager.__new__(TradeManager)
    m._lock = threading.RLock()
    m.open_trades = {t.id: t for t in trades}
    m.broker = SimpleNamespace(
        get_fill=lambda oid: (
            {"state": "FILLED", "quantity": fills[oid]}
            if fills and oid in fills and fills[oid]
            else {"state": "UNKNOWN"}
        )
    )
    m._live_position_for_trade = lambda _t: (
        ({"netqty": position_qty}, True) if position_qty is not None else (None, False)
    )
    return m


# ── who else is on the contract ──────────────────────────────────────────────

def test_a_lone_trade_is_sole_on_its_contract():
    t = _t("T1", 75)

    assert _mgr([t])._sole_trade_on_contract(t) is True


def test_a_sibling_on_the_same_strike_is_detected():
    t1, t2 = _t("T1", 75), _t("T2", 75)

    assert _mgr([t1, t2])._sole_trade_on_contract(t1) is False


def test_a_different_strike_is_not_a_sibling():
    t1 = _t("T1", 75)
    other = _t("T2", 75, symbol="NIFTY2681424350CE")

    assert _mgr([t1, other])._sole_trade_on_contract(t1) is True


def test_a_ghost_is_not_a_sibling():
    t1, ghost = _t("T1", 75), _t("G", 75, ghost=True)

    assert _mgr([t1, ghost])._sole_trade_on_contract(t1) is True


# ── the false confirmation this removes ──────────────────────────────────────

def test_a_siblings_quantity_no_longer_confirms_an_add():
    """The reported P0: aggregate already at expected_total before the add fills."""
    t1 = _t("T1", 75, scale_in_order_id="ADD-1")
    t2 = _t("T2", 75)
    mgr = _mgr([t1, t2], fills={"ADD-1": 0}, position_qty=150)

    assert mgr._confirmed_scaled_quantity(t1, 150, timeout=0.5) is None, (
        "a sibling's 75 confirmed an add that never filled"
    )


def test_an_add_that_really_filled_is_confirmed_from_its_own_order():
    t1 = _t("T1", 75, scale_in_order_id="ADD-1")
    t2 = _t("T2", 75)
    mgr = _mgr([t1, t2], fills={"ADD-1": 75}, position_qty=225)

    assert mgr._confirmed_scaled_quantity(t1, 150, timeout=0.5) == 150


def test_a_reduce_on_a_shared_contract_can_now_be_confirmed():
    """Previously impossible: the aggregate never fell to this trade's remainder."""
    t1 = _t("T1", 150, partial_reduce_order_id="RED-1")
    t2 = _t("T2", 75)
    mgr = _mgr([t1, t2], fills={"RED-1": 75}, position_qty=150)

    assert mgr._confirmed_reduced_quantity(t1, 75, timeout=0.5) == 75


def test_an_unfilled_reduce_stays_unproven():
    t1 = _t("T1", 150, partial_reduce_order_id="RED-1")
    t2 = _t("T2", 75)
    mgr = _mgr([t1, t2], fills={"RED-1": 0}, position_qty=225)

    assert mgr._confirmed_reduced_quantity(t1, 75, timeout=0.5) is None


# ── the single-trade case keeps its original evidence ────────────────────────

def test_a_lone_trade_still_confirms_an_add_from_positions():
    """No order id available, but the aggregate is unambiguous here."""
    t1 = _t("T1", 75)
    mgr = _mgr([t1], position_qty=150)

    assert mgr._confirmed_scaled_quantity(t1, 150, timeout=0.5) == 150


def test_a_lone_trade_still_confirms_a_reduce_from_positions():
    t1 = _t("T1", 150)
    mgr = _mgr([t1], position_qty=75)

    assert mgr._confirmed_reduced_quantity(t1, 75, timeout=0.5) == 75


def test_a_lone_trade_gone_flat_reads_as_zero():
    t1 = _t("T1", 75)
    mgr = _mgr([t1])
    mgr._live_position_for_trade = lambda _t: (None, True)

    assert mgr._confirmed_reduced_quantity(t1, 0, timeout=0.5) == 0


def test_a_shared_contract_without_an_order_id_refuses_to_guess():
    """No per-order evidence and an ambiguous aggregate: unproven is the honest answer."""
    t1, t2 = _t("T1", 75), _t("T2", 75)
    mgr = _mgr([t1, t2], position_qty=150)

    assert mgr._confirmed_scaled_quantity(t1, 150, timeout=0.5) is None
