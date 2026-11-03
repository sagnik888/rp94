"""A daily-expiry token alert must be raised again at an hour someone can act on.

The watchdog de-duplicates each alert to one per incident, which is correct for a
mid-session failure. Both broker tokens, though, expire on a clock: Kite invalidates its
access token around 06:00 and the Fyers token expires at roughly the same hour. On
2026-09-16 the only Kite alert landed at 04:30 and the only Fyers alert at 06:05, and
neither was ever repeated -- both logins were completed at 08:41 by the operator
noticing, 34 minutes before the open.

These tests pin the pre-open re-raise and, just as importantly, pin that it stays
narrow: outside the window, on a weekend, and for any alert that is not one of the two
daily-expiry broker alerts, the single-shot behaviour is unchanged.
"""
from datetime import datetime, time as dt_time

import pytest

from main import (
    _PREOPEN_AUTH_ALERT_KEYS,
    _preopen_auth_realert_due,
    _release_preopen_auth_latches,
)


PREOPEN = dt_time(8, 30)
OPEN = dt_time(9, 15)


def _due(when, last=0.0, mono=10_000.0, every=600.0):
    return _preopen_auth_realert_due(
        when,
        preopen_start=PREOPEN,
        session_start=OPEN,
        last_realert_monotonic=last,
        now_monotonic=mono,
        every_seconds=every,
    )


# --- the window ----------------------------------------------------------------
def test_first_pass_inside_the_window_re_raises():
    assert _due(datetime(2026, 9, 16, 8, 30)) is True
    assert _due(datetime(2026, 9, 16, 9, 0)) is True


def test_before_the_window_is_silent():
    """04:30 and 06:05 are exactly when the original alerts were wasted."""
    assert _due(datetime(2026, 9, 16, 4, 30)) is False
    assert _due(datetime(2026, 9, 16, 6, 5)) is False
    assert _due(datetime(2026, 9, 16, 8, 29)) is False


def test_the_window_closes_at_the_open_and_never_reopens_intraday():
    assert _due(datetime(2026, 9, 16, 9, 15)) is False
    assert _due(datetime(2026, 9, 16, 11, 0)) is False
    assert _due(datetime(2026, 9, 16, 15, 45)) is False


def test_weekend_is_silent():
    assert _due(datetime(2026, 9, 19, 8, 45)) is False   # Saturday
    assert _due(datetime(2026, 9, 20, 8, 45)) is False   # Sunday


# --- the cadence ---------------------------------------------------------------
def test_second_pass_waits_for_the_configured_interval():
    when = datetime(2026, 9, 16, 8, 45)
    assert _due(when, last=10_000.0, mono=10_300.0, every=600.0) is False
    assert _due(when, last=10_000.0, mono=10_600.0, every=600.0) is True


def test_interval_has_a_one_minute_floor_so_a_zero_cannot_spam():
    when = datetime(2026, 9, 16, 8, 45)
    assert _due(when, last=10_000.0, mono=10_030.0, every=0.0) is False
    assert _due(when, last=10_000.0, mono=10_061.0, every=0.0) is True


def test_a_longer_interval_is_honoured():
    when = datetime(2026, 9, 16, 8, 45)
    assert _due(when, last=10_000.0, mono=11_000.0, every=1800.0) is False
    assert _due(when, last=10_000.0, mono=11_900.0, every=1800.0) is True


# --- what the release actually touches -----------------------------------------
def test_release_clears_exactly_the_two_broker_latches():
    state = {"kite_token": True, "fyers": True}
    assert sorted(_release_preopen_auth_latches(state)) == ["fyers", "kite_token"]
    assert state == {}


def test_release_leaves_every_other_alert_single_shot():
    """A scanner-dead or resource alert must not be re-raised by the pre-open pass."""
    state = {
        "kite_token": True,
        "scanner_dead": True,
        "auto_never_armed": True,
        "resource_pressure": True,
        "kite_stream": True,
        "preopen_realert_at": 123.0,
    }
    released = _release_preopen_auth_latches(state)

    assert released == ["kite_token"]
    assert state == {
        "scanner_dead": True,
        "auto_never_armed": True,
        "resource_pressure": True,
        "kite_stream": True,
        "preopen_realert_at": 123.0,
    }


def test_release_on_a_healthy_morning_is_a_no_op():
    state = {}
    assert _release_preopen_auth_latches(state) == []
    assert state == {}


def test_release_reports_nothing_for_a_latch_that_was_already_cleared():
    """Both brokers authenticated: the pop must not read as a re-raise."""
    state = {"kite_token": False, "fyers": False}
    assert _release_preopen_auth_latches(state) == []


def test_released_keys_are_the_documented_pair():
    assert set(_PREOPEN_AUTH_ALERT_KEYS) == {"kite_token", "fyers"}


# --- the incident this was built for -------------------------------------------
def test_the_2026_09_16_timeline_would_have_been_told_before_the_open():
    """Alert latched at 04:30; operator acted unprompted at 08:41."""
    state = {"kite_token": True, "fyers": True}
    last = 0.0
    mono = 0.0
    raised_at = []
    # one watchdog poll per minute from 04:00 to the open
    for minute in range(4 * 60, 9 * 60 + 15):
        now = datetime(2026, 9, 16, minute // 60, minute % 60)
        mono += 60.0
        if _preopen_auth_realert_due(
            now, preopen_start=PREOPEN, session_start=OPEN,
            last_realert_monotonic=last, now_monotonic=mono, every_seconds=600.0,
        ):
            last = mono
            if _release_preopen_auth_latches(state):
                raised_at.append(now.strftime("%H:%M"))
            state.update({"kite_token": True, "fyers": True})  # still not logged in

    assert raised_at, "no pre-open alert was raised at all"
    assert raised_at[0] == "08:30"
    assert all("08:30" <= t < "09:15" for t in raised_at)
    # bounded, not a stream: 45 minutes at a 10-minute cadence
    assert len(raised_at) <= 6
