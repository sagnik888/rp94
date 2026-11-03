"""UT/EMA charts retain native events without consuming the execution engine."""
import copy
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from engine.alt_scanners import UTEmaSignalEngine
from engine.ut_ema_engine import UTEmaEngine, epoch
from tests.test_s12_chart_visibility import _chart_host


def _frame(timeframe="5min", bars=50):
    close = 20000. + 160. * np.sin(np.arange(bars) / 3.)
    opening = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": opening, "high": np.maximum(opening, close) + 20.,
        "low": np.minimum(opening, close) - 20., "close": close, "volume": np.full(bars, 500.)},
        index=pd.date_range("2026-10-28 09:15", periods=bars, freq=timeframe))


@pytest.fixture
def settings(monkeypatch):
    settings = SimpleNamespace(scanner_engine="ut_ema", dual_lane_enabled=False,
        chart_rebuild_min_interval_seconds=3600.)
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)
    return settings


def _native_snapshot(core):
    return json.dumps({"processed": core._processed, "live_bar": core._live_bar,
        "last_tick": core._last_tick, "timeline": repr(core._timeline.__dict__) if core._timeline else None,
        "last_result": repr(core._last_result)}, default=str, sort_keys=True)


@pytest.mark.parametrize("instrument,timeframe", [
    ("NIFTY", "5min"), ("NIFTY", "15min"), ("BANKNIFTY", "5min"),
    ("SENSEX", "15min"), ("MIDCPNIFTY", "5min"),
])
def test_native_markers_keep_colors_labels_and_skip_shared_arrows(settings, instrument, timeframe):
    frame = _frame(timeframe, 24 if timeframe == "15min" else 50)
    result = UTEmaEngine().process(frame, instrument, timeframe, realtime=False,
        as_of=frame.index[-1] + pd.Timedelta(minutes=15))
    assert result["native_markers"], "The fixture must produce actual Pine-style events"
    # Shared ENTRY/ADD/EXIT signals are execution messages, not native chart labels.
    # An opaque value also proves the generic signal-arrow path is not evaluated.
    result["signals"] = [object()]
    result["indicator_overlays"] = [{"name": "UT stop", "color": "#ff9900",
        "values": [None] * 3 + [19950.] * (len(frame) - 3)}]
    host = _chart_host(instrument, timeframe, frame, result)
    displayed = host._build_chart_from_cache(instrument)[timeframe]
    expected = result["native_markers"][-160:]
    assert displayed["markers"] == expected
    assert displayed["trailing_stop"] == []
    assert displayed["indicator_overlays"][0]["name"] == "UT stop"
    assert displayed["indicator_overlays"][0]["color"] == "#ff9900"
    assert all(marker["time"] >= epoch(frame.index[0]) for marker in displayed["markers"])
    assert all(marker["text"].startswith(("UT ENTRY", "UT EXIT", "EMA ENTRY", "EMA EXIT"))
               for marker in displayed["markers"])


def test_same_candle_native_exit_bypasses_the_chart_rebuild_floor(settings):
    frame = _frame()
    result = {"source_engine": "ut_ema", "signals": [], "state": {"position": 1},
        "native_markers": [], "indicator_overlays": []}
    host = _chart_host("NIFTY", "5min", frame, result)
    assert host._build_chart_from_cache("NIFTY")["5min"]["markers"] == []
    marker = {"time": epoch(frame.index[-1]), "position": "aboveBar", "color": "#ebc33c",
        "shape": "arrowDown", "text": "EMA EXIT LONG RSI-50 @ 20000.00", "price": 20000.,
        "engine": "EMA", "action": "EXIT"}
    result["native_markers"].append(marker)
    displayed = host.get_chart_snapshot("NIFTY", "5min")["chart"]["5min"]
    assert displayed["markers"] == [marker]


def test_live_chart_requests_do_not_advance_native_state(settings, monkeypatch):
    frame = _frame()
    adapter = UTEmaSignalEngine()
    adapter.process(frame, "NIFTY", "5min", realtime=True,
        as_of=frame.index[-1] + pd.Timedelta(minutes=1))
    before = _native_snapshot(adapter._native)
    def forbidden(*args, **kwargs):
        raise AssertionError("A chart must not process the live execution adapter")
    monkeypatch.setattr(adapter, "process", forbidden)
    host = _chart_host("NIFTY", "5min", frame, {}, engine=adapter)
    for _ in range(3):
        displayed = host._chart_result_for_display("NIFTY", "5min", frame, {})
        assert displayed["source_engine"] == "ut_ema"
    host._build_chart_from_cache("NIFTY", force=True)
    assert _native_snapshot(adapter._native) == before


def test_cold_chart_fallback_uses_private_replay_and_two_bar_input(settings, monkeypatch):
    frame = _frame(bars=2)
    adapter = UTEmaSignalEngine()
    before = _native_snapshot(adapter._native)
    monkeypatch.setattr(adapter, "process", lambda *a, **kw: pytest.fail("Live adapter invoked"))
    host = _chart_host("NIFTY", "5min", frame, {}, engine=adapter)
    displayed = host._chart_result_for_display("NIFTY", "5min", frame, {})
    assert displayed["source_engine"] == "ut_ema"
    assert isinstance(displayed["state"], dict)
    assert _native_snapshot(adapter._native) == before


def test_other_engine_cache_is_rejected_without_mutating_scan_results(settings):
    frame = _frame()
    stale = {"source_engine": "utbot", "signals": [], "state": {"position": -1},
        "trailing_stop": [123.] * len(frame), "native_markers": [{"text": "WRONG ENGINE"}]}
    host = _chart_host("NIFTY", "5min", frame, stale)
    host.mtf._result_cache["NIFTY_5min"] = copy.deepcopy(stale)
    original = copy.deepcopy(host._cached_results)
    displayed = host._chart_result_for_display("NIFTY", "5min", frame, stale)
    assert displayed["source_engine"] == "ut_ema"
    assert not any(marker["text"] == "WRONG ENGINE" for marker in displayed["native_markers"])
    assert host._cached_results == original
