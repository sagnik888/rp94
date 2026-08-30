"""Live 1m entries use live candle evidence, never a fresh quote or stale REST cache."""

import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd

import config.settings as settings_module
import scanner as scanner_module
from data.market_data import MarketDataProvider
from scanner import IST, Scanner


def _freshness_settings():
    return SimpleNamespace(
        one_min_entry_max_bar_age_seconds=90.0,
        one_min_entry_max_receipt_age_seconds=30.0,
        one_min_entry_max_provider_lag_seconds=90.0,
    )


def _one_min_frame(at: datetime, **extra):
    row = {
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 0.0,
        "tick_count": 12,
        "volume_basis": "TICK_ACTIVITY",
        "source": "live_tick",
        "provider_time": at,
        "receipt_at": at,
    }
    row.update(extra)
    return pd.DataFrame([row], index=pd.DatetimeIndex([at]))


def test_live_one_min_freshness_reads_provider_time(monkeypatch):
    now = datetime.now(IST).replace(tzinfo=None)
    bar_at = now.replace(second=0, microsecond=0)
    scanner = Scanner.__new__(Scanner)
    scanner.candles = SimpleNamespace(
        latest_candle_receipt=lambda instrument, timeframe: now
    )
    monkeypatch.setattr(scanner_module, "get_settings", _freshness_settings)

    status = scanner._one_min_entry_freshness("NIFTY", _one_min_frame(bar_at))
    assert status["fresh"] is True
    assert status["reason"] == "FRESH_LIVE_1MIN"
    assert status["provider_at"] == bar_at.isoformat()
    assert status["tick_count_available"] is True


def test_stale_history_and_stale_provider_have_distinct_reasons(monkeypatch):
    now = datetime.now(IST).replace(tzinfo=None)
    scanner = Scanner.__new__(Scanner)
    monkeypatch.setattr(scanner_module, "get_settings", _freshness_settings)

    scanner.candles = SimpleNamespace(
        latest_candle_receipt=lambda instrument, timeframe: None
    )
    old_at = now.replace(second=0, microsecond=0) - timedelta(minutes=4)
    history = _one_min_frame(
        old_at,
        source="fyers_history",
        tick_count=0,
        provider_time=old_at,
    )
    stale = scanner._one_min_entry_freshness("NIFTY", history)
    assert stale["fresh"] is False
    assert stale["reason"] == "STALE_BACKFILL"
    assert stale["backfill_age_seconds"] >= 180.0

    current_at = now.replace(second=0, microsecond=0)
    scanner.candles = SimpleNamespace(
        latest_candle_receipt=lambda instrument, timeframe: now
    )
    provider_stale = _one_min_frame(
        current_at,
        provider_time=now - timedelta(minutes=5),
        receipt_at=now,
    )
    status = scanner._one_min_entry_freshness("NIFTY", provider_stale)
    assert status["fresh"] is False
    assert status["reason"] == "STALE_PROVIDER_BAR"


def test_live_one_min_missing_source_or_provider_timestamp_fails_closed(monkeypatch):
    now = datetime.now(IST).replace(tzinfo=None)
    bar_at = now.replace(second=0, microsecond=0)
    scanner = Scanner.__new__(Scanner)
    scanner.candles = SimpleNamespace(
        latest_candle_receipt=lambda instrument, timeframe: now
    )
    monkeypatch.setattr(scanner_module, "get_settings", _freshness_settings)

    no_source = scanner._one_min_entry_freshness(
        "NIFTY", _one_min_frame(bar_at, source="")
    )
    assert no_source["fresh"] is False
    assert no_source["reason"] == "CANDLE_SOURCE_UNAVAILABLE"

    no_provider = scanner._one_min_entry_freshness(
        "NIFTY", _one_min_frame(bar_at, provider_time=None)
    )
    assert no_provider["fresh"] is False
    assert no_provider["reason"] == "PROVIDER_TIMESTAMP_UNAVAILABLE"


def test_future_provider_timestamp_fails_closed(monkeypatch):
    now = datetime.now(IST).replace(tzinfo=None)
    bar_at = now.replace(second=0, microsecond=0)
    scanner = Scanner.__new__(Scanner)
    scanner.candles = SimpleNamespace(
        latest_candle_receipt=lambda instrument, timeframe: now
    )
    monkeypatch.setattr(scanner_module, "get_settings", _freshness_settings)

    status = scanner._one_min_entry_freshness(
        "NIFTY",
        _one_min_frame(
            bar_at,
            provider_time=now + timedelta(seconds=5),
            receipt_at=now,
        ),
    )
    assert status["fresh"] is False
    assert status["reason"] == "FUTURE_PROVIDER_TIMESTAMP"


