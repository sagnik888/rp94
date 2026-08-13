"""A confirmed candle must not be repaint-aborted, however late the tracker looks.

_repaint_close_check_in_window is the policy, and its docstring states it outright:

    Repaint is an INTRABAR concept: it is only meaningful while the source candle
    is forming, plus a brief grace at the close moment so a normally running
    scanner can make the final check. Once that grace passes, the candle has long
    confirmed and a still-open trade is a NORMAL tracked position — it must never
    be repaint-aborted afterwards.

It was consulted at scanner.py:16338 and scanner.py:21921 — both session-mirror
paths — and NOT in _track_repaint_until_candle_close, the tracker that closes the
REAL broker position. Same shape as the earlier defect in this same function, where
every configured repaint MITIGATION was reachable only from the paper mirror while
the live position was market-exited immediately.

The consequence: a tracker whose first absence reading lands long after the bar
confirmed would still market-exit on it. A 15m signal was aborted eight minutes past
confirmation, with a 90s grace configured.

NOT a reordering. The loop runs the absence check and THEN `if now >= candle_close_time:
break`, and that ordering is correct — the tick at or just after the close is the
authoritative final verdict, and breaking first would delete it. What was missing was
any bound on how far past the close an abort may still fire.
"""
import inspect

from datetime import datetime, timedelta

import pytest

from scanner import Scanner


SIG = datetime(2026, 8, 14, 11, 0)


class _S:
    """Only what _repaint_close_check_in_window touches."""


def _in_window(timeframe, at):
    return Scanner._repaint_close_check_in_window(_S(), SIG, timeframe, at)


# ── the policy this now honours ──────────────────────────────────────────────

def test_a_forming_candle_is_in_window():
    assert _in_window("15min", SIG + timedelta(minutes=8)) is True


def test_the_close_moment_is_in_window():
    assert _in_window("15min", SIG + timedelta(minutes=15)) is True


def test_the_grace_after_the_close_is_in_window():
    """The grace exists so a running scanner can make its final check."""
    assert _in_window("15min", SIG + timedelta(minutes=15, seconds=60)) is True


def test_eight_minutes_past_confirmation_is_out_of_window():
    """The reported case: a 15m signal aborted long after the bar confirmed."""
    assert _in_window("15min", SIG + timedelta(minutes=23)) is False


def test_a_five_minute_signal_past_grace_is_out_of_window():
    assert _in_window("5min", SIG + timedelta(minutes=10)) is False


def test_a_one_minute_signal_uses_its_own_bar_length():
    assert _in_window("1min", SIG + timedelta(seconds=30)) is True
    assert _in_window("1min", SIG + timedelta(minutes=5)) is False


# ── the broker tracker consults it ───────────────────────────────────────────

def test_the_broker_tracker_checks_the_window():
    src = inspect.getsource(Scanner._track_repaint_until_candle_close)

    assert "_repaint_close_check_in_window(" in src, (
        "the tracker that closes the REAL position still aborts without a validity bound"
    )


def test_being_out_of_window_holds_rather_than_aborts():
    src = inspect.getsource(Scanner._track_repaint_until_candle_close)

    assert "if not within_repaint_window:" in src
    i = src.index("if not within_repaint_window:")
    assert "hold_reason" in src[i: i + 200], (
        "out-of-window must route to the existing HOLD path, which falls through to "
        "the candle-close exit"
    )


def test_the_window_bound_is_applied_after_the_mitigation_block():
    """The mitigation `except` resets hold_reason; the bound must outlive that."""
    src = inspect.getsource(Scanner._track_repaint_until_candle_close)

    assert src.index("Repaint mitigation check unavailable") < src.index(
        "if not within_repaint_window:"
    )


def test_an_unevaluable_window_does_not_abort():
    """This is a validity bound, not a mitigation, so it fails the other way.

    The mitigations fail SAFE toward aborting. Not knowing whether the bar has
    confirmed makes exiting the dangerous move: a missed abort leaves the trade to
    its stop and target.
    """
    src = inspect.getsource(Scanner._track_repaint_until_candle_close)
    i = src.index("within_repaint_window = False")
    guarded = src[src.index("within_repaint_window = Scanner."): i]

    assert "except Exception" in guarded, (
        "the window check must be wrapped so a failure defaults to NOT aborting"
    )


def test_the_candle_close_exit_still_runs_last():
    """The ordering is deliberate and must not be 'fixed' by reversing it."""
    src = inspect.getsource(Scanner._track_repaint_until_candle_close)

    assert src.index("if absence_definite:") < src.index("if now >= candle_close_time:"), (
        "the tick at/after the close is the authoritative final repaint check; "
        "breaking before it would delete that verdict"
    )
