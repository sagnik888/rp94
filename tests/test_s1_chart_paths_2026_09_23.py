"""S1's chart and cached 15m replay must use the same inputs as the live lane."""
from types import SimpleNamespace
from pathlib import Path
import shutil
import subprocess
import ast

import pandas as pd
import pytest

from config.settings import Settings
from engine.multi_timeframe import MultiTimeframeEngine
from engine.scanner_chart_mixin import ScannerChartMixin


def test_incremental_broadcast_preserves_s1_overlays_and_engine():
    source = Path(__file__).resolve().parents[1] / "scanner.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                              and t.value.id == "stripped_chart" for t in n.targets))
    overlay = [{"name": "S1 VWAP", "data": [{"time": 1, "value": 100}]}]
    payload = eval(compile(ast.Expression(assignment.value), str(source), "eval"),
                   {"v": {"engine": "s1", "candle_type": "heikin_ashi", "indicator_overlays": overlay}})
    assert payload["engine"] == "s1"
    assert payload["candle_type"] == "heikin_ashi"
    assert payload["indicator_overlays"] == overlay


def _frame(tf="5min", bars=30):
    index = pd.date_range("2026-09-23 09:15", periods=bars, freq=tf)
    return pd.DataFrame({
        "open": 100.0, "high": 102.0, "low": 99.0,
        "close": 101.0, "volume": 500.0,
    }, index=index)


class _Engine:
    signal_source = "s1"
    signal_mode = "confirmed"

    def __init__(self):
        self.calls = []

    def process(self, **kwargs):
        self.calls.append(kwargs)
        return {"source_engine": "s1", "candle_type": "heikin_ashi", "signals": [], "state": {"position": 0},
                "trailing_stop": [None] * len(kwargs["df"])}


class _Candles:
    def __init__(self, frames):
        self.frames = frames

    def get_candles(self, instrument, timeframe):
        return self.frames.get(timeframe)

    def update_candles(self, instrument, frame, timeframe, **kwargs):
        self.frames[timeframe] = frame


def _mtf(engine, timeframe="15min"):
    host = MultiTimeframeEngine.__new__(MultiTimeframeEngine)
    host.engines = {f"NIFTY_{timeframe}": engine}
    host._data_cache = {"NIFTY_15min": _frame("15min"), "NIFTY_5min": _frame()}
    host._result_cache = {}
    host._data_hash = {}
    host.tf_weights = {"1min": 0.15, "5min": 0.4, "15min": 0.45}
    return host


def _chart_host(engine=None, tf="15min", lower=None):
    host = ScannerChartMixin()
    host.mode = "HISTORICAL"
    host.chart_stream_enabled = True
    host.active_chart_instrument = "NIFTY"
    host.active_chart_tf = tf
    host.simulation_id = 1
    host._cached_results = {}
    host._chart_metadata_cache = {}
    host.candles = _Candles({tf: _frame(tf), **({"5min": lower} if tf != "5min" else {})})
    host.trades = SimpleNamespace(open_trades={})
    host.intel_snapshots = SimpleNamespace(history=lambda *args, **kwargs: [])
    host.mtf = SimpleNamespace(engines={f"NIFTY_{tf}": engine} if engine else {},
                               _result_cache={}, _data_cache={})
    return host


def test_s1_15m_cache_invalidates_on_5m_correction_and_missing_lower_frame():
    engine = _Engine()
    host = _mtf(engine)
    first = host.process_instrument("NIFTY")
    assert first.timeframe_metadata["15min"]["status"] == "recomputed"
    assert engine.calls[-1]["df_5min"] is host._data_cache["NIFTY_5min"]
    assert host.process_instrument("NIFTY").timeframe_metadata["15min"]["status"] == "cached"

    # Own 15m candles are unchanged; a revised earlier 5m close changes RSI.
    host._data_cache["NIFTY_5min"].iloc[-3, 3] += 10.0
    assert host.process_instrument("NIFTY").timeframe_metadata["15min"]["status"] == "recomputed"
    host._data_cache.pop("NIFTY_5min")
    assert host.process_instrument("NIFTY").timeframe_metadata["15min"]["status"] == "recomputed"
    assert engine.calls[-1]["df_5min"] is None
    assert len(engine.calls) == 3


