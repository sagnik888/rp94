"""Never transact against the REAL ACCOUNT broker outside REAL mode.

_live_orders_permitted guards the two dispatch paths that ask it — the entry in
open_trade and the full exit in close_trade. Several broker-MUTATING paths never
asked anything but wiring (`if not self.broker`):

    emergency_flatten_trade        cancels the SL-M, then sends a MARKET exit
    reconcile_broker_positions     cancels a resting entry and MARKET-replaces it,
                                   and cancels a protective stop
    _handle_orphans_at_session_end sends close_order

In ordinary HISTORICAL running that costs nothing: the factory attaches a
SignalSimBroker whose order methods are stubs. The hole is the REAL -> HISTORICAL
hot swap. scanner.configure sets

    scanner.py:4194   self.trades.mode = mode          # -> "HISTORICAL"
    scanner.py:4305   self.trades.broker = mock_broker # ~111 lines later

and __init__ starts the PositionSync daemon unconditionally, which calls reconcile
every ~5s. In that window mode already says HISTORICAL while self.broker is still
the live KiteBroker, and reconcile is not operator-triggered — it fires on a timer.

So the invariant is narrower and truer than "not REAL mode": never send to the REAL
ACCOUNT broker outside REAL mode. Gating on mode alone would refuse the fake brokers
that 53 test files inject in HISTORICAL deliberately. real_broker is wired only in
production startup (main.py:898), so the predicate is inert under test.
"""
import inspect

import pytest

from trading.trade_manager import TradeManager


def _mgr(mode, broker=None, real_broker=None):
    mgr = TradeManager.__new__(TradeManager)
    mgr.mode = mode
    mgr.broker = broker
    if real_broker is not None:
        mgr.real_broker = real_broker
    return mgr


class _Broker:
    pass


# ── the race this exists for ─────────────────────────────────────────────────

def test_the_hot_swap_window_is_blocked():
    """Mode already flipped to HISTORICAL, broker not yet swapped back."""
    live = _Broker()

    assert _mgr("HISTORICAL", broker=live, real_broker=live)._live_account_send_blocked() is True


def test_real_mode_is_never_blocked():
    live = _Broker()

    assert _mgr("REAL", broker=live, real_broker=live)._live_account_send_blocked() is False


def test_a_sim_broker_in_historical_is_not_blocked():
    """After the swap completes the attached broker is the mock — stubs, harmless."""
    live, sim = _Broker(), _Broker()

    assert _mgr("HISTORICAL", broker=sim, real_broker=live)._live_account_send_blocked() is False


# ── inert under test, which is what keeps the suite honest ───────────────────

def test_a_manager_with_no_real_broker_is_never_blocked():
    """53 test files inject fake brokers in HISTORICAL on purpose."""
    assert _mgr("HISTORICAL", broker=_Broker())._live_account_send_blocked() is False


def test_a_null_real_broker_is_never_blocked():
    assert _mgr("HISTORICAL", broker=_Broker(), real_broker=None)._live_account_send_blocked() is False


def test_no_broker_at_all_is_not_blocked():
    assert _mgr("HISTORICAL", broker=None)._live_account_send_blocked() is False


@pytest.mark.parametrize("mode", ["HISTORICAL", "historical", "", None, "PAPER"])
def test_every_non_real_mode_blocks_the_live_account(mode):
    live = _Broker()

    assert _mgr(mode, broker=live, real_broker=live)._live_account_send_blocked() is True


def test_a_manager_with_no_mode_attribute_blocks():
    """Absent is not permission."""
    mgr = TradeManager.__new__(TradeManager)
    live = _Broker()
    mgr.broker = live
    mgr.real_broker = live

    assert mgr._live_account_send_blocked() is True


# ── the mutating paths are wired to it ───────────────────────────────────────

def test_emergency_flatten_refuses_before_touching_the_broker():
    src = inspect.getsource(TradeManager.emergency_flatten_trade)
    head = src[: src.index("if not self.broker")]

    assert "_live_account_send_blocked()" in head, (
        "the refusal must come before the wiring check and before any send"
    )


def test_reconcile_gates_the_cancel_replace_branch():
    src = inspect.getsource(TradeManager.reconcile_broker_positions)
    i = src.index("broker_cancel_replace_enabled")
    # The guard sits at the end of the same `if`, past its explanatory comment.
    window = src[i: src.index("):", i) + 2]

    assert "_live_account_send_blocked()" in window, (
        "the entry cancel/MARKET-replace branch is ungated"
    )


def test_reconcile_gates_the_protective_stop_cancel():
    src = inspect.getsource(TradeManager.reconcile_broker_positions)
    i = src.index('cancel_order(stop_id, "STOPLOSS")')

    assert "_live_account_send_blocked()" in src[max(0, i - 200): i]


def test_the_orphan_flatten_is_gated():
    src = inspect.getsource(TradeManager._handle_orphans_at_session_end)
    # Anchor on the setting READ, not the docstring's mention of it by name.
    i = src.index('getattr(settings, "force_exit_flattens_orphans"')

    assert "_live_account_send_blocked()" in src[i: i + 200]


def test_the_reconcile_read_half_is_not_gated():
    """Orphan detection and quantity confirmation must keep running in every mode.

    Gating the whole pass would stop the system noticing a broker position while it
    is not trading — the opposite of safe.
    """
    src = inspect.getsource(TradeManager.reconcile_broker_positions)
    head = src[: src.index("broker_cancel_replace_enabled")]

    assert "_live_account_send_blocked()" not in head, (
        "the read half of reconcile must not be gated"
    )


# ── queued work re-asks authority at dispatch, not just at enqueue ───────────

def test_the_predicate_can_be_asked_about_a_captured_broker():
    """Queued tasks capture the broker at enqueue and send later.

    By dispatch time self.broker may already be the mock while the closure still
    holds the live account, so asking about self.broker would wave it through.
    """
    live, sim = _Broker(), _Broker()
    mgr = _mgr("HISTORICAL", broker=sim, real_broker=live)

    assert mgr._live_account_send_blocked() is False        # self.broker is the mock
    assert mgr._live_account_send_blocked(live) is True     # the captured one is not


def test_asking_about_a_mock_broker_is_never_blocked():
    live, sim = _Broker(), _Broker()
    mgr = _mgr("HISTORICAL", broker=live, real_broker=live)

    assert mgr._live_account_send_blocked(sim) is False


def test_real_mode_is_unblocked_for_a_captured_broker_too():
    live = _Broker()

    assert _mgr("REAL", broker=live, real_broker=live)._live_account_send_blocked(live) is False


def test_the_queued_exit_reasks_authority_at_dispatch():
    src = inspect.getsource(TradeManager._request_broker_exit)

    assert "_live_account_send_blocked(broker)" in src, (
        "the queued exit still relies on the enqueue-time gate alone"
    )


def test_the_dispatch_check_runs_before_any_send():
    src = inspect.getsource(TradeManager._request_broker_exit)
    i = src.index("_live_account_send_blocked(broker)")

    for call in ("broker.close_order", "broker.cancel_order"):
        if call in src:
            assert i < src.index(call), f"{call} is reachable before the dispatch check"
