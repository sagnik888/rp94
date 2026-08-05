"""allow_blocking=False must reach every slow call in the loop, not just the first one.

_update_active_trades is called two ways: unhurried from the scan cycle, and with
allow_blocking=False from the 100ms scanner event loop. The flag was threaded to
_get_live_premium_cached and stopped there. The intelligence early-exit check a few lines
below went on calling _get_trade_intel, which recomputes the whole bundle when the cache
has expired — measured at 48ms with a 40-strike chain, inside a 100ms budget.

That was already true at the old 90s TTL. Shortening the analysis TTL to 30s (so entries
stop being decided on 90-second-old evidence) makes it three times more frequent, which is
what turned a latent cost into one worth fixing.

ut_intel_early_exit is True in the live config, so this path runs on real money.
"""
from types import SimpleNamespace

import pytest

from scanner import Scanner


class _Recorder:
    """Stands in for the scanner, recording which intel path each pass takes."""

    def __init__(self, cached=None):
        self.recomputes = 0
        self.cache_reads = 0
        self._cached = cached

    def _get_trade_intel(self, trade):
        self.recomputes += 1
        return self._cached

    def _cached_timeframe_intel(self, instrument, tf, allow_fallback=True):
        self.cache_reads += 1
        return self._cached


def _run(allow_blocking, cached=None):
    """Drive the exact branch from _update_active_trades."""
    rec = _Recorder(cached)
    trade = SimpleNamespace(instrument="NIFTY", timeframe="5min")
    intel_result = (
        rec._get_trade_intel(trade)
        if allow_blocking
        else rec._cached_timeframe_intel(
            str(getattr(trade, "instrument", "") or "").upper(),
            str(getattr(trade, "timeframe", "5min") or "5min").lower(),
            allow_fallback=True,
        )
    )
    return rec, intel_result


def test_the_source_actually_branches_on_allow_blocking():
    """Pin the real code, not my reproduction of it.

    Parsed rather than grepped: a first draft asserted the string "allow_blocking"
    appeared near the call, which stayed true when the condition was replaced with a
    constant. The test has to name the condition.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(Scanner._update_active_trades)))
    ternaries = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.IfExp)
        and any(
            isinstance(c, ast.Attribute) and c.attr == "_get_trade_intel"
            for c in ast.walk(n.body)
        )
    ]
    assert ternaries, (
        "the intelligence early-exit no longer chooses between a recompute and a cache "
        "read: the 100ms scanner loop pays for a full recompute again"
    )
    guard = ternaries[0].test
    assert isinstance(guard, ast.Name) and guard.id == "allow_blocking", (
        f"the recompute is guarded by {ast.dump(guard)[:80]}, not by allow_blocking"
    )
    assert any(
        isinstance(c, ast.Attribute) and c.attr == "_cached_timeframe_intel"
        for c in ast.walk(ternaries[0].orelse)
    ), "the fast path does not fall back to the intelligence cache"


def test_the_fast_path_reads_the_cache_and_never_recomputes():
    rec, _ = _run(allow_blocking=False)
    assert rec.recomputes == 0, "recomputed intelligence on the 100ms event loop"
    assert rec.cache_reads == 1


def test_the_unhurried_path_still_recomputes():
    """The slow caller must keep getting fresh evidence."""
    rec, _ = _run(allow_blocking=True)
    assert rec.recomputes == 1
    assert rec.cache_reads == 0


def test_a_warm_cache_still_drives_the_early_exit_on_the_fast_path():
    """Skipping the recompute must not skip the CHECK when evidence is available."""
    intel = {"score": 33.0, "regime": "TRENDING_DOWN"}
    _rec, result = _run(allow_blocking=False, cached=intel)
    assert result is intel


def test_a_cold_cache_on_the_fast_path_simply_defers():
    """No evidence is not an exit signal; the next unhurried pass will have it."""
    _rec, result = _run(allow_blocking=False, cached=None)
    assert result is None


def test_the_hft_worker_still_calls_with_allow_blocking_false():
    """The whole fix rests on this call site keeping its flag."""
    import inspect

    src = inspect.getsource(Scanner)
    assert "_update_active_trades(allow_blocking=False)" in src, (
        "the 100ms worker no longer marks itself as the fast path"
    )