@pytest.mark.parametrize("tf", ["5min", "15min"])
def test_s1_cache_invalidates_when_confluence_setting_changes(monkeypatch, tf):
    settings = Settings(_env_file=None)
    monkeypatch.setattr("config.settings.get_settings", lambda: settings)
    engine = _Engine()
    host = _mtf(engine, tf)
    host.process_instrument("NIFTY")
    assert host.process_instrument("NIFTY").timeframe_metadata[tf]["status"] == "cached"
    settings.s1_min_score += 5
    assert host.process_instrument("NIFTY").timeframe_metadata[tf]["status"] == "recomputed"
    assert len(engine.calls) == 2


def test_s1_5m_cache_depends_on_native_15m_candles_and_candle_type(monkeypatch):
    settings = Settings(_env_file=None)
    monkeypatch.setattr("config.settings.get_settings", lambda: settings)
    engine = _Engine()
    host = _mtf(engine, "5min")
    host.process_instrument("NIFTY")
    assert engine.calls[-1]["df_15min"] is host._data_cache["NIFTY_15min"]
    host._data_cache["NIFTY_15min"].iloc[-3, 0] += 1.0
    assert host.process_instrument("NIFTY").timeframe_metadata["5min"]["status"] == "recomputed"
    settings.s1_candle_type = "standard"
    assert host.process_instrument("NIFTY").timeframe_metadata["5min"]["status"] == "recomputed"


def test_s1_chart_uses_same_candle_type_without_modifying_store(monkeypatch):
    from engine.s1_engine import s1_candle_frame
    settings = Settings(_env_file=None)
    settings.scanner_engine = "s1"
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)
    host = _chart_host(tf="5min")
    raw = host.candles.frames["5min"].copy()
    host._cached_results["NIFTY_5min"] = {"source_engine": "s1", "candle_type": "heikin_ashi",
                                       "state": {"position": 0}, "trailing_stop": [None] * len(raw)}
    chart = host._build_chart_from_cache("NIFTY")["5min"]
    expected = s1_candle_frame(raw, "heikin_ashi")
    assert chart["candle_type"] == "heikin_ashi"
    assert [p["close"] for p in chart["candles"]] == expected.close.round(2).tolist()
    pd.testing.assert_frame_equal(host.candles.frames["5min"], raw)
    settings.s1_candle_type = "standard"
    chart = host._build_chart_from_cache("NIFTY")["5min"]
    assert chart["candle_type"] == "standard"
    assert [p["close"] for p in chart["candles"]] == raw.close.round(2).tolist()


@pytest.mark.parametrize("source", ["mtf", "candles", "disk"])
def test_s1_15m_chart_rebuild_receives_real_5m_frame(monkeypatch, tmp_path, source):
    # Pinned to S1: the chart only accepts results from the SELECTED engine, and this test
    # read the operator's .env (SCANNER_ENGINE='ut_smart' since 2026-09-26).
    settings = Settings(_env_file=None)
    settings.scanner_engine = "s1"
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)
    monkeypatch.chdir(tmp_path)
    lower = _frame()
    engine = _Engine()
    host = _chart_host(engine, lower=lower if source == "candles" else None)
    if source == "mtf":
        host.mtf._data_cache["NIFTY_5min"] = lower
    elif source == "disk":
        candle_dir = tmp_path / "data_store" / "candles"
        candle_dir.mkdir(parents=True)
        lower.to_csv(candle_dir / "NIFTY_5min.csv", index_label="timestamp")
    result = host._chart_result_for_display("NIFTY", "15min", _frame("15min"), {})
    assert "state" in result
    assert len(engine.calls) == 1
    pd.testing.assert_frame_equal(engine.calls[0]["df_5min"], lower, check_freq=False, check_names=False)