def test_live_one_min_history_cache_uses_short_ttl(monkeypatch):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    key = "hist_api_NIFTY_NSE_123_1min_7_latest"
    cached = pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]},
        index=pd.DatetimeIndex([datetime(2026, 8, 29, 9, 15)]),
    )
    provider._api_hist_cache = {key: cached}
    provider._last_api_hist_fetch = {key: time.time() - 60.0}

    class Fyers:
        calls = 0

        def history(self, data):
            self.calls += 1
            stamps = [
                int(datetime(2026, 8, 29, 9, minute).timestamp())
                for minute in (15, 16)
            ]
            return {
                "s": "ok",
                "candles": [
                    [stamps[0], 100, 101, 99, 100, 500],
                    [stamps[1], 100, 102, 99, 101, 600],
                ],
            }

    provider.fyers = Fyers()
    provider.is_connected = False
    provider.smart_api = None
    provider._yahoo_request_count = 0
    provider._save_and_merge_candles = lambda frame, path: frame
    market_states = iter((True, False))
    provider.is_market_open = lambda: next(market_states, False)
    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(
            history_cache_ttl_seconds=240.0,
            live_1min_history_cache_ttl_seconds=45.0,
        ),
    )

    result = provider.get_historical_candles(
        123, "NSE", "1min", days_back=7, instrument_name="NIFTY"
    )
    assert provider.fyers.calls == 1
    assert float(result.iloc[-1]["close"]) == 101.0
    assert {"provider_time", "receipt_at", "source", "volume_basis", "tick_count"} <= set(
        result.columns
    )


def test_fresh_live_one_min_cache_is_reused(monkeypatch):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    key = "hist_api_NIFTY_NSE_123_1min_7_latest"
    cached = pd.DataFrame({"close": [123.0]})
    provider._api_hist_cache = {key: cached}
    provider._last_api_hist_fetch = {key: time.time() - 30.0}
    provider.is_market_open = lambda: True
    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(
            history_cache_ttl_seconds=240.0,
            live_1min_history_cache_ttl_seconds=45.0,
        ),
    )

    result = provider.get_historical_candles(
        123, "NSE", "1min", days_back=7, instrument_name="NIFTY"
    )
    assert result is cached


def test_scanner_history_fetch_does_not_advance_live_receipt(monkeypatch):
    scanner = Scanner.__new__(Scanner)
    scanner.backtest_days = 2
    scanner._api_semaphore = asyncio.Semaphore(1)

    class Limiter:
        async def consume(self, amount):
            return None

    class Data:
        def get_historical_candles(self, *args, **kwargs):
            return _one_min_frame(datetime(2026, 8, 29, 9, 15))

    class Candles:
        call = None

        def update_candles(self, *args, **kwargs):
            self.call = (args, kwargs)

    async def no_sleep(seconds):
        return None

    scanner._history_rate_limiter = Limiter()
    scanner.data = Data()
    scanner.candles = Candles()
    monkeypatch.setattr(scanner_module.asyncio, "sleep", no_sleep)

    asyncio.run(
        scanner._fetch_one(
            "NIFTY", {"token": 123, "exchange": "NSE"}, "1min"
        )
    )
    assert scanner.candles.call is not None
    assert scanner.candles.call[1]["mark_receipt"] is False


def test_early_exit_requires_the_trade_timeframes_own_receipt(monkeypatch):
    now = datetime.now(IST).replace(tzinfo=None)
    frame = _one_min_frame(now.replace(second=0, microsecond=0))

    class Candles:
        def get_candles(self, instrument, timeframe):
            assert timeframe == "15min"
            return frame

        def latest_candle_receipt(self, instrument, timeframe):
            assert timeframe == "15min"
            return now - timedelta(minutes=5)

    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.candles = Candles()
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(
            early_exit_candle_max_age_seconds_1min=90.0,
            early_exit_candle_max_age_seconds_5min=90.0,
            early_exit_candle_max_age_seconds_15min=90.0,
            early_exit_timeframe_alignment_seconds=90.0,
        ),
    )

    status = scanner._early_exit_candle_freshness("NIFTY", "15min", now)
    assert status["fresh"] is False
    assert "receipt age" in status["reason"]


def test_early_exit_accepts_fresh_exact_timeframe_candle(monkeypatch):
    now = datetime.now(IST).replace(tzinfo=None)
    frame = _one_min_frame(now.replace(second=0, microsecond=0))
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.candles = SimpleNamespace(
        get_candles=lambda instrument, timeframe: frame,
        latest_candle_receipt=lambda instrument, timeframe: now,
    )
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(
            early_exit_candle_max_age_seconds_1min=90.0,
            early_exit_candle_max_age_seconds_5min=90.0,
            early_exit_candle_max_age_seconds_15min=90.0,
            early_exit_timeframe_alignment_seconds=90.0,
        ),
    )

    status = scanner._early_exit_candle_freshness("NIFTY", "1min", now)
    assert status["fresh"] is True
    assert status["reason"] == "fresh exact-timeframe candle"
