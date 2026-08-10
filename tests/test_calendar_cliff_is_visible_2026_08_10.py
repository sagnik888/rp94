"""The exchange calendar runs out on 2026-12-31 and stops all trading silently.

`NSE_HOLIDAYS_BY_YEAR` carries exactly one year: 2026. `is_market_holiday` treats any
date outside the covered years as a holiday and says why in a comment — "deliberate
safety - do not trade a year whose holidays we cannot verify". That is the right call
and this does not change it.

What it changes is the SILENCE. From 2027-01-01 every date reads as a holiday, so:

  * is_market_open is False all day, every day
  * the Institutional Execution Gate rejects 100% of entries
  * the scanner still runs, still scans, still serves a dashboard

and the only evidence is one CRITICAL log line, emitted once per uncovered year and
gone from the console long before anyone notices the flat P&L. Readiness did surface it
— as the bare string "market holiday", which on an ordinary Monday reads like the
exchange is shut rather than like the software has run out of data.

Two changes:

  1. an uncovered year now names itself in the blocker, with the fix in the message
  2. readiness warns while coverage is still valid but expiring, so a five-month fuse
     becomes a routine data update instead of a morning outage

A genuine holiday inside a covered year is untouched and still reads "market holiday".
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from engine.safe_mode import calendar_coverage_status

COVERED = {"2026-01-15", "2026-01-26", "2026-12-25"}


def _provider(holidays=COVERED):
    return SimpleNamespace(nse_holidays=set(holidays))


# ── the coverage read ────────────────────────────────────────────────────────

def test_a_covered_year_reports_time_remaining():
    c = calendar_coverage_status(_provider(), datetime(2026, 8, 10))
    assert c["current_covered"] is True
    assert c["days_remaining"] == 143
    assert c["expires_on"] == "2026-12-31"


def test_an_uncovered_year_is_reported_as_such():
    c = calendar_coverage_status(_provider(), datetime(2027, 1, 4))
    assert c["current_covered"] is False
    assert c["days_remaining"] < 0
    assert c["years"] == [2026]


def test_the_expiry_is_the_end_of_the_last_covered_year():
    c = calendar_coverage_status(
        _provider({"2026-01-15", "2027-01-26", "2028-12-25"}), datetime(2026, 8, 10)
    )
    assert c["years"] == [2026, 2027, 2028]
    assert c["expires_on"] == "2028-12-31"


def test_multiple_years_are_all_counted():
    c = calendar_coverage_status(_provider({"2026-01-15", "2027-03-02"}), datetime(2027, 6, 1))
    assert c["current_covered"] is True
    assert c["years"] == [2026, 2027]


# ── an absent calendar is NOT the cliff ──────────────────────────────────────

def test_no_calendar_at_all_is_not_reported_as_expiry():
    """is_market_holiday only fails closed when coverage_years is non-empty
    (`if coverage_years and dt.year not in coverage_years`). An empty calendar is a
    different condition and must not be dressed up as an expired one."""
    c = calendar_coverage_status(_provider(set()), datetime(2027, 1, 4))
    assert c["years"] == []
    assert c["current_covered"] is True
    assert c["days_remaining"] is None


def test_a_provider_without_the_attribute_is_safe():
    c = calendar_coverage_status(SimpleNamespace(), datetime(2027, 1, 4))
    assert c["current_covered"] is True


def test_junk_entries_are_ignored_rather_than_crashing():
    c = calendar_coverage_status(
        _provider({"2026-01-15", "not-a-date", "", None}), datetime(2026, 8, 10)
    )
    assert c["years"] == [2026]


def test_a_raising_provider_does_not_propagate():
    class Boom:
        @property
        def nse_holidays(self):
            raise RuntimeError("calendar backend down")

    assert calendar_coverage_status(Boom(), datetime(2026, 8, 10))["current_covered"] is True


# ── readiness says which of the two it is ────────────────────────────────────

def _readiness(when, holidays=COVERED):
    """Run the real readiness check, not a source-text match.

    An earlier version of these asserted on inspect.getsource, which happily passes
    against dead code — replacing the branch condition with `if False:` broke nothing.
    These call the function.
    """
    import pytz

    from config.settings import get_settings
    from engine.safe_mode import validate_real_mode_readiness

    years = {int(str(x)[:4]) for x in holidays}
    provider = SimpleNamespace(
        nse_holidays=set(holidays),
        is_market_holiday=lambda d: d.year not in years or d.strftime("%Y-%m-%d") in holidays,
        get_source_health=lambda: {},
        kite=None, fyers=None, is_connected=False, _fyers_auth_required=False,
    )
    return validate_real_mode_readiness(
        get_settings(), provider, pytz.timezone("Asia/Kolkata").localize(when)
    )


def test_an_expired_calendar_names_itself_in_the_blocker():
    r = _readiness(datetime(2027, 1, 4, 10, 0))
    hits = [b for b in r["blockers"] if "calendar" in b]
    assert hits, f"expired calendar still reports as a plain holiday: {r['blockers']}"
    assert "2027" in hits[0] and "NO trade" in hits[0]
    assert "market_holidays.json" in hits[0], "the blocker does not say how to fix it"


def test_a_real_holiday_inside_a_covered_year_still_reports_as_one():
    r = _readiness(datetime(2026, 12, 25, 10, 0))
    assert "market holiday" in r["blockers"]
    assert not [b for b in r["blockers"] if "calendar covers" in b]


def test_readiness_warns_before_the_calendar_expires():
    r = _readiness(datetime(2026, 11, 25, 10, 0))
    hits = [w for w in r["warnings"] if "calendar runs out" in w]
    assert hits, f"no advance warning: {r['warnings']}"
    assert "36 days" in hits[0]
    assert not [b for b in r["blockers"] if "calendar" in b], "warned AND blocked"


def test_no_warning_while_coverage_is_comfortable():
    r = _readiness(datetime(2026, 8, 10, 10, 0))
    assert not [w for w in r["warnings"] if "calendar runs out" in w]


def test_an_expired_calendar_blocks_rather_than_merely_warning():
    """Once past the cliff it must be a BLOCKER; a warning would let REAL mode arm."""
    r = _readiness(datetime(2027, 1, 4, 10, 0))
    assert r["ok"] is False
    assert not [w for w in r["warnings"] if "calendar runs out" in w], "double-reported"


def test_the_warn_window_setting_resolves():
    from config.settings import get_settings

    assert int(getattr(get_settings(), "calendar_coverage_warn_days")) > 0


# ── the premise: the bundled calendar really does end in 2026 ────────────────

def test_the_bundled_calendar_still_covers_only_one_year():
    """If this starts failing, someone extended the calendar — good. Update the docstring
    above rather than deleting the guard."""
    from data.market_calendar import NSE_HOLIDAYS_BY_YEAR

    assert set(NSE_HOLIDAYS_BY_YEAR) == {2026}, (
        f"coverage changed to {sorted(NSE_HOLIDAYS_BY_YEAR)}; re-check the cliff date"
    )


def test_the_fail_closed_behaviour_is_deliberately_unchanged():
    """This is a VISIBILITY fix. Trading a year whose holidays cannot be verified would
    be the worse failure, so the fail-closed path must stay exactly as it is."""
    import inspect

    import data.market_data as md

    src = inspect.getsource(md.MarketDataProvider.is_market_holiday)
    assert "coverage_years and dt.year not in coverage_years" in src
    assert "return True" in src
