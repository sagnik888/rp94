"""The 100ms exit loop could close a position on intelligence of unbounded age.

`_update_active_trades(allow_blocking=False)` runs on the event loop's fast path. Its
intelligence early-exit reads `_cached_timeframe_intel`, which is a plain dict lookup with
no age check, and feeds it to `_intel_reversal_reason` -> `_close_and_record(...,
"OI_PCR_REVERSAL")`. A real position, squared off on a cached OI/PCR flip of any age.

Reading the cache on the fast path is CORRECT and deliberate — recomputing the bundle was
measured at 48ms inside a 100ms budget. What was missing is the staleness bound.

The failure is worst exactly when the fast path matters most: when the scan loop stalls,
the bundle stops refreshing while this loop keeps acting on it, so the exit fires on
evidence that may have reverted minutes earlier.

The TTL primitive existed and was applied on two other paths; this one never got it.
`loss_guard_intel_max_age_seconds` (90s) is the codebase's existing "too old to drive a
decision" bound and sits well above the 30s recompute TTL, so a healthy loop is untouched.
"""
import ast
import inspect
import textwrap

import pytest

from scanner import Scanner
from engine.exit_cycle import ExitCycleService


def _exit_loop_source():
    return textwrap.dedent(inspect.getsource(ExitCycleService.update_active_trades))


def test_the_fast_path_bounds_the_age_of_its_cached_intelligence():
    src = _exit_loop_source()
    assert "loss_guard_intel_max_age_seconds" in src, (
        "the 100ms exit path still acts on cached intelligence of unbounded age"
    )
    assert "_intel_age_seconds" in src


def test_the_bound_is_applied_only_on_the_non_blocking_path():
    """The blocking path recomputes, so it is fresh by construction."""
    src = _exit_loop_source()
    marker = "loss_guard_intel_max_age_seconds"
    window = src[max(0, src.index(marker) - 1200):src.index(marker)]
    assert "not allow_blocking" in window


def _age_guard():
    """The `if age is not None and age > max_age:` node, parsed not windowed.

    A first draft took a fixed 900-character slice from the marker and missed the guard
    entirely — the same char-budget brittleness already fixed twice in this suite today.
    """
    tree = ast.parse(_exit_loop_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        dumped = ast.dump(node.test)
        if "'age'" in dumped and "max_age" in dumped:
            return node
    raise AssertionError("no age guard found on the fast path")


def test_a_stale_read_is_dropped_rather_than_acted_on():
    assigned = [
        t.id
        for n in ast.walk(_age_guard())
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    ]
    assert "intel_result" in assigned, (
        "a stale bundle is still passed to the reversal check"
    )


def test_the_drop_is_counted_so_a_stalled_loop_is_visible():
    src = _exit_loop_source()
    assert "fast_path_stale_skipped" in src


def test_the_age_helper_still_returns_none_when_unknown():
    """The bound must not fire on 'cannot say' — that would disable the exit entirely."""
    s = Scanner.__new__(Scanner)
    assert Scanner._intel_age_seconds(s, None, "NIFTY", None) is None
    assert Scanner._intel_age_seconds(s, {}, "NIFTY", None) is None


def test_an_unknown_age_does_not_drop_the_reading():
    """`age is not None` must guard the comparison; unknown has to stay usable.

    Without it, every trade whose intel carries no timestamp loses the reversal exit
    entirely — turning a staleness bound into a silent feature removal.
    """
    test = _age_guard().test
    assert isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And), (
        "the age comparison is not guarded against an unknown age"
    )
    assert any(
        isinstance(v, ast.Compare) and isinstance(v.ops[0], ast.IsNot)
        for v in test.values
    ), "no `age is not None` arm on the staleness guard"


def test_the_setting_this_reuses_still_exists_and_is_consumed():
    """Premise: if it disappears, the 90s default here becomes a magic number."""
    from config.settings import get_settings

    assert float(getattr(get_settings(), "loss_guard_intel_max_age_seconds", 0.0)) > 0.0
    assert "loss_guard_intel_max_age_seconds" in inspect.getsource(
        Scanner._loss_guard_severity
    ) or "loss_guard_intel_max_age_seconds" in _exit_loop_source()


def test_the_blocking_path_still_recomputes():
    """The fix must not turn the unhurried pass into a cache read too."""
    tree = ast.parse(_exit_loop_source())
    calls = {
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "_get_trade_intel" in calls
    assert "_cached_timeframe_intel" in calls
