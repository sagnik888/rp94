"""The ordinary exit path cancelled the protective stop on an acknowledgement.

Before sending a full-size MARKET exit, the exit path cancels the resting SL-M so the
position is not sold twice. It tested that cancel with:

    if not broker.cancel_order(trade.protective_order_id, "STOPLOSS"):

`KiteBroker.cancel_order` returns True whenever the HTTP call did not raise. That is an
acknowledgement, not a terminal state.

The SL-M trigger sits at `protective_trigger` — the SAME level the local trailing stop
fires at — so the cancel races the trigger at exactly the moment an exit is decided. Kite
can accept the cancel request while the triggered order still fills. The market exit then
sells on top of an SL-M that already sold: the position is exited twice, and because
options are always BOUGHT here the second fill is a NAKED WRITTEN option in
CARRYFORWARD/NRML, which the broker will not square off.

The proven variant already existed and its contract is explicit — True only for a
confirmed safe terminal state, False when it filled, None when unknown — and
`_run_partial_reduce` already used the `is not True` test with the rule written out:
"the fill routinely wins the race against the cancel". This path never adopted it.
"""
import threading
from types import SimpleNamespace

import pytest

from trading.trade_manager import TradeManager


class _Broker:
    """`cancel_order` always acknowledges; `cancel_order_confirmed` tells the truth."""

    def __init__(self, confirmed):
        self._confirmed = confirmed
        self.exits = []

    def cancel_order(self, order_id, variety="NORMAL"):
        return True

    def cancel_order_confirmed(self, order_id, variety="NORMAL", **kw):
        if isinstance(self._confirmed, Exception):
            raise self._confirmed
        return self._confirmed

    def close_order(self, *a, **kw):
        self.exits.append(a)
        return "EXIT-1"


class _AckOnlyBroker(_Broker):
    """A broker stub without the proven variant."""

    cancel_order_confirmed = None


def _cancel_decision(broker, order_id="SL-1"):
    """Reproduce the exit path's cancel decision."""
    from trading.order_retry import BrokerOrderRejected

    confirm = getattr(broker, "cancel_order_confirmed", None)
    if callable(confirm):
        try:
            cancelled = confirm(order_id, "STOPLOSS")
        except (BrokerOrderRejected, PermissionError):
            cancelled = None
    else:
        cancelled = True if broker.cancel_order(order_id, "STOPLOSS") else None
    return cancelled is True


# ── the race ─────────────────────────────────────────────────────────────────

def test_a_stop_that_already_filled_blocks_the_market_exit():
    """False means it filled. Exiting on top of it opens a naked short."""
    assert _cancel_decision(_Broker(confirmed=False)) is False


def test_an_unknown_cancel_state_blocks_the_market_exit():
    """None means unread. A failed read is not a state — withhold."""
    assert _cancel_decision(_Broker(confirmed=None)) is False


def test_a_refused_cancel_blocks_the_market_exit():
    assert _cancel_decision(_Broker(confirmed=PermissionError("lease held"))) is False


def test_a_proven_cancel_allows_the_market_exit():
    assert _cancel_decision(_Broker(confirmed=True)) is True


def test_a_mere_acknowledgement_is_not_enough():
    """The bug: cancel_order returns True even when the stop filled."""
    broker = _Broker(confirmed=False)
    assert broker.cancel_order("SL-1", "STOPLOSS") is True
    assert _cancel_decision(broker) is False, "acted on the acknowledgement"


def test_a_broker_without_the_proven_variant_keeps_working():
    """Partial broker stubs are a real pattern; this must not deadlock them."""
    assert _cancel_decision(_AckOnlyBroker(confirmed=None)) is True


# ── the wiring ───────────────────────────────────────────────────────────────

def test_the_exit_path_uses_the_proven_variant():
    import inspect

    src = inspect.getsource(TradeManager)
    marker = "Protective stop cancellation unconfirmed; market exit withheld"
    assert marker in src, "the exit-path withhold branch moved or was renamed"
    window = src[max(0, src.index(marker) - 2000):src.index(marker)]
    assert "cancel_order_confirmed" in window, (
        "the ordinary exit still cancels the protective stop on an acknowledgement"
    )
    assert "cancelled is not True" in window


def test_the_partial_reduce_contract_this_was_aligned_to_still_holds():
    """If _run_partial_reduce stops proving its cancel, re-read this reasoning."""
    import inspect

    src = inspect.getsource(TradeManager._run_partial_reduce)
    assert "cancel_order_confirmed" in src
    assert "cancelled is not True" in src


def test_the_proven_variant_still_returns_three_states():
    """True / False / None. If it collapses to a bool, the `is not True` test is wrong."""
    import inspect

    from trading.broker import Broker

    doc = inspect.getdoc(Broker.cancel_order_confirmed) or ""
    assert "None" in doc and "filled" in doc
