"""Offline regression fixtures for requested history, separate from live tick caps."""

import asyncio
from types import SimpleNamespace

import pandas as pd
import pytest

from data.candle_builder import CandleBuilder
from data.market_data import MarketDataProvider
from engine.recalculation_worker import _fetch_days
from scanner import Scanner


def _sessions_frame(sessions=30):
    index = pd.DatetimeIndex([
        timestamp
        for day in pd.bdate_range("2026-06-01", periods=sessions)
        for timestamp in pd.date_range(day + pd.Timedelta(hours=9, minutes=15), periods=375, freq="min")
    ], name="timestamp")
    return pd.DataFrame(
        {"open": 25000.0, "high": 25002.0, "low": 24998.0,
         "close": 25001.0, "volume": 100.0}, index=index,
    )


def _scanner(mode="HISTORICAL", days=30):
    scanner = Scanner.__new__(Scanner)
    scanner.candles = CandleBuilder(max_candles=2000)
    scanner.mode = mode
    scanner.backtest_days = days
    return scanner


def _manifest(tmp_path, frame):
    path = tmp_path / "NIFTY_1min.csv"
    frame.to_csv(path)
    return {"files": [{"path": str(path), "instrument": "NIFTY", "timeframe": "1min"}]}


@pytest.mark.parametrize("rows", [5000, 11250])
def test_recalculation_retains_requested_history_and_reports_retained_rows(tmp_path, rows):
    scanner = _scanner()
    frame = _sessions_frame().iloc[-rows:]
    loaded = scanner._load_worker_candles(_manifest(tmp_path, frame))
    retained = scanner.candles.get_candles("NIFTY", "1min")
    assert len(retained) == rows
    assert loaded == rows
    pd.testing.assert_frame_equal(retained, frame, check_freq=False)
    assert scanner.candles.latest_candle_receipt("NIFTY", "1min") is None


def test_recalculation_count_excludes_duplicate_rows(tmp_path):
    scanner = _scanner()
    frame = _sessions_frame(1).iloc[:100]
    with_duplicates = pd.concat([frame, frame.iloc[-2:]])
    loaded = scanner._load_worker_candles(_manifest(tmp_path, with_duplicates))
    assert loaded == len(scanner.candles.get_candles("NIFTY", "1min")) == 100


def test_real_recalculation_keeps_live_memory_bound_and_reports_actual_rows(tmp_path):
    scanner = _scanner("REAL")
    loaded = scanner._load_worker_candles(_manifest(tmp_path, _sessions_frame().iloc[-5000:]))
    assert scanner.candles.max_candles == 2000
    assert loaded == scanner.candles.get_candle_count("NIFTY", "1min") == 2000


@pytest.mark.parametrize("timeframe", ["1min", "5min", "15min"])
def test_thirty_session_request_covers_weekends_within_broker_cap(timeframe):
    assert 45 <= _fetch_days(30, timeframe) <= 60


def test_next_disk_merge_preserves_thirty_full_sessions(tmp_path):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    frame = _sessions_frame()
    cache_file = tmp_path / "NIFTY_1min.csv"
    frame.to_csv(cache_file)
    correction = frame.iloc[-1:].copy()
    correction["close"] += 0.5
    merged = provider._save_and_merge_candles(correction, cache_file)
    disk = pd.read_csv(cache_file, index_col=0, parse_dates=True)
    assert len(merged) == len(disk) == 30 * 375
    assert disk.index[0] == frame.index[0]
    assert disk.iloc[-1]["close"] == correction.iloc[-1]["close"]


def test_fallback_fetch_uses_same_horizon_and_retention(monkeypatch):
    scanner = _scanner()
    frame = _sessions_frame()
    requests = []

    def fetch(*args, **kwargs):
        requests.append(kwargs)
        return frame

    async def noop(*args):
        pass

    scanner.data = SimpleNamespace(get_historical_candles=fetch)
    scanner._api_semaphore = asyncio.Semaphore(1)
    scanner._history_rate_limiter = SimpleNamespace(consume=noop)
    monkeypatch.setattr("scanner.asyncio.sleep", noop)
    asyncio.run(scanner._fetch_one("NIFTY", {"token": 123, "exchange": "NSE"}, "1min"))
    assert requests[0]["days_back"] == _fetch_days(30, "1min")
    assert requests[0]["days_back"] >= 45
    assert scanner.candles.get_candle_count("NIFTY", "1min") == len(frame)


def test_restoring_real_mode_cap_bounds_the_next_tick(tmp_path):
    scanner = _scanner()
    frame = _sessions_frame().iloc[-5000:]
    scanner._load_worker_candles(_manifest(tmp_path, frame))
    scanner.mode = "REAL"
    scanner._set_candle_history_capacity()
    assert scanner.candles.max_candles == 2000
    scanner.candles.update_candles("NIFTY", frame.iloc[-1:], "1min")
    assert scanner.candles.get_candle_count("NIFTY", "1min") == 2000


@pytest.mark.parametrize("step,expected", [(1, 60 * 375), (5, 5000)])
def test_disk_archive_is_bounded_with_a_floor_for_coarser_candles(tmp_path, step, expected):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    frame = _sessions_frame(80).iloc[::step]
    path = tmp_path / f"NIFTY_{step}min.csv"
    frame.to_csv(path)
    correction = frame.iloc[-1:].copy()
    correction["close"] += 0.5
    merged = provider._save_and_merge_candles(correction, path)
    assert len(merged) == expected
    assert merged.index[0] == frame.index[-expected]
    assert len(pd.read_csv(path)) == expected


def test_yahoo_fallback_uses_its_own_limit_for_broker_sized_request(monkeypatch):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.fyers = None
    provider.is_connected = False
    provider.smart_api = None
    provider._yahoo_request_count = 0
    provider.is_market_open = lambda: False
    requests = []
    frame = _sessions_frame(1)

    def history(**kwargs):
        requests.append(kwargs)
        return frame

    monkeypatch.setattr("data.market_data.yf.Ticker", lambda symbol: SimpleNamespace(history=history))
    result = provider.get_historical_candles(
        123, "NSE", "1min", days_back=_fetch_days(30, "1min"),
        instrument_name="NIFTY", force_refresh=True, persist_cache=False,
    )
    assert len(result) == len(frame)
    assert requests == [{"period": "7d", "interval": "1m"}]


def test_kite_adapter_receives_supported_sixty_day_minute_range():
    from data.kite_market_data import KiteDataSource

    requests = []

    def historical_data(token, start, end, interval, oi=False):
        requests.append((token, start, end, interval))
        return []

    source = KiteDataSource(kite=SimpleNamespace(historical_data=historical_data))
    source.get_historical(123, "1min", days_back=_fetch_days(30, "1min"))
    assert len(requests) == 1
    token, start, end, interval = requests[0]
    assert token == 123 and interval == "minute"
    assert (end - start).days == 60
