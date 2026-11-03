from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from engine.historical_data import HistoricalDataGateway
from scanner import Scanner


class _NetworkBomb:
    def __getattr__(self, name):
        raise AssertionError(f"historical path touched live provider attribute {name}")


class _Candles:
    def __init__(self):
        self.frames = {}

    def update_candles(self, instrument, frame, timeframe, mark_receipt=False):
        self.frames[(instrument, timeframe)] = frame


def _frame():
    return pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5], "volume": [10.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-09-01 09:15")]),
    )


def test_historical_fetch_reads_local_cache_without_touching_provider(tmp_path):
    cache = tmp_path / "candles"
    cache.mkdir()
    _frame().to_csv(cache / "NIFTY_5min.csv")
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner.data = _NetworkBomb()
    scanner.candles = _Candles()
    scanner._historical_data_gateway = HistoricalDataGateway(cache)
    scanner._last_data_fetch = {}
    scanner._historical_offline_missing = []
    scanner._set_candle_history_capacity = lambda **_kwargs: None

    asyncio.run(
        Scanner._fetch_all_data(
            scanner,
            {"NIFTY": {"token": "must-not-be-read", "exchange": "NSE"}},
            force=True,
            timeframes=["5min"],
        )
    )

    assert ("NIFTY", "5min") in scanner.candles.frames
    assert scanner._historical_offline_missing == []


def test_historical_missing_cache_is_explicit_abstention(tmp_path):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner.data = _NetworkBomb()
    scanner.candles = _Candles()
    scanner._historical_data_gateway = HistoricalDataGateway(tmp_path / "missing")
    scanner._last_data_fetch = {}
    scanner._set_candle_history_capacity = lambda **_kwargs: None

    asyncio.run(
        Scanner._fetch_all_data(
            scanner,
            {"NIFTY": {}},
            force=True,
            timeframes=["1min"],
        )
    )

    assert scanner._historical_offline_missing == ["candle:NIFTY:1min"]
    assert scanner.candles.frames == {}


def test_historical_chain_and_depth_never_touch_live_provider():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner.data = _NetworkBomb()

    chain, quality = Scanner._get_option_chain_cached(scanner, "NIFTY", {})
    depth = Scanner._get_depth_context(scanner, "NIFTY", {})

    assert chain.empty
    assert quality["historical_abstention"] is True
    assert quality["intelligence_usable"] is False
    assert depth["historical_abstention"] is True
    assert depth["usable"] is False


def test_historical_timeframe_intelligence_uses_bar_aligned_cache_only():
    frame = _frame()
    calls = []
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner.candles = SimpleNamespace(get_candles=lambda *_args: frame)
    scanner._get_historical_intel_context_at = (
        lambda instrument, timestamp, timeframe: calls.append(
            (instrument, timestamp, timeframe)
        )
        or {"as_of": str(timestamp), "timeframe": timeframe}
    )

    result = Scanner._get_timeframe_intel(scanner, "NIFTY", "5min")

    assert result["timeframe"] == "5min"
    assert calls == [("NIFTY", frame.index[-1], "5min")]
