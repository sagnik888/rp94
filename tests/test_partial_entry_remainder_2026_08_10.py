"""The unfilled tail of a partially filled ENTRY rested all session.

The 12-second cancel/replace timer only runs while `position is None` AND
execution_status == 'ENTRY_SUBMITTED'. The instant a partial fill lands, a position row
exists and the status becomes 'PARTIAL' — so both preconditions are permanently false
and nothing ages the remainder again. The PARTIAL branch re-reads the fill and re-arms
protection on every pass, but never touches the order still working at the exchange.

    10:05  3-lot entry fills 1 lot. SL-M armed for 1. Status PARTIAL.
    ...    2 lots rest at the exchange for four and a half hours.
    14:40  a spike sweeps them. Position is 3 lots; the stop covers 1.

Until the next reconcile tick two lots are naked, and P&L, risk headroom and the
concentration guards were all sized on one lot for the whole interval. The only backstop
was the 15:25 force exit.

The cancel is PROVEN, never acknowledged — the same rule the stale-entry replace already
follows, and for the same reason: this order is resting because price is near its limit,
so the fill routinely wins the race against the cancel. cancel_order_confirmed answers
True only for a broker-confirmed terminal non-fill, False when it actually filled, and
None when it cannot tell. Only True permits the claim that the remainder is gone.

Aged from entry_submitted_at, which is persisted — so a restart does not reset the clock
and hand the remainder another full timeout.
"""
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytz

from trading.trade_manager import TradeManager

IST = pytz.timezone("Asia/Kolkata")


def _tm(verdict, raises=False):
    tm = TradeManager.__new__(TradeManager)

    def _confirm(order_id, variety="NORMAL"):
        if raises:
            raise RuntimeError("broker unreachable")
        return verdict

    tm.broker = SimpleNamespace(cancel_order_confirmed=_confirm)
    tm._log_ui = lambda *a, **k: None
    return tm


def _trade(age_seconds=300, order_id="O1"):
    return SimpleNamespace(
        id="T1", trading_symbol="NIFTY25000CE", broker_order_id=order_id,
        entry_submitted_at=datetime.now(IST) - timedelta(seconds=age_seconds),
        pending_execution=True, execution_error="", entry_remainder_withdrawn=False,
    )


def _run(tm, trade, filled=65, expected=195):
    TradeManager._withdraw_stale_entry_remainder(tm, trade, filled, expected)
    return trade


# ── only a PROVEN cancel may claim the remainder is gone ─────────────────────

def test_a_proven_cancel_withdraws_the_remainder():
    t = _run(_tm(True), _trade())
    assert t.entry_remainder_withdrawn is True
    assert t.pending_execution is False
    assert "remainder withdrawn" in t.execution_error


def test_a_remainder_that_filled_makes_no_claim():
    """False means it filled. Marking it withdrawn would leave real quantity unprotected
    in the local book."""
    t = _run(_tm(False), _trade())
    assert t.entry_remainder_withdrawn is False
    assert t.pending_execution is True


def test_an_unknown_verdict_makes_no_claim():
    t = _run(_tm(None), _trade())
    assert t.entry_remainder_withdrawn is False
    assert t.pending_execution is True


def test_a_raising_broker_makes_no_claim():
    t = _run(_tm(True, raises=True), _trade())
    assert t.entry_remainder_withdrawn is False
    assert t.pending_execution is True


# ── the age gate ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("age", [0, 30, 119])
def test_a_fresh_remainder_is_left_alone(age):
    """A legitimate fill needs time to complete; cancelling at once would fight it."""
    t = _run(_tm(True), _trade(age_seconds=age))
    assert t.entry_remainder_withdrawn is False


@pytest.mark.parametrize("age", [121, 600, 3600])
def test_a_stale_remainder_is_withdrawn(age):
    assert _run(_tm(True), _trade(age_seconds=age)).entry_remainder_withdrawn is True


def test_the_clock_is_the_persisted_submission_time():
    """entry_submitted_at is a persisted column, so a restart does not hand the
    remainder a fresh timeout."""
    from data.sqlite_db import TRADE_PERSISTED_COLUMNS

    assert "entry_submitted_at" in TRADE_PERSISTED_COLUMNS
    src = inspect.getsource(TradeManager._withdraw_stale_entry_remainder)
    assert "entry_submitted_at" in src


def test_a_string_timestamp_is_parsed():
    t = _trade()
    t.entry_submitted_at = (datetime.now(IST) - timedelta(seconds=300)).isoformat()
    assert _run(_tm(True), t).entry_remainder_withdrawn is True


def test_a_missing_timestamp_is_safe():
    t = _trade()
    t.entry_submitted_at = None
    assert _run(_tm(True), t).entry_remainder_withdrawn is False


def test_a_junk_timestamp_is_safe():
    t = _trade()
    t.entry_submitted_at = "not a time"
    assert _run(_tm(True), t).entry_remainder_withdrawn is False


# ── it does not fire when it should not ──────────────────────────────────────

def test_a_fully_filled_entry_is_untouched():
    t = _run(_tm(True), _trade(), filled=195, expected=195)
    assert t.entry_remainder_withdrawn is False


def test_no_order_id_means_nothing_to_cancel():
    assert _run(_tm(True), _trade(order_id="")).entry_remainder_withdrawn is False


def test_it_only_acts_once():
    tm, t = _tm(True), _trade()
    _run(tm, t)
    t.pending_execution = True          # something else re-set it
    _run(tm, t)
    assert t.pending_execution is True, "acted twice on an already-withdrawn remainder"


def test_a_broker_without_proven_cancel_makes_no_claim():
    tm = TradeManager.__new__(TradeManager)
    tm.broker = SimpleNamespace()
    tm._log_ui = lambda *a, **k: None
    assert _run(tm, _trade()).entry_remainder_withdrawn is False


# ── wiring and settings ──────────────────────────────────────────────────────

def test_the_partial_branch_calls_it():
    src = inspect.getsource(TradeManager.reconcile_broker_positions)
    assert "_withdraw_stale_entry_remainder" in src, (
        "the remainder is still left resting for the rest of the session"
    )
    marker = "_withdraw_stale_entry_remainder"
    window = src[max(0, src.index(marker) - 300): src.index(marker)]
    assert "filled_qty < expected_qty" in window, "called even on a complete fill"


def test_the_settings_resolve():
    from config.settings import get_settings

    s = get_settings()
    assert bool(getattr(s, "partial_entry_remainder_cancel_enabled")) is True
    assert float(getattr(s, "partial_entry_remainder_timeout_seconds")) >= 5.0


def test_it_can_be_disabled():
    from config.settings import get_settings

    s = get_settings()
    saved = s.partial_entry_remainder_cancel_enabled
    s.partial_entry_remainder_cancel_enabled = False
    try:
        assert _run(_tm(True), _trade()).entry_remainder_withdrawn is False
    finally:
        s.partial_entry_remainder_cancel_enabled = saved


def test_the_stale_entry_replace_still_proves_its_cancel():
    """The premise this mirrors. If that ever drops to a bare cancel_order, this
    function's contract is the odd one out."""
    src = inspect.getsource(TradeManager.reconcile_broker_positions)
    assert "cancel_order_confirmed" in src