def test_s1_exit_refreshes_chart_without_new_entry_or_candle(monkeypatch):
    settings = Settings(_env_file=None)
    settings.scanner_engine = "s1"
    settings.chart_rebuild_min_interval_seconds = 3600.0
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)
    host = _chart_host(tf="5min")
    frame = host.candles.frames["5min"]
    sig = SimpleNamespace(timestamp=frame.index[3], signal_type="BUY", price=101.0,
                          stop_distance=0.2, source_engine="s1", source_lane="S1",
                          s1_exit_time=None, s1_exit_reason="", s1_exit_price=None)
    host._cached_results["NIFTY_5min"] = {
        "source_engine": "s1", "candle_type": "heikin_ashi",
        "signals": [sig], "state": {"position": 1},
        "trailing_stop": [None, float("nan")] + [100.0] * (len(frame) - 2),
        "trailing_stop_colors": ["green"] * len(frame),
    }
    before = host._build_chart_from_cache("NIFTY")["5min"]
    assert [m["text"] for m in before["markers"]] == ["BUY S1 @ 101"]
    assert len(before["trailing_stop"]) == len(frame)
    assert all("value" not in p for p in before["trailing_stop"][:2])
    assert host._build_chart_from_cache("NIFTY")["5min"] is before

    sig.s1_exit_time = frame.index[7]
    sig.s1_exit_reason = "Momentum Exit"
    sig.s1_exit_price = 102.0
    after = host._build_chart_from_cache("NIFTY")["5min"]
    assert after is not before
    assert len(after["markers"]) == 2
    exit_marker = after["markers"][-1]
    assert exit_marker["text"] == "EXIT LONG S1 | Momentum Exit"
    assert exit_marker["color"] == "#ff9800"
    assert exit_marker["time"] == int(frame.index[7].tz_localize("Asia/Kolkata").timestamp())


def test_s1_short_exit_markers_are_sorted_by_exit_time(monkeypatch):
    settings = Settings(_env_file=None)
    settings.scanner_engine = "s1"
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)
    host = _chart_host(tf="5min")
    frame = host.candles.frames["5min"]
    sigs = [SimpleNamespace(timestamp=frame.index[i], signal_type="SELL", price=101.0,
                           stop_distance=0.2, source_engine="s1", source_lane="S1",
                           s1_exit_time=frame.index[j], s1_exit_reason="Hard SL1",
                           s1_exit_price=102.0) for i, j in [(2, 9), (7, 11)]]
    host._cached_results["NIFTY_5min"] = {
        "source_engine": "s1", "candle_type": "heikin_ashi",
        "signals": sigs, "state": {"position": 0},
        "trailing_stop": [None] * len(frame),
    }
    markers = host._build_chart_from_cache("NIFTY")["5min"]["markers"]
    assert len(markers) == 4
    assert [m["time"] for m in markers] == sorted(m["time"] for m in markers)
    assert all(m["position"] == "belowBar" for m in markers if m["text"].startswith("EXIT"))


@pytest.mark.parametrize("source,candle_type", [("utbot", "standard"), ("s1", "standard"), (None, None)])
def test_chart_cannot_label_old_source_as_selected_s1_when_rebuild_fails(monkeypatch, source, candle_type):
    settings = Settings(_env_file=None)
    settings.scanner_engine = "s1"
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)
    host = _chart_host(tf="5min")
    old = {"source_engine": source, "candle_type": candle_type,
           "signals": ["stale"], "state": {"position": 1}, "trailing_stop": [100.]}
    host.mtf._result_cache["NIFTY_5min"] = old
    assert host._chart_result_for_display("NIFTY", "5min", _frame(), old) == {}
    class Broken:
        signal_source = "s1"
        def process(self, **kwargs):
            raise ValueError("unavailable data")
    host.mtf.engines["NIFTY_5min"] = Broken()
    assert host._chart_result_for_display("NIFTY", "5min", _frame(), old) == {}
    host.mtf.engines["NIFTY_5min"] = SimpleNamespace(signal_source="utbot", process=lambda **kwargs: old)
    assert host._chart_result_for_display("NIFTY", "5min", _frame(), old) == {}


