"""Intel age was inflated by exactly the IST offset, so fresh intel read as stale.

2026-08-12 13:27:35 and 13:39:37, both EARLY_LOSS_CUT reasons carried:

    intel stale 19804s>90s
    intel stale 19801s>90s

19800s is 5h30m — the IST offset. The true ages were 4 and 1 seconds.

_intel_age_seconds built `now_ts` from a NAIVE latest_time via
pd.Timestamp(lt).timestamp(), which reads a naive value as UTC. Every naive
datetime in this codebase is an IST wall clock, so the epoch came out 19800s too
large. _cache_timeframe_intel stores `as_of = time.time()` — a true epoch — so
the subtraction added the offset straight into the age.

The datetime-candidate branch made the same naive-as-UTC assumption, so ITS error
cancelled and those readings were right. That is why the bug survived: the
function was internally inconsistent, correct on one path and wrong on the other.
Both paths are now true epochs.
"""
import time
from datetime import datetime, timedelta

import pytest

from scanner import Scanner
from trading.trade_manager import IST

IST_OFFSET = 19800.0


def _sc():
    sc = Scanner.__new__(Scanner)
    sc._last_intel_fetch = {}
    return sc


def _naive_now():
    """How latest_time is built everywhere in this codebase."""
    return datetime.now(IST).replace(tzinfo=None)


@pytest.mark.parametrize("true_age", [0.0, 1.0, 4.0, 120.0])
def test_epoch_intel_age_is_the_real_elapsed_time(true_age):
    intel = {"as_of": time.time() - true_age}

    age = Scanner._intel_age_seconds(_sc(), intel, "NIFTY", _naive_now())

    assert age == pytest.approx(true_age, abs=2.0)


def test_fresh_intel_is_not_reported_as_five_and_a_half_hours_old():
    """The exact shape of the two live log lines."""
    intel = {"as_of": time.time() - 4.0}

    age = Scanner._intel_age_seconds(_sc(), intel, "NIFTY", _naive_now())

    assert age < 90.0, f"fresh intel still reads stale: {age:.0f}s"
    assert age != pytest.approx(IST_OFFSET + 4.0, abs=5.0)


def test_naive_and_aware_latest_time_agree():
    intel = {"as_of": time.time() - 30.0}
    sc = _sc()

    naive = Scanner._intel_age_seconds(sc, intel, "NIFTY", datetime.now(IST).replace(tzinfo=None))
    aware = Scanner._intel_age_seconds(sc, intel, "NIFTY", datetime.now(IST))

    assert naive == pytest.approx(aware, abs=2.0)


def test_genuinely_stale_intel_is_still_stale():
    """The guard must keep working — this is not a licence to trust old intel."""
    intel = {"as_of": time.time() - 600.0}

    age = Scanner._intel_age_seconds(_sc(), intel, "NIFTY", _naive_now())

    assert age == pytest.approx(600.0, abs=2.0)
    assert age > 90.0


def test_a_naive_datetime_candidate_still_reads_correctly():
    """This path was right by cancellation; it must stay right without it."""
    intel = {"as_of": _naive_now() - timedelta(seconds=45)}

    age = Scanner._intel_age_seconds(_sc(), intel, "NIFTY", _naive_now())

    assert age == pytest.approx(45.0, abs=2.0)


def test_an_aware_datetime_candidate_reads_correctly():
    intel = {"as_of": datetime.now(IST) - timedelta(seconds=45)}

    age = Scanner._intel_age_seconds(_sc(), intel, "NIFTY", _naive_now())

    assert age == pytest.approx(45.0, abs=2.0)


def test_an_explicit_age_seconds_field_is_passed_through():
    age = Scanner._intel_age_seconds(_sc(), {"age_seconds": 12.0}, "NIFTY", _naive_now())

    assert age == pytest.approx(12.0)


def test_the_last_fetch_fallback_is_real_elapsed_time():
    sc = _sc()
    sc._last_intel_fetch = {"NIFTY": time.time() - 25.0}

    age = Scanner._intel_age_seconds(sc, {}, "NIFTY", _naive_now())

    assert age == pytest.approx(25.0, abs=2.0)
