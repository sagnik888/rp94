from types import SimpleNamespace

import pandas as pd

from config.settings import get_settings
from engine.ut_bot_core import UTBotSignal
from scanner import Scanner


class _DummyCandles:
    def __init__(self, df):
        self.df = df

    def get_candles(self, instrument, timeframe):
        return self.df.copy()

    def update_candles(self, instrument, df, timeframe):
        self.df = df.copy()


class _DummyUtEngine:
    def __init__(self):
        self.calls = 0
        self._states = {}

    def process(self, df, instrument, timeframe, capital, risk_pct, lots, lot_size):
        self.calls += 1
        stops = [float(v) - 10.0 for v in df["close"].tolist()]
        return {
            "signals": [],
            "trailing_stop": stops,
            "trailing_stop_colors": ["green"] * len(stops),
            "state": {
                "position": 1,
                "position_label": "LONG",
                "trailing_stop": stops[-1],
            },
        }


def _chart_signal(ts, signal_type="BUY", price=100.0, source_engine="", source_lane=""):
    return UTBotSignal(
        timestamp=ts,
        signal_type=signal_type,
        price=float(price),
        trailing_stop=float(price) - 10.0 if signal_type == "BUY" else float(price) + 10.0,
        stop_distance=10.0,
        atr_value=8.0,
        adx_value=30.0,
        position_state=1 if signal_type == "BUY" else -1,
        bar_index=1,
        instrument="NIFTY",
        timeframe="5min",
        source_engine=source_engine,
        source_lane=source_lane,
    )


def test_chart_snapshot_rebuilds_ut1_state_and_filters_premarket_zero_volume_candles():
    session_index = pd.date_range("2026-06-12 09:15", periods=20, freq="5min")
    premarket_index = pd.date_range("2026-06-15 08:00", periods=5, freq="5min")
    session = pd.DataFrame(
        {
            "open": [100.0 + i for i in range(len(session_index))],
            "high": [101.0 + i for i in range(len(session_index))],
            "low": [99.0 + i for i in range(len(session_index))],
            "close": [100.5 + i for i in range(len(session_index))],
            "volume": [1000] * len(session_index),
        },
        index=session_index,
    )
    premarket = pd.DataFrame(
        {
            "open": [150.0] * len(premarket_index),
            "high": [151.0] * len(premarket_index),
            "low": [149.0] * len(premarket_index),
            "close": [150.5] * len(premarket_index),
            "volume": [0] * len(premarket_index),
        },
        index=premarket_index,
    )
    df = pd.concat([session, premarket])
    engine = _DummyUtEngine()

    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.chart_stream_enabled = True
    scanner.active_chart_instrument = "NIFTY"
    scanner.active_chart_tf = "5min"
    scanner._chart_metadata_cache = {}
    scanner._cached_results = {}
    scanner.candles = _DummyCandles(df)
    scanner.trades = SimpleNamespace(open_trades={})
    scanner.mtf = SimpleNamespace(engines={"NIFTY_5min": engine}, _result_cache={})
    scanner.config = {"indices": {"NIFTY": {"lot_size": 50}}}
    scanner.lots = {"NIFTY": 1}
    scanner.capital_total = 100000.0
    scanner.risk_fut_pct = 1.0
    scanner.intel_snapshots = SimpleNamespace(history=lambda *args, **kwargs: {})
    scanner.simulation_id = 123

    chart = scanner._build_chart_from_cache("NIFTY", force=True)["5min"]

    assert engine.calls == 1
    assert len(chart["candles"]) == len(session)
    assert len(chart["trailing_stop"]) == len(session)
    assert chart["state"]["position_label"] == "LONG"
    assert all(
        pd.Timestamp.fromtimestamp(candle["time"], tz="Asia/Kolkata").time()
        >= pd.Timestamp("2026-06-12 09:15").time()
        for candle in chart["candles"]
    )


def test_chart_snapshot_hydrates_empty_memory_from_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    candle_dir = tmp_path / "data_store" / "candles"
    candle_dir.mkdir(parents=True)
    disk_df = _ohlcv_frame(pd.date_range("2026-06-17 09:15", periods=20, freq="5min"))
    disk_df.to_csv(candle_dir / "NIFTY_5min.csv", index_label="timestamp")

    engine = _DummyUtEngine()
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner.chart_stream_enabled = True
    scanner.active_chart_instrument = "NIFTY"
    scanner.active_chart_tf = "5min"
    scanner._chart_metadata_cache = {}
    scanner._cached_results = {}
    scanner.candles = _DummyCandles(pd.DataFrame())
    scanner.trades = SimpleNamespace(open_trades={})
    scanner.mtf = SimpleNamespace(engines={"NIFTY_5min": engine}, _result_cache={})
    scanner.config = {"indices": {"NIFTY": {"lot_size": 50}}}
    scanner.lots = {"NIFTY": 1}
    scanner.capital_total = 100000.0
    scanner.risk_fut_pct = 1.0
    scanner.intel_snapshots = SimpleNamespace(history=lambda *args, **kwargs: {})
    scanner.simulation_id = 456

    snapshot = scanner.get_chart_snapshot("NIFTY", "5min")
    chart = snapshot["chart"]["5min"]

    assert engine.calls == 1
    assert len(chart["candles"]) == len(disk_df)
    assert len(scanner.candles.df) == len(disk_df)
    assert chart["state"]["position_label"] == "LONG"


def _ohlcv_frame(index, start=100.0):
    return pd.DataFrame(
        {
            "open": [start + i for i in range(len(index))],
            "high": [start + i + 1.0 for i in range(len(index))],
            "low": [start + i - 1.0 for i in range(len(index))],
            "close": [start + i + 0.5 for i in range(len(index))],
            "volume": [1000] * len(index),
        },
        index=index,
    )


