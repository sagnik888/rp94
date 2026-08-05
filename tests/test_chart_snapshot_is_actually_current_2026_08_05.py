"""A chart stamped "now" must not be showing bars from before the last tab switch.

_build_chart_from_cache rebuilds ONLY the active instrument+timeframe, so every other
chart's cache freezes the moment the operator switches away from it. get_chart_snapshot's
currency test asked two questions — does a payload EXIST, and does simulation_id match —
and never whether the data was still true. It then returned
`"timestamp": datetime.now(IST).isoformat()`.

So: open NIFTY, switch to BANKNIFTY, switch back, and the NIFTY chart renders the bars it
held before the switch, presented as current. Measured on a 5min frame that had grown from
30 to 48 bars: the snapshot served a last close of 24029.5 while the candle store held
24047.5 — ninety minutes of price action missing, timestamped now.

The scan loop refreshes the active chart on its next cycle, so this usually self-heals
within one cadence. It does not heal when the loop is stalled or the instrument has
dropped out of active_indices — exactly the moments an operator is staring at the chart
trying to work out what is wrong.
"""
from types import SimpleNamespace

import pandas as pd
import pytest

from scanner import Scanner


class _DummyCandles:
    def __init__(self, df):
        self.df = df

    def get_candles(self, instrument, timeframe):
        return self.df.copy()


class _DummyUtEngine:
    def __init__(self):
        self.calls = 0

    def process(self, df, instrument, timeframe, capital, risk_pct, lots, lot_size):
        self.calls += 1
        stops = [float(v) - 10.0 for v in df["close"].tolist()]
        return {
            "signals": [],
            "trailing_stop": stops,
            "trailing_stop_colors": ["green"] * len(stops),
            "state": {"position": 1, "position_label": "LONG", "trailing_stop": stops[-1]},
        }


def _frame(n, start="2026-06-12 09:15"):
    idx = pd.date_range(start, periods=n, freq="5min")
    return pd.DataFrame(
        {
            "open": [24000.0 + i for i in range(n)],
            "high": [24001.0 + i for i in range(n)],
            "low": [23999.0 + i for i in range(n)],
            "close": [24000.5 + i for i in range(n)],
            "volume": [700000] * n,
        },
        index=idx,
    )


def _scanner(df):
    s = Scanner.__new__(Scanner)
    s.mode = "REAL"
    s.chart_stream_enabled = True
    s.active_chart_instrument = "NIFTY"
    s.active_chart_tf = "5min"
    s._chart_metadata_cache = {}
    s._cached_results = {}
    s.candles = _DummyCandles(df)
    s.trades = SimpleNamespace(open_trades={})
    s.mtf = SimpleNamespace(
        engines={
            f"{i}_{t}": _DummyUtEngine()
            for i in ("NIFTY", "BANKNIFTY")
            for t in ("1min", "5min", "15min")
        },
        _result_cache={},
    )
    s.config = {"indices": {"NIFTY": {"lot_size": 75}, "BANKNIFTY": {"lot_size": 35}}}
    s.lots = {"NIFTY": 1, "BANKNIFTY": 1}
    s.capital_total = 100000.0
    s.risk_fut_pct = 1.0
    s.intel_snapshots = SimpleNamespace(history=lambda *a, **k: {})
    s.simulation_id = 1
    return s


def _last_close(snapshot, tf="5min"):
    return snapshot["chart"][tf]["candles"][-1]["close"]


def test_switching_away_and_back_does_not_serve_a_frozen_chart():
    """The live defect, end to end."""
    s = _scanner(_frame(30))
    first = _last_close(s.get_chart_snapshot("NIFTY", "5min"))

    s.candles = _DummyCandles(_frame(48))       # 90 more minutes of tape
    s.get_chart_snapshot("BANKNIFTY", "5min")   # operator switches tabs
    again = s.get_chart_snapshot("NIFTY", "5min")

    assert _last_close(again) != first, (
        "served the pre-switch payload, stamped with the current time"
    )
    assert _last_close(again) == pytest.approx(float(_frame(48)["close"].iloc[-1]))
    assert len(again["chart"]["5min"]["candles"]) == 48


def _count_rebuilds(scanner):
    """Count forced rebuilds; engine.calls is not a proxy — _cached_results short-circuits it."""
    original = scanner._build_chart_from_cache
    seen = []

    def spy(instrument, force=False):
        seen.append(force)
        return original(instrument, force=force)

    scanner._build_chart_from_cache = spy
    return seen


