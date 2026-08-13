"""A cleared scan cache market-exited live positions.

`_repaint_signal_status` is deliberately tri-state and its docstring names the reason:
VANISHED is "the indicator WITHDREW its claim", FLIPPED is "evidence AGAINST the trade",
and UNKNOWN is "cannot tell — no cached scan yet, or no timestamp to match on".

Every consumer read it through `_live_utbot_signal_still_present`, which is
`status == "PRESENT"`. That boolean collapses UNKNOWN into VANISHED. So "I cannot see
the arrow" and "the arrow is gone" became the same input to a real-money exit.

`_cached_results.clear()` is called from ordinary operator actions while a repaint
tracker is in flight:

    scanner.py:888  / 5909   day rollover
    scanner.py:3868          configure(reset=True)
    scanner.py:3931          configure -> needs_full_refresh (ANY settings save)
    scanner.py:4032          perform_system_recalibration
    engine/scanner_recalculation.py:374
    engine/scanner_chart_mixin.py:336  invalidate_chart_caches (scanner-engine switch)

So: operator saves a setting at 10:32:05; at the tracker's next 20s tick the cache is
empty, status is UNKNOWN, and a NIFTY position that never repainted is closed
REPAINT_ABORT at market.

FIX: `_repaint_absence_is_definite` — only VANISHED/FLIPPED count as evidence. UNKNOWN
falls through to the candle-close check and re-reads on the next tick, so a genuine
repaint is still caught; a cleared cache costs one 20s cycle instead of a forced exit.
Applied to all four consumers: the real-money tracker, both session-mirror branches, and
the post-close severity flag.

Deliberately NOT fixed by making _repaint_signal_status return PRESENT on an empty
cache — that would disarm the guard system-wide.
"""
import asyncio
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from scanner import Scanner
from trading.trade_manager import IST

INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]


def _scanner_with_cache(cache):
    s = Scanner.__new__(Scanner)
    s._cached_results = cache
    return s


def _cand(instrument="NIFTY", direction="LONG", tf="5min"):
    return SimpleNamespace(instrument=instrument, timeframe=tf, direction=direction,
                           source_signal_type="BUY" if direction == "LONG" else "SELL")


# ── the rule ─────────────────────────────────────────────────────────────────

def test_an_empty_cache_is_not_definite_absence():
    """The headline: a cleared cache must not read as a repaint."""
    s = _scanner_with_cache({})
    assert s._repaint_absence_is_definite(_cand(), datetime(2026, 8, 7, 10, 30)) is False


def test_a_missing_cache_attribute_is_not_definite_absence():
    s = Scanner.__new__(Scanner)
    assert s._repaint_absence_is_definite(_cand(), datetime(2026, 8, 7, 10, 30)) is False


def test_a_missing_timestamp_is_not_definite_absence():
    s = _scanner_with_cache({"NIFTY_5min": {"signals": [SimpleNamespace(
        timestamp=datetime(2026, 8, 7, 10, 30), signal_type="BUY")]}})
    assert s._repaint_absence_is_definite(_cand(), None) is False


@pytest.mark.parametrize("index", INDICES)
def test_a_real_vanish_is_still_definite(index):
    """The guard must keep working. A populated cache with no matching arrow = VANISHED."""
    ts = datetime(2026, 8, 7, 10, 30)
    s = _scanner_with_cache({f"{index}_5min": {"signals": [
        SimpleNamespace(timestamp=datetime(2026, 8, 7, 10, 25), signal_type="BUY")]}})
    assert s._repaint_absence_is_definite(_cand(index), ts) is True


@pytest.mark.parametrize("direction,flipped", [("LONG", "SELL"), ("SHORT", "BUY")])
def test_a_flip_is_still_definite(direction, flipped):
    """Bidirectional: the system buys CE and PE, so both flip directions must count."""
    ts = datetime(2026, 8, 7, 10, 30)
    s = _scanner_with_cache({"NIFTY_5min": {"signals": [
        SimpleNamespace(timestamp=ts, signal_type=flipped)]}})
    assert s._repaint_absence_is_definite(_cand(direction=direction), ts) is True


def test_a_present_arrow_is_not_absence():
    ts = datetime(2026, 8, 7, 10, 30)
    s = _scanner_with_cache({"NIFTY_5min": {"signals": [
        SimpleNamespace(timestamp=ts, signal_type="BUY")]}})
    assert s._repaint_absence_is_definite(_cand(), ts) is False


