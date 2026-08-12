"""The consecutive-loss halt had no way out, so it ran to the closing bell.

_record_consecutive_loss_outcome clears an index's streak only on a NON-losing
outcome for that same index. Once the streak reached max_consecutive_losses the
index was refused every entry — so it could never produce the winning outcome
that clears the streak. The halt was self-perpetuating by construction.

2026-08-12: NIFTY and SENSEX each ran up 4 losses between 12:42 and 13:39, on
entries the market-wide bypass should never have admitted. At 14:15 the tape
reversed and every index turned buyer. rtracker for that window:

    14:30  SENSEX 15min LONG  REJECTED  Index HALTED: 4 uninterrupted same-index losses
    14:45  SENSEX 15min LONG  REJECTED  Index HALTED: 4 uninterrupted same-index losses
    14:30  NIFTY  5min  LONG  REJECTED  Index HALTED: 4 uninterrupted same-index losses

Both indices sat out the largest move of the day. restore_point_51 answers a
loss streak by skipping ONE candidate in that lane and then clearing the streak:

    if consume_loss_skip and pending.get(lane, 0) > 0:
        pending[lane] = 0
        self._loss_streaks_by_lane[lane] = 0
        return False, "Same-lane loss suspension: skipped ... after N consecutive losses"

A cooling-off, not a lockout. That semantic is restored here.
"""
from datetime import datetime

import pytest

from scanner import IST, Scanner


def _scanner(losses=4, max_losses=4):
    sc = Scanner.__new__(Scanner)
    sc._reset_daily_counters = lambda: None
    sc._trades_today = {"NIFTY": 1}
    sc._losses_today = {"NIFTY": losses}
    sc.max_consecutive_losses = max_losses
    sc._dynamic_trade_budget = lambda _i: (20, "test")
    sc._index_cooldown_applies = lambda *_a: (False, "")
    sc._last_exit_time = {}
    sc._post_loss_cooldown_block = lambda *_a, **_k: (False, "")
    sc.index_cooldown_minutes = 4.0
    sc.log_event = lambda *_a, **_k: None
    return sc


def _ask(sc):
    return sc._can_trade_instrument("NIFTY", datetime.now(IST).replace(tzinfo=None), direction="LONG")


def test_the_streak_suspends_one_candidate_then_lets_the_index_trade_again():
    sc = _scanner()

    blocked, reason = _ask(sc)
    assert blocked is False
    assert "loss suspension" in reason

    # The streak is cleared by that skip, so the NEXT signal is eligible. This is
    # the 14:15 reversal NIFTY and SENSEX were locked out of.
    allowed, _reason = _ask(sc)
    assert allowed is True
    assert sc._losses_today["NIFTY"] == 0


def test_below_the_threshold_nothing_is_suspended():
    sc = _scanner(losses=3, max_losses=4)

    allowed, _reason = _ask(sc)

    assert allowed is True
    assert sc._losses_today["NIFTY"] == 3


def test_a_fresh_streak_suspends_again():
    """Cooling-off is repeatable — it just is not permanent."""
    sc = _scanner()

    assert _ask(sc)[0] is False
    assert _ask(sc)[0] is True

    sc._losses_today["NIFTY"] = 4
    blocked, reason = _ask(sc)
    assert blocked is False
    assert "loss suspension" in reason
    assert _ask(sc)[0] is True


def test_the_suspension_is_never_a_permanent_halt():
    """No sequence of calls may leave the index refused forever."""
    sc = _scanner(losses=99, max_losses=4)

    _ask(sc)
    allowed, _reason = _ask(sc)

    assert allowed is True


@pytest.mark.parametrize("max_losses", [1, 2, 3, 5])
def test_it_honours_the_configured_threshold(max_losses):
    sc = _scanner(losses=max_losses, max_losses=max_losses)

    assert _ask(sc)[0] is False
    assert _ask(sc)[0] is True