def test_dashboard_renders_exit_and_indicator_changes_without_a_price_tick():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the actual chart JavaScript")
    source = Path(__file__).resolve().parents[1] / "dashboard/static/js/dashboard-charts.js"
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const series = () => ({sets: [], updates: [], setData(v) { this.sets.push(v); },
    update(v) { this.updates.push(v); }, applyOptions() {}});
const markers = [];
const state = {chartStreamEnabled: true, currentTimeframe: '5min', currentInstrument: 'NIFTY',
    candleSeries: series(), volumeSeries: series(), tsLineSeries: series(),
    engineOverlaySeries: Array.from({length: 5}, series)};
state.chart = {timeScale: () => ({getVisibleRange: () => null,
    setVisibleLogicalRange() {}, setVisibleRange() {}})};
const context = vm.createContext({state, document: {getElementById: () => null},
    LightweightCharts: {createSeriesMarkers(_series, first) {
        markers.push(first); return {setMarkers(next) { markers.push(next); }};
    }}});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const chart = {engine: 's1', candles: [{time: 1, open: 100, high: 102, low: 99, close: 101, volume: 50}],
    markers: [{time: 1, text: 'BUY S1'}], trailing_stop: [],
    indicator_overlays: Array.from({length: 5}, (_, i) => ({name: `S1 ${i}`, data: [{time: 1, value: 100 + i}]}))};
const data = {simulation_id: 1, chart: {'5min': chart}};
context.updateChart(data);
assert.strictEqual(markers.length, 1);
chart.markers = [...chart.markers, {time: 1, text: 'EXIT LONG S1 | Momentum Exit'}];
context.updateChart(data);
assert.strictEqual(markers.length, 2, 'an exit must render without a candle tick');
assert.strictEqual(markers[1].length, 2);
context.updateChart(data);
assert.strictEqual(markers.length, 2, 'an unchanged payload remains cached');
chart.indicator_overlays[4].data[0].value = 105;
context.updateChart(data);
assert.strictEqual(state.engineOverlaySeries[4].sets.at(-1)[0].value, 105);
chart.trailing_stop = [{time: 1, value: 99}];
context.updateChart(data);
assert.strictEqual(state.tsLineSeries.updates.at(-1).value, 99);
chart.trailing_stop = [{time: 1}];
context.updateChart(data);
assert.strictEqual('value' in state.tsLineSeries.updates.at(-1), false, 'flat state clears the active stop');
chart.engine = 'utbot';
chart.indicator_overlays = [];
context.updateChart(data);
assert.strictEqual(state.candleSeries.sets.length, 2, 'engine switch must trigger full redraw');
assert.strictEqual(state.engineOverlaySeries[4].sets.at(-1).length, 0);
const standard = {engine: 's1', candle_type: 'standard', candles: [{time: 1}, {time: 2}], markers: [{time: 1}]};
const ha = {engine: 's1', candle_type: 'heikin_ashi', candles: [{time: 3}], markers: []};
let requested = 0;
context.requestChartSnapshot = () => requested++;
assert.strictEqual(context.mergeChartData(standard, ha).candles.length, 1, 'do not mix raw and HA histories');
assert.strictEqual(requested, 1, 'source switch requests a full snapshot');
assert.strictEqual(context.mergeChartData(standard, {...standard, candles: [{time: 3}], markers: []}).markers.length, 0,
    'a recompute with no signals clears old markers');
"""
    completed = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
