"""Combined chart ticks respect the display floor without delaying evidence changes."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from types import SimpleNamespace
import threading

import pandas as pd
import pytest

from config.settings import Settings
from engine import ut_smart_engine as uts
from tests.test_chart_rebuild_floor_2026_09_16 import _Host, _frame


@pytest.fixture
def chart_host(monkeypatch):
    settings = Settings(_env_file=None)
    settings.chart_rebuild_min_interval_seconds = 2.0
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)
    monkeypatch.setattr("config.settings.get_settings", lambda: settings)
    clock = [pd.Timestamp("2026-10-05 12:00:30")]
    original_clock = uts.evaluation_clock
    monkeypatch.setattr(uts, "evaluation_clock", lambda as_of=None:
                        clock[0] if as_of is None else original_clock(as_of))
    frames = {}
    for tf in ("5min", "15min"):
        frame = _frame()
        frame.index = pd.date_range(end="2026-10-05 12:00", periods=len(frame), freq=tf)
        frames[tf] = frame
    host = _Host(frames["5min"])
    host.candles = SimpleNamespace(get_candles=lambda instrument, tf: frames.get(tf))
    return host, settings, frames, clock


@pytest.mark.parametrize("mode", ["quad_prime_s1", "ut_smart_s1"])
@pytest.mark.parametrize("changed_tf", ["5min", "15min"])
def test_combined_forming_ticks_reuse_chart_then_refresh_after_display_floor(chart_host, mode, changed_tf):
    host, settings, frames, _ = chart_host
    settings.scanner_engine = mode
    first = host._build_chart_from_cache("NIFTY")["5min"]
    frames[changed_tf].iloc[-1, frames[changed_tf].columns.get_loc("high")] += 1.0
    frames[changed_tf].iloc[-1, frames[changed_tf].columns.get_loc("close")] += 0.5
    assert host._build_chart_from_cache("NIFTY")["5min"] is first
    assert host.builds == 1
    host.chart_cache_NIFTY_5min["built_at"] -= 3.0
    host._build_chart_from_cache("NIFTY")
    assert host.builds == 2


@pytest.mark.parametrize("mode", ["quad_prime_s1", "ut_smart_s1"])
@pytest.mark.parametrize("changed_tf", ["5min", "15min"])
@pytest.mark.parametrize("terminal_closed", [False, True])
def test_combined_closed_corrections_bypass_chart_floor(chart_host, mode, changed_tf, terminal_closed):
    host, settings, frames, clock = chart_host
    settings.scanner_engine = mode
    if terminal_closed:
        clock[0] = pd.Timestamp("2026-10-05 12:16:01")
    host._build_chart_from_cache("NIFTY")
    row = -1 if terminal_closed else -3
    frames[changed_tf].iloc[row, frames[changed_tf].columns.get_loc("close")] += 1.0
    host._build_chart_from_cache("NIFTY")
    assert host.builds == 2


@pytest.mark.parametrize("mode", ["quad_prime_s1", "ut_smart_s1"])
def test_combined_dependency_close_bypasses_floor_without_a_tick(chart_host, mode):
    host, settings, _, clock = chart_host
    settings.scanner_engine = mode
    host.active_chart_tf = "15min"
    clock[0] = pd.Timestamp("2026-10-05 12:04:59")
    host._build_chart_from_cache("NIFTY")
    clock[0] += pd.Timedelta(seconds=2)
    host._build_chart_from_cache("NIFTY")
    assert host.builds == 2


@pytest.mark.parametrize("mode", ["quad_prime_s1", "ut_smart_s1"])
@pytest.mark.parametrize("action", ["force", "zero_floor", "settings", "new_bar"])
def test_combined_chart_preserves_immediate_changes(chart_host, mode, action):
    host, settings, frames, _ = chart_host
    settings.scanner_engine = mode
    host._build_chart_from_cache("NIFTY")
    frames["5min"].iloc[-1, frames["5min"].columns.get_loc("high")] += 1.0
    if action == "zero_floor":
        settings.chart_rebuild_min_interval_seconds = 0.0
    elif action == "settings":
        settings.s1_min_score += 5.0
    elif action == "new_bar":
        frames["5min"] = pd.concat([frames["5min"], frames["5min"].iloc[[-1]].set_axis(
            [frames["5min"].index[-1] + pd.Timedelta(minutes=5)])])
    host._build_chart_from_cache("NIFTY", force=action == "force")
    assert host.builds == 2


def test_inactive_instrument_does_not_wait_for_selected_chart_lock(chart_host):
    host, _, _, _ = chart_host
    host._chart_build_lock = threading.RLock()
    executor = ThreadPoolExecutor(max_workers=1)
    fast = False
    host._chart_build_lock.acquire()
    try:
        future = executor.submit(host._build_chart_from_cache, "BANKNIFTY")
        try:
            assert future.result(timeout=0.2) == {}
            fast = True
        except TimeoutError:
            pass
    finally:
        host._chart_build_lock.release()
        executor.shutdown(wait=True)
    assert fast, "An unselected instrument must not queue behind display-only calculation"
