"""The closed-candle fast path skipped two gates it had no business skipping.

A signal whose source bar has already CLOSED does not belong in an anti-repaint buffer —
a final bar's arrow cannot repaint, so waiting 30s buys nothing. That shortcut is right.

But it was implemented as `matured_candidates.append(c); continue`, and `continue` leaves
the loop body before BOTH of the things below it:

  1. `_entry_cutoff_allows_candidate` — the per-timeframe no-entry-after policy. Both
     enforcement points live further down: the pre-buffer check, and the matured check
     inside the `_pending_live_signals` loop. A closed-candle candidate never enters that
     dict, so it reaches neither. The wave-level check higher up is an `any()` across all
     candidates, so one eligible candidate lets the whole wave through.

     Failure: a 15min closed bar at 15:10 opens a fresh bought option with 15 minutes to
     the 15:25 force exit, past the operator's 15:00 cutoff.

  2. `_evict_superseded_opposite_buffer` — its only call site is on the buffering path.

     Failure: a LONG is buffered and stabilising. The chart flips SHORT on the NEXT bar.
     The SHORT arrives as a closed-candle candidate, `continue`s, and never evicts the
     stale LONG. The repaint gate cannot save it either — `_repaint_signal_status` looks
     the buffered arrow up in the cached signals list, and that list holds the WHOLE
     session's flip history, so after a cross-bar reversal the old LONG arrow is still
     present and reads PRESENT. The stale LONG matures and enters.

     Options are always BOUGHT here, so that is a long CE and a long PE on one chart,
     two premiums and two spreads, theta against both. Nobody chose that straddle.

Requirement 21 is precisely "a buy signal can turn to sell immediately — check the system
performs this transition without mistakes".
"""
import ast
import inspect
import textwrap
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from scanner import Scanner

IST_NAIVE = datetime(2026, 8, 7, 15, 10, 0)


def _candidate(direction="LONG", timeframe="15min", instrument="NIFTY", ts=None):
    return SimpleNamespace(
        instrument=instrument,
        timeframe=timeframe,
        direction=direction,
        action="ENTRY",
        entered_intracandle=False,
        signal_timestamp=ts,
        reasons=[],
    )


# ── the eviction gate ────────────────────────────────────────────────────────

def _scanner_with_buffer(stale_direction, stale_ts):
    s = Scanner.__new__(Scanner)
    s._pending_live_signals = {
        f"NIFTY_15min_{stale_direction}": {
            "timestamp": IST_NAIVE,
            "candidate": _candidate(stale_direction, ts=stale_ts),
            "sig_timestamp": stale_ts,
            "stable_scans": 0,
        }
    }
    s._diagnostics = {}
    s._diag_inc = lambda *a, **k: None
    s.log_event = lambda *a, **k: None
    s._rtracker_record_candidate = lambda *a, **k: None
    s._forget_signal_display_rows = lambda *a, **k: None
    s._naive_ist_time = lambda v: v
    return s


def test_a_closed_bar_flip_evicts_the_stale_opposite_buffer():
    """The core requirement-21 case: cross-bar LONG -> SHORT."""
    old = IST_NAIVE - timedelta(minutes=15)
    s = _scanner_with_buffer("LONG", old)
    Scanner._evict_superseded_opposite_buffer(s, _candidate("SHORT", ts=IST_NAIVE), IST_NAIVE)
    assert "NIFTY_15min_LONG" not in s._pending_live_signals, (
        "a stale LONG survived a SHORT flip and can still mature into a second bought leg"
    )


def test_a_newer_buffered_arrow_is_left_alone():
    """If the incoming one is the stale side, the repaint gate handles it instead."""
    newer = IST_NAIVE
    s = _scanner_with_buffer("LONG", newer)
    Scanner._evict_superseded_opposite_buffer(
        s, _candidate("SHORT", ts=IST_NAIVE - timedelta(minutes=15)), IST_NAIVE - timedelta(minutes=15)
    )
    assert "NIFTY_15min_LONG" in s._pending_live_signals


def test_eviction_is_scoped_to_the_same_chart():
    old = IST_NAIVE - timedelta(minutes=15)
    s = _scanner_with_buffer("LONG", old)
    Scanner._evict_superseded_opposite_buffer(
        s, _candidate("SHORT", timeframe="5min", ts=IST_NAIVE), IST_NAIVE
    )
    assert "NIFTY_15min_LONG" in s._pending_live_signals, "evicted another timeframe's buffer"


def test_no_buffer_is_a_no_op():
    s = _scanner_with_buffer("LONG", IST_NAIVE)
    s._pending_live_signals = {}
    Scanner._evict_superseded_opposite_buffer(s, _candidate("SHORT", ts=IST_NAIVE), IST_NAIVE)
    assert s._pending_live_signals == {}


# ── the wiring: the fast path must reach BOTH gates ──────────────────────────

def _fast_path_branch():
    """The `if not bool(getattr(c, 'entered_intracandle', True)):` body, as an AST."""
    src = textwrap.dedent(inspect.getsource(Scanner._coordinate_and_execute))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = ast.dump(node.test)
        if "entered_intracandle" in test and "UnaryOp" in test:
            return node
    raise AssertionError("closed-candle fast path branch not found")


def _calls_in(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out


def test_the_fast_path_applies_the_entry_cutoff():
    calls = _calls_in(_fast_path_branch())
    assert "_entry_cutoff_allows_candidate" in calls, (
        "a closed candle bypasses the no-entry-after policy: both other cutoff checks "
        "are below the `continue`, and the wave-level one is an any() across candidates"
    )


def test_the_fast_path_evicts_a_superseded_opposite_buffer():
    calls = _calls_in(_fast_path_branch())
    assert "_evict_superseded_opposite_buffer" in calls, (
        "a closed-bar flip cannot kill the stale opposite buffer, so both directions "
        "can mature and open opposing bought legs on one chart"
    )


def test_the_cutoff_is_checked_before_the_candidate_is_accepted():
    """Order matters: gating after the append would let it through anyway."""
    branch = _fast_path_branch()
    cutoff_line = min(
        n.lineno for n in ast.walk(branch)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_entry_cutoff_allows_candidate"
    )
    append_line = min(
        n.lineno for n in ast.walk(branch)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "append"
    )
    assert cutoff_line < append_line, "cutoff is evaluated after the candidate is accepted"


def test_the_eviction_happens_before_the_candidate_is_accepted():
    branch = _fast_path_branch()
    evict_line = min(
        n.lineno for n in ast.walk(branch)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_evict_superseded_opposite_buffer"
    )
    append_line = min(
        n.lineno for n in ast.walk(branch)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "append"
    )
    assert evict_line < append_line


def test_the_fast_path_still_skips_the_stabilization_wait():
    """What the shortcut exists for must survive these additions."""
    branch = _fast_path_branch()
    calls = _calls_in(branch)
    assert "_live_signal_stabilization_requirements" not in calls
    src = ast.dump(branch)
    assert "_pending_live_signals" not in src, (
        "the closed-candle path put the candidate back into the anti-repaint buffer"
    )