def test_an_unchanged_chart_still_serves_from_cache():
    """The cache has to keep earning its place; rebuilding every request is waste."""
    s = _scanner(_frame(30))
    s.get_chart_snapshot("NIFTY", "5min")
    rebuilds = _count_rebuilds(s)
    for _ in range(5):
        s.get_chart_snapshot("NIFTY", "5min")
    assert rebuilds == [], "rebuilt an unchanged chart on every request"


def test_a_single_new_bar_invalidates_the_cache():
    s = _scanner(_frame(30))
    s.get_chart_snapshot("NIFTY", "5min")
    s.candles = _DummyCandles(_frame(31))
    assert len(s.get_chart_snapshot("NIFTY", "5min")["chart"]["5min"]["candles"]) == 31


def test_a_new_simulation_id_still_invalidates_the_cache():
    """The original currency test must keep working."""
    s = _scanner(_frame(30))
    s.get_chart_snapshot("NIFTY", "5min")
    rebuilds = _count_rebuilds(s)
    s.simulation_id = 2
    s.get_chart_snapshot("NIFTY", "5min")
    assert rebuilds == [True]


def test_each_timeframe_tracks_its_own_currency():
    s = _scanner(_frame(30))
    s.get_chart_snapshot("NIFTY", "5min")
    s.get_chart_snapshot("NIFTY", "15min")
    s.candles = _DummyCandles(_frame(48))
    assert len(s.get_chart_snapshot("NIFTY", "5min")["chart"]["5min"]["candles"]) == 48


# ── the currency test itself ─────────────────────────────────────────────────

def _cached(times):
    return {"data": {"candles": [{"time": t} for t in times]}, "simulation_id": 1}


def test_a_cache_ending_on_the_newest_bar_is_current():
    s = _scanner(_frame(30))
    newest = s.candles.get_candles("NIFTY", "5min").index[-1]
    epoch = int(pd.Timestamp(newest).tz_localize("Asia/Kolkata").timestamp())
    assert s._chart_cache_matches_candles(_cached([epoch]), "NIFTY", "5min") is True


def test_a_cache_ending_on_an_older_bar_is_not_current():
    s = _scanner(_frame(30))
    newest = s.candles.get_candles("NIFTY", "5min").index[-1]
    epoch = int(pd.Timestamp(newest).tz_localize("Asia/Kolkata").timestamp())
    assert s._chart_cache_matches_candles(_cached([epoch - 300]), "NIFTY", "5min") is False


def test_an_empty_cached_payload_is_not_current():
    s = _scanner(_frame(30))
    assert s._chart_cache_matches_candles(_cached([]), "NIFTY", "5min") is False


def test_no_candle_store_leaves_the_cache_alone():
    """Nothing to compare against is not evidence of staleness."""
    s = _scanner(_frame(30))
    s.candles = None
    assert s._chart_cache_matches_candles(_cached([1]), "NIFTY", "5min") is True


def test_an_instrument_with_no_candles_leaves_the_cache_alone():
    s = _scanner(_frame(30))
    s.candles = SimpleNamespace(get_candles=lambda i, t: None)
    assert s._chart_cache_matches_candles(_cached([1]), "NIFTY", "5min") is True


def test_an_uncomparable_timestamp_rebuilds_rather_than_trusting_the_cache():
    s = _scanner(_frame(30))
    payload = {"data": {"candles": [{"time": "not-an-epoch"}]}, "simulation_id": 1}
    assert s._chart_cache_matches_candles(payload, "NIFTY", "5min") is False


# ── the cheap accessor this leans on ─────────────────────────────────────────

def test_latest_candle_time_reads_without_copying_the_frame():
    from data.candle_builder import CandleBuilder

    builder = CandleBuilder.__new__(CandleBuilder)
    import threading

    builder._lock = threading.RLock()
    df = _frame(30)
    builder._candles_1min = {}
    builder._candles_5min = {"NIFTY": df}
    builder._candles_15min = {}

    assert builder.latest_candle_time("NIFTY", "5min") == df.index[-1]
    assert builder.latest_candle_time("NIFTY", "1min") is None
    assert builder.latest_candle_time("BANKNIFTY", "5min") is None
    assert builder.latest_candle_time("NIFTY", "3min") is None


def test_latest_candle_time_handles_an_empty_frame():
    from data.candle_builder import CandleBuilder
    import threading

    builder = CandleBuilder.__new__(CandleBuilder)
    builder._lock = threading.RLock()
    builder._candles_1min = {}
    builder._candles_5min = {"NIFTY": _frame(30).iloc[0:0]}
    builder._candles_15min = {}
    assert builder.latest_candle_time("NIFTY", "5min") is None
