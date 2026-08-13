"""The 15-second decision cache was blind to the 1-minute lane.

scanner.py:10405 reuses a graded candidate list for up to 15 seconds in REAL mode:

    elif (self.mode == "REAL" and not has_forming_signal
          and decision_cache and decision_cache.get("key") == decision_key
          and now - decision_cache.get("time", 0.0) <= 15.0):
        candidates = decision_cache.get("candidates", [])

Two things decided whether that reuse was safe, and neither looked at 1min:

    decision_key              built from results_5min and results_15min only
    _mtf_has_forming_signal   iterated ("5min", ...), ("15min", ...) only

1min is not a dormant lane. signal_processor.py:2641 inserts it at the FRONT of
candidate_timeframes whenever volume_spike_entry_enabled and
one_min_scalping_enabled are on -- both default True and neither is overridden in
the operator's .env -- or whenever a post-exit reversion window is open.

So on every live scan, for up to 15 seconds:

  * a brand-new 1-minute signal did not change the key, and the cache kept
    serving a candidate list computed before that signal existed; and
  * a signal on a still-forming 1-minute candle did not mark the cache unsafe,
    so a decision graded on partial data was reused. Indicator values (ADX,
    volume, intelligence) are not part of the key, which is the whole reason the
    forming-candle guard exists.

The fastest lane, and the one likeliest to repaint, was the one lane neither
mechanism watched.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from scanner import Scanner


class _Sig:
    def __init__(self, ts):
        self.timestamp = ts


def _mtf(one_min=None, five_min=None, fifteen_min=None):
    def _wrap(sigs):
        return {"signals": list(sigs)} if sigs is not None else None

    return SimpleNamespace(
        results_1min=_wrap(one_min),
        results_5min=_wrap(five_min),
        results_15min=_wrap(fifteen_min),
    )


@pytest.fixture(autouse=True)
def _intracandle_on(monkeypatch):
    """The guard short-circuits to False unless intracandle signals are enabled."""
    monkeypatch.setattr(Scanner, "_intracandle_signals_enabled", staticmethod(lambda: True))


NOW = datetime(2026, 8, 13, 11, 30, 30)


# ── the forming-candle guard now watches the 1-minute lane ───────────────────

def test_a_forming_one_minute_candle_marks_the_cache_unsafe():
    """The regression: this returned False, so a partial 1m grade was reusable."""
    mtf = _mtf(one_min=[_Sig(NOW - timedelta(seconds=20))])

    assert Scanner._mtf_has_forming_signal(mtf, now=NOW) is True


def test_a_closed_one_minute_candle_does_not_block_the_cache():
    """Older than its own bar length — settled, so reuse stays allowed."""
    mtf = _mtf(one_min=[_Sig(NOW - timedelta(minutes=3))])

    assert Scanner._mtf_has_forming_signal(mtf, now=NOW) is False


def test_the_one_minute_window_is_one_minute_not_fifteen():
    """`5 if tf == "5min" else 15` would have given a 1m candle a 15m window,
    holding the cache unsafe for fourteen minutes after the bar had closed."""
    settled = _mtf(one_min=[_Sig(NOW - timedelta(minutes=2))])

    assert Scanner._mtf_has_forming_signal(settled, now=NOW) is False


# ── the existing lanes are unchanged ─────────────────────────────────────────

def test_a_forming_five_minute_candle_still_blocks():
    mtf = _mtf(five_min=[_Sig(NOW - timedelta(minutes=2))])

    assert Scanner._mtf_has_forming_signal(mtf, now=NOW) is True


def test_a_forming_fifteen_minute_candle_still_blocks():
    mtf = _mtf(fifteen_min=[_Sig(NOW - timedelta(minutes=9))])

    assert Scanner._mtf_has_forming_signal(mtf, now=NOW) is True


def test_all_lanes_settled_allows_reuse():
    mtf = _mtf(
        one_min=[_Sig(NOW - timedelta(minutes=3))],
        five_min=[_Sig(NOW - timedelta(minutes=7))],
        fifteen_min=[_Sig(NOW - timedelta(minutes=20))],
    )

    assert Scanner._mtf_has_forming_signal(mtf, now=NOW) is False


def test_no_signals_at_all_allows_reuse():
    assert Scanner._mtf_has_forming_signal(_mtf(), now=NOW) is False


def test_a_missing_one_minute_result_is_not_an_error():
    """results_1min is Optional on the MTF result and is None before first fill."""
    mtf = _mtf(five_min=[_Sig(NOW - timedelta(minutes=7))])

    assert Scanner._mtf_has_forming_signal(mtf, now=NOW) is False


def test_a_signal_without_a_timestamp_is_skipped_not_crashed():
    mtf = _mtf(one_min=[_Sig(None)])

    assert Scanner._mtf_has_forming_signal(mtf, now=NOW) is False


# ── the cache identity includes the 1-minute lane ────────────────────────────

def test_the_decision_key_is_built_from_all_three_lanes():
    """Pinned at source: a 1m signal must be able to change the cache identity.

    Without this the key is identical before and after a 1-minute entry signal
    appears, and the cached list — which predates it — is served as current.
    """
    import inspect

    src = inspect.getsource(Scanner)
    i = src.index("signal_parts = []")
    window = src[i: i + 700]

    assert '("1min", getattr(mtf_result, "results_1min", None))' in window, (
        "1min absent from the decision key: a fresh 1-minute signal cannot bust the cache"
    )
    assert '("5min", mtf_result.results_5min)' in window
    assert '("15min", mtf_result.results_15min)' in window


def test_the_forming_guard_covers_every_lane_the_key_does():
    """The two mechanisms must not drift apart — each is the other's backstop."""
    import inspect

    src = inspect.getsource(Scanner._mtf_has_forming_signal)

    for tf in ("1min", "5min", "15min"):
        assert f'"{tf}", "results_{tf}"' in src, f"{tf} unguarded against forming candles"
