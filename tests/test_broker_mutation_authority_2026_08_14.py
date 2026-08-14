"""One gate inside the adapter, asked at the moment of every mutation.

Authority used to be checked by the CALLER, and in pieces: trade_manager asked its own
mode before queueing work, the lease was asked at seven separate sites inside the
adapter, and the system mode was never asked there at all. That leaves a real window --
a job queued while REAL and dispatched after a switch to HISTORICAL was already past
every check, and reconcile runs on a ~5s timer in every mode.

_authorize_mutation is the single gate: session, account lease, and system mode, asked
together at the point of send. Because the mode is read through a provider at call
time, a mode change between enqueue and dispatch is seen.

Strictness is keyed to the account lease rather than to a default, because the default
is where this keeps going wrong (acquire_execution_lease returning True for a
lease-less broker is what let runtime REAL arm with no lease at all). A broker holding
an established lease is by construction the production one and MUST have an authority
wired; one without a lease is a fixture and cannot reach a real account.
"""
import inspect

import pytest

from trading.kite_broker import KiteBroker
from trading.order_retry import BrokerOrderRejected


class _Lease:
    """An established, held account lease."""

    def __init__(self, held=True):
        self._held = held

    def held(self):
        return self._held


def _broker(*, leased=True, authority=None, api=object()):
    b = KiteBroker.__new__(KiteBroker)
    b.api = api
    b._execution_lease = _Lease() if leased else None
    if authority is not None:
        b._authority_provider = authority
    return b


@pytest.fixture(autouse=True)
def _lease_predicates(monkeypatch):
    """has_/acquire_execution_lease talk to a real lease file; stub that away."""
    monkeypatch.setattr(
        KiteBroker, "has_execution_lease",
        lambda self: getattr(self, "_execution_lease", None) is not None,
    )
    monkeypatch.setattr(KiteBroker, "acquire_execution_lease", lambda self: True)


# ── the mode dimension the adapter never had ─────────────────────────────────

def test_a_live_broker_in_real_mode_is_allowed():
    _broker(authority=lambda: True)._authorize_mutation("entry order")


def test_a_live_broker_outside_real_mode_is_refused():
    """The queued-job case: authority is asked at the SEND, not at the enqueue."""
    with pytest.raises(BrokerOrderRejected, match="not in REAL mode"):
        _broker(authority=lambda: False)._authorize_mutation("exit order")


def test_a_mode_change_between_enqueue_and_send_is_seen():
    """What a check taken at enqueue time cannot do."""
    mode = {"real": True}
    broker = _broker(authority=lambda: mode["real"])

    broker._authorize_mutation("exit order")      # enqueued while REAL: fine
    mode["real"] = False                          # hot-swap to HISTORICAL
    with pytest.raises(BrokerOrderRejected):
        broker._authorize_mutation("exit order")  # dispatched after: refused


def test_a_live_broker_with_no_authority_wired_is_refused():
    """Refusing beats assuming REAL. An unwired production broker is a wiring bug."""
    with pytest.raises(BrokerOrderRejected, match="no execution authority"):
        _broker()._authorize_mutation("entry order")


def test_an_unreadable_authority_is_refused():
    """Cannot tell is not permission."""
    def _boom():
        raise RuntimeError("manager gone")

    with pytest.raises(BrokerOrderRejected, match="could not be read"):
        _broker(authority=_boom)._authorize_mutation("entry order")


# ── fixtures stay inert ──────────────────────────────────────────────────────

def test_a_lease_less_broker_does_not_need_an_authority():
    """50-odd test files build brokers with no account id; none can reach an account."""
    _broker(leased=False)._authorize_mutation("entry order")


def test_a_lease_less_broker_is_not_refused_by_a_false_authority():
    """Without a live account there is nothing for the mode question to protect."""
    _broker(leased=False, authority=lambda: False)._authorize_mutation("entry order")


# ── session is part of the same gate ─────────────────────────────────────────

def test_no_session_is_refused():
    with pytest.raises(BrokerOrderRejected, match="no authenticated Kite session"):
        _broker(api=None, authority=lambda: True)._authorize_mutation("entry order")


# ── every mutation routes through it ─────────────────────────────────────────

MUTATIONS = [
    "place_order", "close_order", "place_protective_stop",
    "place_gtt_stop", "modify_protective_stop", "cancel_order",
]


@pytest.mark.parametrize("name", MUTATIONS)
def test_every_mutation_passes_the_gate(name):
    fn = getattr(KiteBroker, name, None)
    assert fn is not None, f"{name} no longer exists; update this list"
    src = inspect.getsource(fn)

    assert "_authorize_mutation(" in src, f"{name} mutates without passing the gate"


def test_the_lease_is_no_longer_asked_piecemeal():
    """Seven scattered lease calls became one gate; they must not creep back."""
    src = inspect.getsource(KiteBroker)
    direct = [
        line.strip() for line in src.splitlines()
        if "_require_account_execution_lease(" in line
        and "def " not in line
    ]

    # Exactly one caller remains: _authorize_mutation itself.
    assert len(direct) == 1, f"lease asked outside the gate: {direct}"


def test_gtt_cancellation_is_gated_too():
    """A server-side trigger left armed is as live as an order."""
    src = inspect.getsource(KiteBroker.cancel_gtt_stop)

    assert "_authorize_mutation(" in src


# ── the authority is actually wired in production ────────────────────────────

def test_startup_binds_the_authority_to_the_trade_manager():
    import main

    src = inspect.getsource(main)

    assert "set_execution_authority(" in src, "the live adapter is never given an authority"
    i = src.index("set_execution_authority(")
    assert "_live_orders_permitted" in src[i: i + 120], (
        "the authority must read the manager's CURRENT mode, not a captured value"
    )


def test_the_authority_is_a_callable_not_a_captured_value():
    """Binding the bound method, not its result, is what makes it live."""
    import main

    src = inspect.getsource(main)
    i = src.index("set_execution_authority(")
    call = src[i: src.index(")", i) + 1]

    assert "_live_orders_permitted()" not in call, (
        "a captured boolean freezes the mode at startup"
    )
