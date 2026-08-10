"""Cascade adds reached the broker with no funds check and took no reservation.

Every new entry funnels through _entry_margin_verdict -> preflight_entry_margin, which
compares the exact Kite order margin against free cash AND reserves it so a concurrent
entry cannot spend the same rupees. The add path — _offer_cascade_add -> scale_in_trade
-> _request_broker_scale_in -> _run_scale_in — went straight to place_order.

The missing RESERVATION is the sharper half. With no record that the add committed money,
an ordinary entry arriving seconds later reads `available - outstanding` as if the add
never happened, passes its own preflight, and the account over-commits — the exact race
the reservation ledger was built to prevent.

Nothing else gated it: _entry_risk_admission is entry_risk_budget_enabled-gated and that
ships False, so an enabled scale-in had no rupee gate whatsoever.

UNREACHABLE TODAY — scale_in_enabled ships False, so no cascade add is placed at all.
Fixed anyway because the flag is a live dashboard toggle and flipping it would otherwise
arm an unfunded, unreserved order path.

Fail-open, exactly like an entry: a missing or erroring margin endpoint must never stop
trading, only a definitive refusal may. And a refusal must leave the PARENT position
untouched — it is still running and still protected by the stop covering its quantity.
"""
from types import SimpleNamespace

import pytest

from trading.trade_manager import TradeManager
from trading.order_retry import BrokerOrderAmbiguous, BrokerOrderRejected


def _trade():
    return SimpleNamespace(
        id="T1", trading_symbol="NIFTY25000CE", symbol_token="1234",
        inst_type="OPT", direction="LONG", quantity=65, broker_quantity=65,
        lots=1, scale_in_order_id="", scale_in_requested_quantity=0,
        execution_status="OPEN", status="OPEN", execution_error="",
    )


def _tm(*, verdict, place=None, released=None):
    tm = TradeManager.__new__(TradeManager)
    calls = {"placed": 0, "released": []}

    def _preflight(**kw):
        calls.update(preflight_kw=kw)
        if verdict is RuntimeError:
            raise RuntimeError("margin endpoint down")
        return verdict

    def _place(*a, **k):
        calls["placed"] += 1
        if place is not None:
            raise place
        return "ORD1"

    tm.broker = SimpleNamespace(
        preflight_entry_margin=_preflight,
        place_order=_place,
        release_entry_margin_reservation=lambda rid: calls["released"].append(rid),
    )
    tm.product_type = "CARRYFORWARD"
    tm._tag_kwargs = lambda fn, t, tag: {}
    tm._mark_recovery = lambda t, msg: calls.update(recovery=msg)
    tm.save_state = lambda: None
    tm._lock = __import__("threading").RLock()
    tm._confirmed_scaled_quantity = lambda t, exp, **k: exp
    tm._book_scale_in = lambda t, l, p, r: None
    tm.ensure_broker_protection = lambda t, q: True
    tm.calls = calls
    return tm


def _run(tm, trade=None):
    TradeManager._run_scale_in(tm, trade or _trade(), 1, 65, 120.0, "CASCADE_ADD")


OK = {"ok": True, "reservation_id": "R1", "limitation": ""}
NO = {"ok": False, "reservation_id": "", "limitation": "insufficient funds"}


# ── the gate ─────────────────────────────────────────────────────────────────

def test_an_unfunded_add_never_reaches_the_broker():
    tm = _tm(verdict=NO)
    _run(tm)
    assert tm.calls["placed"] == 0, "the add was sent with no money behind it"


def test_a_funded_add_is_placed():
    tm = _tm(verdict=OK)
    _run(tm)
    assert tm.calls["placed"] == 1


def test_the_preflight_is_asked_about_the_add_quantity_not_the_parent():
    tm = _tm(verdict=OK)
    _run(tm)
    assert tm.calls["preflight_kw"]["qty"] == 65
    assert tm.calls["preflight_kw"]["side"] == "BUY"


def test_an_option_add_is_always_a_buy():
    """Options are always bought — a bearish view is a bought PE."""
    tm = _tm(verdict=OK)
    t = _trade()
    t.direction = "SHORT"
    _run(tm, t)
    assert tm.calls["preflight_kw"]["side"] == "BUY"


# ── fail-open ────────────────────────────────────────────────────────────────

def test_a_broker_with_no_preflight_still_adds():
    tm = _tm(verdict=OK)
    del tm.broker.preflight_entry_margin
    _run(tm)
    assert tm.calls["placed"] == 1, "a missing margin endpoint stopped trading"


def test_a_raising_preflight_still_adds():
    tm = _tm(verdict=RuntimeError)
    _run(tm)
    assert tm.calls["placed"] == 1


def test_an_unverified_margin_still_adds():
    tm = _tm(verdict={"ok": True, "reservation_id": "", "basis": "MARGIN_UNVERIFIED"})
    _run(tm)
    assert tm.calls["placed"] == 1


# ── the reservation ──────────────────────────────────────────────────────────

def test_a_definite_rejection_gives_the_margin_back():
    tm = _tm(verdict=OK, place=BrokerOrderRejected("RMS"))
    _run(tm)
    assert tm.calls["released"] == ["R1"]


def test_no_order_id_gives_the_margin_back():
    tm = _tm(verdict=OK)
    tm.broker.place_order = lambda *a, **k: ""
    _run(tm)
    assert tm.calls["released"] == ["R1"]


def test_an_ambiguous_acknowledgement_keeps_holding_the_margin():
    """That order may be live. Releasing here lets the next entry spend money this one
    may already be consuming."""
    tm = _tm(verdict=OK, place=BrokerOrderAmbiguous("timeout"))
    _run(tm)
    assert tm.calls["released"] == [], "margin released while the add may be live"


def test_a_successful_add_does_not_release_early():
    tm = _tm(verdict=OK)
    _run(tm)
    assert tm.calls["released"] == []


# ── the parent position is never harmed ──────────────────────────────────────

def test_a_refused_add_leaves_the_parent_untouched():
    tm = _tm(verdict=NO)
    t = _trade()
    before = (t.status, t.execution_status, t.broker_quantity, t.scale_in_order_id)
    _run(tm, t)
    assert (t.status, t.execution_status, t.broker_quantity, t.scale_in_order_id) == before


def test_a_refused_add_does_not_mark_recovery():
    """A funds refusal is an ordinary no, not a state needing human reconciliation."""
    tm = _tm(verdict=NO)
    _run(tm)
    assert "recovery" not in tm.calls


# ── the premise ──────────────────────────────────────────────────────────────

def test_scale_in_is_still_disabled_in_the_shipped_config():
    """Records why this is defensive. If it flips, this path is now funded."""
    from config.settings import get_settings

    assert bool(getattr(get_settings(), "scale_in_enabled", False)) is False


def test_the_other_rupee_gate_is_still_off_too():
    """Why the preflight had to be added rather than relying on the risk budget."""
    from config.settings import get_settings

    assert bool(getattr(get_settings(), "entry_risk_budget_enabled", False)) is False