def test_a_raising_status_is_not_absence():
    """Fail SAFE: an unreadable status is 'cannot tell', not 'gone'."""
    s = Scanner.__new__(Scanner)

    def _boom(*_a):
        raise RuntimeError("cache backend down")

    s._repaint_signal_status = _boom
    assert s._repaint_absence_is_definite(_cand(), datetime(2026, 8, 7)) is False


# ── the real-money tracker honours it ────────────────────────────────────────

def _run_tracker(status, monkeypatch, entry=100.0, mark=99.0):
    async def no_wait(_s):
        return None

    monkeypatch.setattr(scanner_module.asyncio, "sleep", no_wait)
    candidate = SimpleNamespace(
        instrument="NIFTY", timeframe="5min", direction="LONG",
        signal_timestamp=datetime.now(IST) - timedelta(minutes=6),
    )
    trade = SimpleNamespace(id="T9", status="OPEN", entry_price=entry, current_price=mark)
    closed = []
    dummy = SimpleNamespace(
        _repaint_signal_status=lambda _c, _t: status,
        candles=SimpleNamespace(get_latest_price=lambda _i: mark),
        _close_and_record=lambda *a, **k: closed.append(a),
        log_event=lambda *_a: None,
    )
    # asyncio.run, not get_event_loop().run_until_complete: the latter adopts whatever
    # loop the previously-run test left behind. Once any earlier test calls asyncio.run
    # the policy's loop is closed, so this became order-dependent -- green in file order,
    # a RuntimeError under a shuffled suite. asyncio.run makes and disposes its own loop.
    asyncio.run(Scanner._track_repaint_until_candle_close(dummy, trade, candidate))
    return trade, closed


def test_the_tracker_does_not_abort_on_an_unknown_status(monkeypatch):
    """The money assertion: a settings save must not close a live position."""
    trade, closed = _run_tracker("UNKNOWN", monkeypatch)
    assert closed == [], f"a cleared cache still market-exited the position: {closed}"
    assert trade.repaint_guard_checked is True, "the tracker must still finish at close"


def test_the_tracker_still_aborts_on_a_real_vanish(monkeypatch):
    _, closed = _run_tracker("VANISHED", monkeypatch)
    assert closed == [("T9", 99.0, "REPAINT_ABORT")]


def test_the_tracker_still_aborts_on_a_flip(monkeypatch):
    _, closed = _run_tracker("FLIPPED", monkeypatch)
    assert closed == [("T9", 99.0, "REPAINT_ABORT")]


def test_the_tracker_holds_a_present_arrow(monkeypatch):
    _, closed = _run_tracker("PRESENT", monkeypatch)
    assert closed == []


# ── every consumer routes through the rule ───────────────────────────────────

def test_no_repaint_consumer_still_reads_the_boolean_wrapper():
    """_live_utbot_signal_still_present cannot distinguish gone from unseen."""
    src = open("scanner.py", encoding="utf-8").read()
    lines = [
        f"{i}: {ln.strip()}"
        for i, ln in enumerate(src.splitlines(), 1)
        if "_live_utbot_signal_still_present(" in ln
        and "def " not in ln
        and not ln.strip().startswith("#")
    ]
    assert not lines, (
        "a repaint decision still reads the True/False wrapper: " + "; ".join(lines)
    )


def test_the_helper_is_used_by_the_tracker_and_both_mirror_branches():
    src = open("scanner.py", encoding="utf-8").read()
    assert src.count("_repaint_absence_is_definite(") >= 5


def test_the_status_function_still_reports_unknown_for_an_empty_cache():
    """The premise. If this ever returns PRESENT instead, the guard is disarmed."""
    s = _scanner_with_cache({})
    assert s._repaint_signal_status(_cand(), datetime(2026, 8, 7, 10, 30)) == "UNKNOWN"


def test_the_clear_sites_still_exist():
    """If nothing clears the cache any more, this whole family is moot — say so loudly."""
    src = open("scanner.py", encoding="utf-8").read()
    assert src.count("_cached_results.clear()") >= 3, (
        "the cache-clear sites moved; re-check whether this guard is still needed"
    )
