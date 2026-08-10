"""The morning-refresh attempt cap marked the day warmed, killing the 09:15 catch-up.

Pre-open the NFO/BFO option chains are empty, so with morning_refresh_require_all_chains
True every attempt between 09:00 and 09:15 returns INCOMPLETE. Three of those exhaust
morning_refresh_max_attempts, and the cap branch does:

    self._morning_refresh_last_date = session_day     # "stop blocking scans forever"
    summary["complete"] = False
    summary["accepted_incomplete"] = True

But `_morning_refresh_is_complete_for_day` is exactly `_morning_refresh_last_date ==
session_day`, and `_ensure_market_session_refresh` tests it on its FIRST line and returns.
So the 09:15 catch-up — whose own log line reads "re-warming missing morning keys after
incomplete premarket pass" — sits below an early return it can never reach. Nothing else
clears the date within the day.

THE OBVIOUS FIX IS WRONG. Making _morning_refresh_is_complete_for_day return False for an
accepted-incomplete day lets the flow fall through, re-schedule, hit the cap again, and
return True from _ensure_market_session_refresh — which blocks signal work on every scan
for the rest of the session. That is precisely the failure the cap was added to prevent,
so "completeness" must keep gating the retry loop.

What is granted instead is ONE fresh pass, at the live-open boundary only, guarded by the
existing _morning_refresh_catchup_done_date latch (set BEFORE scheduling, so it cannot
loop). After that pass the cap re-arms and the day stays non-blocking.
"""
from datetime import datetime, time as dtime
from types import SimpleNamespace

import pytest
import pytz

from scanner import Scanner

IST = pytz.timezone("Asia/Kolkata")


def _scanner(*, accepted_incomplete, catchup_done=False, attempts=3):
    s = Scanner.__new__(Scanner)
    day = "2026-08-11"
    s._morning_refresh_last_date = day
    s._morning_refresh_attempts = {day: attempts}
    s._morning_refresh_inflight = False
    s._morning_refresh_catchup_done_date = day if catchup_done else None
    s._morning_refresh_summary = {
        "session_day": day,
        "complete": False,
        "accepted_incomplete": bool(accepted_incomplete),
        "missing_keys": ["chains", "chain:NIFTY"],
    }
    if not accepted_incomplete:
        s._morning_refresh_summary["complete"] = True

    s.scheduled = []
    s._is_trading_day = lambda now: True
    s._is_real_mode_premarket_arm_window = lambda now: False
    s.log_event = lambda *a, **k: None
    s.data = SimpleNamespace(is_market_open=lambda *a, **k: True)
    s._schedule_morning_market_refresh = (
        lambda indices, now, reason="", force_full=False: s.scheduled.append(reason)
    )
    return s


def _at(hh, mm):
    return IST.localize(datetime(2026, 8, 11, hh, mm, 0))


def _run(s, when):
    return Scanner._ensure_market_session_refresh(s, {"NIFTY": {}}, when)


# ── the bug ──────────────────────────────────────────────────────────────────

def test_the_live_open_catchup_fires_after_an_accepted_incomplete_premarket():
    s = _scanner(accepted_incomplete=True)
    _run(s, _at(9, 20))
    assert "live_open_catchup" in s.scheduled, (
        "the cap marked the day warmed, so the 09:15 catch-up never ran"
    )


def test_the_catchup_latch_is_set_so_it_cannot_loop():
    s = _scanner(accepted_incomplete=True)
    _run(s, _at(9, 20))
    assert s._morning_refresh_catchup_done_date == "2026-08-11"


def test_the_catchup_is_granted_exactly_once():
    """Second and later scans must not keep re-warming."""
    s = _scanner(accepted_incomplete=True)
    _run(s, _at(9, 20))
    s.scheduled.clear()
    for mins in (21, 25, 40):
        _run(s, _at(9, mins))
    assert s.scheduled == [], f"catch-up repeated: {s.scheduled}"


# ── the regression the cap exists to prevent ─────────────────────────────────

def test_scans_are_never_blocked_forever_after_the_catchup():
    """_ensure_market_session_refresh returning True holds signal work. Once the
    catch-up has had its pass, the day must go back to non-blocking."""
    s = _scanner(accepted_incomplete=True)
    _run(s, _at(9, 20))
    for mins in (21, 30, 45):
        assert _run(s, _at(9, mins)) is False, "signal work is being held for the session"


def test_a_day_already_catchup_done_returns_immediately():
    s = _scanner(accepted_incomplete=True, catchup_done=True)
    assert _run(s, _at(9, 20)) is False
    assert s.scheduled == []


def test_a_genuinely_complete_day_is_left_alone():
    s = _scanner(accepted_incomplete=False)
    assert _run(s, _at(9, 20)) is False
    assert s.scheduled == []


def test_before_the_open_the_cap_still_holds():
    """Pre-open the cap must keep its grip; the extra pass is a live-open concession."""
    s = _scanner(accepted_incomplete=True)
    assert _run(s, _at(9, 5)) is False
    assert s.scheduled == []


def test_the_attempt_budget_is_reset_only_for_that_one_pass():
    s = _scanner(accepted_incomplete=True)
    _run(s, _at(9, 20))
    assert s._morning_refresh_attempts.get("2026-08-11", 0) <= 1, (
        "the retry loop got an unbounded new budget"
    )


# ── the premise ──────────────────────────────────────────────────────────────

def test_completeness_still_gates_the_retry_loop():
    """If this ever stops being true the cap no longer bounds anything."""
    s = _scanner(accepted_incomplete=True, catchup_done=True)
    assert Scanner._morning_refresh_is_complete_for_day(s, "2026-08-11") is True


def test_the_cap_still_records_accepted_incomplete():
    import inspect

    src = inspect.getsource(Scanner._schedule_morning_market_refresh)
    assert 'accepted_incomplete' in src
    assert "_morning_refresh_last_date = session_day" in src
