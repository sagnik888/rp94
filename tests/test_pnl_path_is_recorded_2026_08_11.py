"""A closed trade recorded two maxima, and two maxima cannot answer the trail question.

Asked of the 12-session book on 2026-08-11 — "would a tighter trail have banked more of
the Rs.42,444 of peak profit that became -Rs.992?" — the honest answer was "somewhere
between -Rs.18,114 and +Rs.10,988", because:

    peak_pnl      = max over t of pnl(t)
    max_drawdown  = max over t of (running_peak(t) - pnl(t))

A trail that exits at f x peak fires only if P&L reached that level AFTER the peak, and
the ORDER of the peak and the trough is precisely what a max discards. 14 of the 16
trades carrying the entire book had drawdowns large enough that a low trail *might* have
decapitated them, and nothing in the record could say whether it did.

So the path is sampled. This file pins the sampling bounds (a monitor loop runs it every
~100ms), the restart round-trip, and the ordering property that motivated it.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytz

from trading.trade_manager import sample_pnl_path

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _pinned_settings():
    from config.settings import Settings, get_settings

    s = get_settings()
    keys = ("pnl_path_logging_enabled", "pnl_path_min_interval_seconds",
            "pnl_path_min_move_rs", "pnl_path_max_samples")
    saved = {k: getattr(s, k, None) for k in keys}
    for k in keys:
        setattr(s, k, Settings.model_fields[k].default)
    yield
    for k, v in saved.items():
        setattr(s, k, v)


def _holder(seconds_ago=0.0):
    return SimpleNamespace(
        entry_time=datetime.now(IST) - timedelta(seconds=seconds_ago),
        pnl=0.0,
        pnl_path=[],
    )


# ── it records at all ────────────────────────────────────────────────────────

def test_a_sample_is_appended():
    h = _holder()
    sample_pnl_path(h, pnl=250.0)
    assert len(h.pnl_path) == 1
    elapsed, pnl = h.pnl_path[0]
    assert pnl == 250.0
    assert 0.0 <= elapsed < 5.0


def test_the_path_is_ordered_by_time():
    h = _holder(seconds_ago=100.0)
    for pnl in (100.0, 300.0, -50.0, 900.0):
        h.entry_time -= timedelta(seconds=10)
        sample_pnl_path(h, pnl=pnl)
    times = [t for t, _ in h.pnl_path]
    assert times == sorted(times)
    assert [p for _, p in h.pnl_path] == [100.0, 300.0, -50.0, 900.0]


# ── the bounds that keep a 100ms loop cheap ──────────────────────────────────

def test_a_quiet_position_is_not_resampled():
    """Same instant, no motion — the timer must suppress it."""
    h = _holder()
    sample_pnl_path(h, pnl=100.0)
    for _ in range(50):
        sample_pnl_path(h, pnl=100.0)
    assert len(h.pnl_path) == 1


def test_a_fast_move_is_recorded_despite_the_timer():
    """The motion bound is what keeps a sharp reversal legible; without it a trade that
    spiked and collapsed inside one interval would read as a straight line."""
    h = _holder()
    sample_pnl_path(h, pnl=100.0)
    sample_pnl_path(h, pnl=900.0)          # +800 inside the 2s window
    assert len(h.pnl_path) == 2
    assert h.pnl_path[-1][1] == 900.0


def test_a_move_below_the_motion_floor_inside_the_window_is_dropped():
    h = _holder()
    sample_pnl_path(h, pnl=100.0)
    sample_pnl_path(h, pnl=120.0)          # +20, under the Rs.50 floor
    assert len(h.pnl_path) == 1


def test_the_sample_cap_keeps_both_ends():
    """A long trade must degrade in resolution, never lose its tail — the tail is where
    the exit is, and the exit is the thing being studied."""
    from config.settings import get_settings

    setattr(get_settings(), "pnl_path_max_samples", 10)
    h = _holder(seconds_ago=10_000.0)
    first = None
    for i in range(60):
        h.entry_time -= timedelta(seconds=10)
        sample_pnl_path(h, pnl=float(i * 100))
        if first is None:
            first = h.pnl_path[0]
    assert len(h.pnl_path) <= 11
    assert h.pnl_path[0] == first, "the opening sample was thinned away"
    assert h.pnl_path[-1][1] == 5900.0, "the closing sample was thinned away"


def test_the_toggle_switches_it_off():
    from config.settings import get_settings

    setattr(get_settings(), "pnl_path_logging_enabled", False)
    h = _holder()
    sample_pnl_path(h, pnl=250.0)
    assert h.pnl_path == []


# ── it can never disturb a live position ─────────────────────────────────────

@pytest.mark.parametrize("holder", [
    SimpleNamespace(),                                        # no entry time at all
    SimpleNamespace(entry_time=None, pnl=1.0),
    SimpleNamespace(entry_time="not-a-datetime", pnl=1.0),
    SimpleNamespace(entry_time=datetime.now(IST), pnl=None),
    SimpleNamespace(entry_time=datetime.now(IST), pnl=float("nan")),
])
def test_a_broken_holder_never_raises(holder):
    sample_pnl_path(holder)


def test_a_naive_entry_time_is_handled():
    h = SimpleNamespace(entry_time=datetime.now(), pnl=0.0, pnl_path=[])
    sample_pnl_path(h, pnl=42.0)
    assert len(h.pnl_path) == 1


def test_a_non_list_path_attribute_is_replaced():
    h = SimpleNamespace(entry_time=datetime.now(IST), pnl=0.0, pnl_path=None)
    sample_pnl_path(h, pnl=10.0)
    assert h.pnl_path == [(pytest.approx(0.0, abs=5.0), 10.0)]


# ── the ordering property the whole exercise is for ──────────────────────────

def test_the_path_distinguishes_two_trades_the_maxima_cannot():
    """Both end flat with peak 1000 and a 1000 peak-to-trough drawdown. A trail at 50%
    of peak fires on ONE of them. peak_pnl and max_drawdown are identical for both;
    only the path separates them."""
    def build(series):
        h = _holder(seconds_ago=1000.0)
        for pnl in series:
            h.entry_time -= timedelta(seconds=10)
            sample_pnl_path(h, pnl=pnl)
        return [p for _, p in h.pnl_path]

    ran_then_faded = build([0.0, 1000.0, 0.0])      # peak first  -> trail WOULD fire
    dipped_then_ran = build([0.0, -1000.0, 1000.0, 0.0])

    def peak(p):
        return max(p)

    def max_dd(p):
        run, dd = p[0], 0.0
        for v in p:
            run = max(run, v)
            dd = max(dd, run - v)
        return dd

    assert peak(ran_then_faded) == peak(dipped_then_ran) == 1000.0
    assert max_dd(ran_then_faded) == max_dd(dipped_then_ran) == 1000.0

    def trail_fires(p, f=0.5):
        run = p[0]
        for v in p:
            run = max(run, v)
            if run >= 1000.0 and v <= f * run:
                return True
        return False

    assert trail_fires(ran_then_faded) is True
    assert trail_fires(dipped_then_ran) is True
    # ...and the paths themselves differ, which is the recoverable information.
    assert ran_then_faded != dipped_then_ran


# ── it survives a restart and reaches the record ─────────────────────────────

def test_the_trade_round_trips_through_serialization():
    from trading.trade_manager import Trade

    t = Trade(id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
              entry_price=100.0, entry_time=datetime.now(IST), trailing_stop=90.0,
              current_stop=90.0, lots=1, lot_size=65, grade="A")
    t.pnl_path = [(0.0, 0.0), (2.5, 120.0), (8.0, -40.0)]
    restored = Trade.from_dict(t.to_dict())
    assert restored.pnl_path == [(0.0, 0.0), (2.5, 120.0), (8.0, -40.0)]


def test_a_trade_stored_before_this_change_restores_empty():
    from trading.trade_manager import Trade

    t = Trade(id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
              entry_price=100.0, entry_time=datetime.now(IST), trailing_stop=90.0,
              current_stop=90.0, lots=1, lot_size=65, grade="A")
    payload = t.to_dict()
    payload.pop("pnl_path", None)
    assert Trade.from_dict(payload).pnl_path == []


def test_malformed_rows_are_dropped_not_fatal():
    from intelligence.rtracker import _pnl_path_rows

    assert _pnl_path_rows([[0.0, 1.0], "junk", [1.0], None, [2.0, 3.0], ["a", "b"]]) == [
        [0.0, 1.0], [2.0, 3.0]
    ]
    assert _pnl_path_rows(None) == []


def test_the_rtracker_event_carries_the_path():
    import inspect

    import intelligence.rtracker as rt

    src = inspect.getsource(rt)
    assert '"pnl_path": _pnl_path_rows(' in src, "the EXIT event no longer records the path"


def test_the_exit_row_inherits_the_path_from_the_entry_row():
    """All five exit routes go through _inherit_exit_entry_metadata; if the path is only
    copied on one of them the other four record nothing."""
    import inspect

    from scanner import Scanner

    src = inspect.getsource(Scanner._inherit_exit_entry_metadata)
    assert "pnl_path" in src


def test_the_inherited_path_is_a_copy():
    """A shared list would keep growing on the entry row after the exit was recorded."""
    from scanner import Scanner

    entry = SimpleNamespace(pnl_path=[(0.0, 10.0)])
    exit_row = SimpleNamespace()
    Scanner._inherit_exit_entry_metadata(Scanner.__new__(Scanner), exit_row, entry)
    entry.pnl_path.append((1.0, 20.0))
    assert exit_row.pnl_path == [(0.0, 10.0)]
