"""S1+S2 chart snapshots must refresh on lane-result changes and carry 15m aux bars."""
from types import SimpleNamespace

import pandas as pd
import pytest

from config.settings import Settings
from engine.scanner_chart_mixin import ScannerChartMixin


def _frame(timeframe, bars=20):
    index = pd.date_range("2026-10-09 09:15", periods=bars, freq=timeframe)
    return pd.DataFrame(
        {"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 500.0},
        index=index,
    )


class _Candles:
    def __init__(self, frames):
        self.frames = frames

    def get_candles(self, instrument, timeframe):
        return self.frames.get((instrument, timeframe))


def _signal(frame, bar, event_id):
    return SimpleNamespace(
        timestamp=frame.index[bar], signal_type="BUY", price=101.0, stop_distance=0.1,
        source_engine="s1s2_dual", source_lane="D1", s12_lane="D1",
        s12_event_id=event_id, s12_exit_reason="", s12_exit_time=None,
    )


def _chart_host(instrument, timeframe, frame, result, *, engine=None, lower=None):
    host = ScannerChartMixin()
    host.mode = "REAL"
    host.chart_stream_enabled = True
    host.active_chart_instrument = instrument
    host.active_chart_tf = timeframe
    host.simulation_id = 1
    host._cached_results = {f"{instrument}_{timeframe}": result}
    host._chart_metadata_cache = {}
    frames = {(instrument, timeframe): frame}
    if lower is not None:
        frames[(instrument, "5min")] = lower
    host.candles = _Candles(frames)
    host.trades = SimpleNamespace(open_trades={})
    host.intel_snapshots = SimpleNamespace(history=lambda *args, **kwargs: [])
    host.config = {"indices": {name: {"lot_size": 1} for name in
                                 ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")}}
    host.lots = {}
    host.mtf = SimpleNamespace(
        engines={f"{instrument}_{timeframe}": engine} if engine else {},
        _result_cache={},
        _data_cache={f"{instrument}_5min": lower} if lower is not None else {},
    )
    return host


@pytest.mark.parametrize("instrument,timeframe", [
    ("NIFTY", "1min"), ("BANKNIFTY", "1min"), ("SENSEX", "1min"), ("MIDCPNIFTY", "1min"),
    ("NIFTY", "5min"), ("BANKNIFTY", "5min"), ("SENSEX", "5min"), ("MIDCPNIFTY", "5min"),
    ("BANKNIFTY", "15min"), ("SENSEX", "15min"), ("MIDCPNIFTY", "15min"),
])
def test_s12_chart_refreshes_same_candle_when_lane_result_changes(monkeypatch, instrument, timeframe):
    settings = Settings(_env_file=None)
    settings.scanner_engine = "s1s2_dual"
    settings.chart_rebuild_min_interval_seconds = 3600.0
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)
    frame = _frame(timeframe)
    first = _signal(frame, 3, "event-1")
    result = {
        "source_engine": "s1s2_dual", "signals": [first], "state": {"position": 0},
        "trailing_stop": [None] * len(frame),
        "s12_evaluation": {"signature": "snapshot-1", "config_hash": "cfg", "current": True},
        "s12_markers": [],
    }
    host = _chart_host(instrument, timeframe, frame, result)

    before = host._build_chart_from_cache(instrument)[timeframe]
    assert [marker["text"] for marker in before["markers"]] == ["BUY D1 @ 101"]

    # The candles do not advance, but a new worker result contains another lane event.
    result["signals"].append(_signal(frame, 5, "event-2"))
    result["s12_evaluation"]["signature"] = "snapshot-2"
    after = host.get_chart_snapshot(instrument, timeframe)["chart"][timeframe]
    assert [marker["text"] for marker in after["markers"]] == ["BUY D1 @ 101", "BUY D1 @ 101"]


def test_s12_15min_chart_fallback_supplies_closed_5min_aux_feed(monkeypatch):
    settings = Settings(_env_file=None)
    settings.scanner_engine = "s1s2_dual"
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)
    frame, lower = _frame("15min"), _frame("5min", bars=60)

    class _Engine:
        signal_source = "s1s2_dual"

        def __init__(self):
            self.kwargs = None

        def process(self, **kwargs):
            self.kwargs = kwargs
            return {"source_engine": "s1s2_dual", "state": {"position": 0}, "trailing_stop": []}

    engine = _Engine()
    host = _chart_host("SENSEX", "15min", frame, {}, engine=engine, lower=lower)
    displayed = host._chart_result_for_display("SENSEX", "15min", frame, {})

    assert displayed["source_engine"] == "s1s2_dual"
    pd.testing.assert_frame_equal(engine.kwargs["df_5min"], lower)
    assert engine.kwargs["instrument"] == "SENSEX"
    assert engine.kwargs["timeframe"] == "15min"
