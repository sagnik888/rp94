"""A forming bar's tick must not force a full 300-candle chart rebuild.

The chart cache fingerprint was one flat tuple that included the last row's close and
volume. Those move on every tick, so while the market was open the cache could not hit:
in the 2026-09-16 session NIFTY 5min rebuilt 3,011 times -- 3.0 rebuilds per scan cycle --
each one re-serialising 300 candles for a payload that was structurally identical.

The fingerprint is now split. A STRUCTURAL change (new candle, new signal, a trade opening
or closing, an engine switch) rebuilds immediately and is never delayed. A VOLATILE change
(the forming bar ticking) is served from cache until `chart_rebuild_min_interval_seconds`
has elapsed, so the chart still tracks price without paying a rebuild per tick.
"""
import time

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from engine.scanner_chart_mixin import ScannerChartMixin


@pytest.fixture(autouse=True)
def _generic_chart_profile(monkeypatch):
    # These fixtures exercise the generic tick floor. UTS correctly treats a
    # correction to these historical, closed bars as an immediate rebuild.
    from config.settings import get_settings

    monkeypatch.setenv("SCANNER_ENGINE", "utbot")
    monkeypatch.setattr(get_settings(), "scanner_engine", "utbot")


def _frame(bars=40, last_close=101.0, last_volume=500.0):
    idx = pd.date_range("2026-09-16 09:15", periods=bars, freq="5min")
    close = np.linspace(100.0, 101.0, bars)
    close[-1] = last_close
    volume = np.full(bars, 400.0)
    volume[-1] = last_volume
    return pd.DataFrame(
        {"open": close - 0.1, "high": close + 0.2, "low": close - 0.2,
         "close": close, "volume": volume},
        index=idx,
    )


class _Candles:
    def __init__(self, df):
        self.df = df

    def get_candles(self, instrument, timeframe):
        return self.df


class _Trades:
    open_trades = {}


class _IntelSnapshots:
    def history(self, instrument, tf, times=None, limit=None):
        return []


class _Host(ScannerChartMixin):
    """Minimal host carrying the REAL mixin, so the code under test is the shipped one."""

    mode = "REAL"
    simulation_id = "sim"
    chart_stream_enabled = True
    active_chart_instrument = "NIFTY"
    active_chart_tf = "5min"

    def __init__(self, df):
        self.candles = _Candles(df)
        self.trades = _Trades()
        self._cached_results = {}
        self._chart_metadata_cache = {}
        self.intel_snapshots = _IntelSnapshots()
        self.builds = 0

    def _chart_display_frame(self, df):
        return df

    def _hydrate_chart_frame_from_disk(self, instrument, tf, df):
        return df

    def _chart_result_for_display(self, instrument, tf, df, result):
        self.builds += 1          # counted here: reached only on a real rebuild
        return result or {}

    def _chart_result_with_adaptive_dual_lanes(self, instrument, tf, df, result):
        return result or {}


@pytest.fixture
def build():
    return ScannerChartMixin._build_chart_from_cache


def _run(host, build):
    return build(host, "NIFTY")


def test_an_unchanged_frame_is_served_from_cache(build):
    host = _Host(_frame())
    _run(host, build)
    assert host.builds == 1
    _run(host, build)
    assert host.builds == 1, "identical fingerprint must not rebuild"


def test_a_forming_bar_tick_is_absorbed_by_the_floor(build, monkeypatch):
    settings = Settings()
    settings.chart_rebuild_min_interval_seconds = 60.0
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)

    host = _Host(_frame(last_close=101.0))
    _run(host, build)
    assert host.builds == 1

    for tick in (101.1, 101.2, 101.3, 101.4):
        host.candles.df = _frame(last_close=tick, last_volume=500.0 + tick)
        _run(host, build)
    assert host.builds == 1, "ticks inside the floor must reuse the cached payload"


def test_a_new_candle_rebuilds_immediately_despite_the_floor(build, monkeypatch):
    settings = Settings()
    settings.chart_rebuild_min_interval_seconds = 3600.0
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)

    host = _Host(_frame(bars=40))
    _run(host, build)
    assert host.builds == 1

    host.candles.df = _frame(bars=41)          # structural: one more bar
    _run(host, build)
    assert host.builds == 2, "a new candle must never wait for the floor"


def test_a_new_trade_rebuilds_immediately_despite_the_floor(build, monkeypatch):
    settings = Settings()
    settings.chart_rebuild_min_interval_seconds = 3600.0
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)

    host = _Host(_frame())
    _run(host, build)
    assert host.builds == 1

    host.trades.open_trades = {"t1": object()}
    _run(host, build)
    assert host.builds == 2, "an opened trade changes the chart's shape"


def test_the_tick_is_rebuilt_once_the_floor_lapses(build, monkeypatch):
    settings = Settings()
    settings.chart_rebuild_min_interval_seconds = 2.0
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)

    host = _Host(_frame(last_close=101.0))
    _run(host, build)
    cache = getattr(host, "chart_cache_NIFTY_5min")
    cache["built_at"] = time.time() - 30.0          # floor has long lapsed

    host.candles.df = _frame(last_close=101.9)
    _run(host, build)
    assert host.builds == 2, "the chart must not freeze once the floor elapses"


def test_a_zero_floor_restores_rebuild_on_every_tick(build, monkeypatch):
    settings = Settings()
    settings.chart_rebuild_min_interval_seconds = 0.0
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)

    host = _Host(_frame(last_close=101.0))
    _run(host, build)
    host.candles.df = _frame(last_close=101.5)
    _run(host, build)
    assert host.builds == 2, "0 must disable the floor, not enable it permanently"


def test_force_always_rebuilds(build, monkeypatch):
    settings = Settings()
    settings.chart_rebuild_min_interval_seconds = 3600.0
    monkeypatch.setattr("engine.scanner_chart_mixin.get_settings", lambda: settings)

    host = _Host(_frame())
    _run(host, build)
    build(host, "NIFTY", force=True)
    assert host.builds == 2


def test_the_floor_ships_at_two_seconds():
    settings = Settings()
    assert settings.chart_rebuild_min_interval_seconds == pytest.approx(2.0)


def test_the_history_ttl_matches_the_bar_period():
    """45s against a once-a-minute bar cost ~1.15 Fyers history fetches per index
    per minute, all serialized behind the single SDK flight."""
    settings = Settings()
    assert settings.live_1min_history_cache_ttl_seconds == pytest.approx(60.0)
    # The entry-gate freshness window is max(180.0, ttl * 3.0) and must not move.
    assert max(180.0, settings.live_1min_history_cache_ttl_seconds * 3.0) == 180.0