def test_adaptive_dual_chart_marks_lane_a_and_apex_lane_b(monkeypatch):
    index = pd.date_range("2026-07-01 09:35", periods=220, freq="5min")
    df = _ohlcv_frame(index, start=24000.0)
    lane_a = _chart_signal(index[-3], "BUY", float(df["close"].iloc[-3]), "adaptive_dual", "A")
    lane_b = _chart_signal(index[-2], "SELL", float(df["close"].iloc[-2]), "apex_hybrid", "B")

    class _DualEngine(_DummyUtEngine):
        def process(self, df, instrument, timeframe, capital, risk_pct, lots, lot_size):
            result = super().process(df, instrument, timeframe, capital, risk_pct, lots, lot_size)
            result["signals"] = [lane_a]
            return result

    import engine.apex_hybrid as apex_hybrid

    monkeypatch.setattr(apex_hybrid, "build_apex_signals", lambda *args, **kwargs: [lane_b])

    settings = get_settings()
    previous_engine = settings.scanner_engine
    previous_dual = getattr(settings, "dual_lane_enabled", True)
    settings.scanner_engine = "adaptive_dual"
    settings.dual_lane_enabled = True
    try:
        scanner = Scanner.__new__(Scanner)
        scanner.mode = "HISTORICAL"
        scanner.chart_stream_enabled = True
        scanner.active_chart_instrument = "NIFTY"
        scanner.active_chart_tf = "5min"
        scanner._chart_metadata_cache = {}
        scanner._cached_results = {}
        scanner.candles = _DummyCandles(df)
        scanner.trades = SimpleNamespace(open_trades={})
        scanner.mtf = SimpleNamespace(engines={"NIFTY_5min": _DualEngine()}, _result_cache={})
        scanner.config = {"indices": {"NIFTY": {"lot_size": 50}}}
        scanner.lots = {"NIFTY": 1}
        scanner.capital_total = 100000.0
        scanner.risk_fut_pct = 1.0
        scanner.intel_snapshots = SimpleNamespace(history=lambda *args, **kwargs: {})
        scanner.simulation_id = 789

        chart = scanner._build_chart_from_cache("NIFTY", force=True)["5min"]
    finally:
        settings.scanner_engine = previous_engine
        settings.dual_lane_enabled = previous_dual

    marker_text = [str(marker.get("text") or "") for marker in chart["markers"]]
    assert any("L1 AH" in text for text in marker_text)
    assert any("L2 APEX" in text for text in marker_text)
    assert chart["adaptive_dual_chart"]["lane_A_signals"] == 1
    assert chart["adaptive_dual_chart"]["lane_B_signals"] == 1


def test_live_signal_window_waits_for_full_5min_warmup_and_clears_bad_state():
    previous = _ohlcv_frame(pd.date_range("2026-06-16 14:10", periods=10, freq="5min"))
    today = _ohlcv_frame(pd.date_range("2026-06-17 09:15", periods=5, freq="5min"), start=200.0)
    underwarmed = pd.concat([previous, today])
    engine = _DummyUtEngine()
    engine._states["NIFTY_5min"] = SimpleNamespace(last_entry_price=24020.7)

    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._get_current_session_day = lambda: "2026-06-17"
    scanner._cached_results = {"NIFTY_5min": {"state": {"last_entry_price": 24020.7}}}
    scanner._candidate_process_cache = {"NIFTY": {"candidates": ["bad"]}}
    scanner._last_signal_time = {"NIFTY_5min": pd.Timestamp("2026-06-17 09:25")}
    scanner._last_signal_identity = {"NIFTY_5min": "bad"}
    scanner._live_signal_warmup_ready = {}
    scanner.mtf = SimpleNamespace(
        _result_cache={"NIFTY_5min": {"signals": ["bad"]}},
        _data_hash={"NIFTY_5min": 1},
        _data_cache={"NIFTY_5min": underwarmed},
        engines={"NIFTY_5min": engine},
    )
    scanner.log_signal_decision_once = lambda *args, **kwargs: None

    assert scanner._prepare_live_signal_data("NIFTY", "5min", underwarmed) is None

    assert scanner._live_signal_warmup_ready["NIFTY_5min"] is False
    assert "NIFTY_5min" not in scanner._cached_results
    assert "NIFTY" not in scanner._candidate_process_cache
    assert "NIFTY_5min" not in scanner.mtf._result_cache
    assert "NIFTY_5min" not in scanner.mtf._data_hash
    assert "NIFTY_5min" not in scanner.mtf._data_cache
    assert "NIFTY_5min" not in engine._states
    assert "NIFTY_5min" not in scanner._last_signal_time
    assert "NIFTY_5min" not in scanner._last_signal_identity


def test_chart_fallback_does_not_run_utbot_when_live_5min_warmup_is_short():
    previous = _ohlcv_frame(pd.date_range("2026-06-16 14:10", periods=10, freq="5min"))
    today = _ohlcv_frame(pd.date_range("2026-06-17 09:15", periods=5, freq="5min"), start=200.0)
    df = pd.concat([previous, today])
    engine = _DummyUtEngine()

    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._get_current_session_day = lambda: "2026-06-17"
    scanner._cached_results = {}
    scanner.mtf = SimpleNamespace(engines={"NIFTY_5min": engine}, _result_cache={})
    scanner.config = {"indices": {"NIFTY": {"lot_size": 50}}}
    scanner.lots = {"NIFTY": 1}
    scanner.capital_total = 100000.0
    scanner.risk_fut_pct = 1.0

    result = scanner._chart_result_for_display("NIFTY", "5min", df, {})

    assert result == {}
    assert engine.calls == 0
