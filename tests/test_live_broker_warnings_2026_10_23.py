"""Successful backup recovery, honest quantity units and sanitized stream failures."""
import threading
import time
from types import SimpleNamespace

from loguru import logger
import pandas as pd
import pytest

from config.settings import get_settings
from data.angelone_market_data import AngelOneIndexQuoteSource
from data.market_data import MarketDataProvider
from tests.test_kite_angel_replacement_2026_10_23 import chain_provider
from tests.test_three_broker_priority_2026_10_23 import native_fyers_chain
from tests.test_volume_health_still_names_the_fallback_2026_08_14 import _Provider, _scanner


def backup_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "intelligence_cache_ttl_seconds", 90.0)
    source = chain_provider(tmp_path)
    source._market_data_route = "angel_fyers"
    calls = native_fyers_chain(source, monkeypatch)
    source.kite.get_option_chain = lambda *args, **kwargs: pd.DataFrame()
    source.angel_index.get_option_pairs = lambda *args, **kwargs: []
    return source, calls


def test_successful_fyers_backup_clears_preferred_provider_error(tmp_path, monkeypatch):
    source, calls = backup_provider(tmp_path, monkeypatch)
    assert not source.get_option_chain("NIFTY").empty
    assert calls == ["fyers"]
    assert source._option_chain_meta["NIFTY"]["auto_certified"]
    assert "opt_chain_NIFTY" not in source._last_opt_chain_error


def test_backup_refreshes_at_normal_ttl_instead_of_failure_cooldown(tmp_path, monkeypatch):
    source, calls = backup_provider(tmp_path, monkeypatch)
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    source.get_option_chain("NIFTY")
    clock[0] += 91
    source.get_option_chain("NIFTY")
    assert calls == ["fyers", "fyers"]
    assert source._option_chain_meta["NIFTY"]["fetched_at"] == clock[0]


def test_real_total_failure_retains_error_backoff(tmp_path, monkeypatch):
    source, calls = backup_provider(tmp_path, monkeypatch)
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    original = source._call_fyers_bounded
    def failed(*args, **kwargs):
        original(*args, **kwargs)
        return False, None
    source._call_fyers_bounded = failed
    assert source.get_option_chain("NIFTY").empty
    clock[0] += 91
    assert source.get_option_chain("NIFTY").empty
    assert calls == ["fyers"]
    clock[0] += 90
    assert source.get_option_chain("NIFTY").empty
    assert calls == ["fyers", "fyers"]


@pytest.mark.parametrize("unit,label", [(None, "broker-reported units"),
    ("broker_reported_quantity", "broker-reported units"), ("contracts", "contracts")])
def test_volume_warning_names_only_the_measured_unit(unit, label):
    source = MarketDataProvider.__new__(MarketDataProvider)
    events = []
    sink = logger.add(lambda message: events.append(message.record["message"]))
    try:
        source._warn_volume_fallback("NIFTY", dict(volume=100, oi=200, tradingsymbol="NIFTY26OCTFUT", unit=unit))
    finally:
        logger.remove(sink)
    assert f"100 {label}" in events[0]
    if unit != "contracts":
        assert "contracts" not in events[0]
    assert "live cumulative cash volume unavailable" in events[0]
    assert "independently verified cash bars" in events[0]


@pytest.mark.parametrize("unit", [None, "broker_reported_quantity", "contracts"])
def test_cached_volume_health_preserves_quantity_unit(unit):
    source = _Provider(fallback=100)
    if unit is not None:
        source._index_futures_fallback["NIFTY"]["unit"] = unit
    row = _scanner(source).volume_source_health(probe_broker=False)["instruments"]["NIFTY"]
    expected = unit or "broker_reported_quantity"
    assert row["unit"] == row["volume_unit"] == expected
    assert row["comparable_to_thresholds"] is False


@pytest.mark.parametrize("unit", ["broker_reported_quantity", "contracts"])
def test_refresh_worker_retains_fallback_unit(unit):
    source = MarketDataProvider.__new__(MarketDataProvider)
    source._index_futures_fallback = {}
    source._index_volume_refresh_guard = threading.Lock()
    source._index_volume_refresh_inflight = {"NIFTY"}
    source._participation_store = lambda: SimpleNamespace(flush=lambda: None)
    source.get_index_activity = lambda name: dict(source="kite_index_futures", volume=100,
        oi=200, unit=unit, volume_unit=unit)
    source._refresh_index_volume_worker("NIFTY")
    assert source._index_futures_fallback["NIFTY"]["unit"] == unit
    assert "NIFTY" not in source._index_volume_refresh_inflight


class StopAfterRetry:
    def __init__(self): self.stopped = False
    def clear(self): self.stopped = False
    def is_set(self): return self.stopped
    def set(self): self.stopped = True
    def wait(self, timeout):
        self.stopped = True
        return True


@pytest.mark.parametrize("reason", ["authentication_rejected", "credentials_not_configured", "connection_failed"])
def test_stream_retry_retains_sanitized_authentication_cause(reason):
    source = AngelOneIndexQuoteSource()
    source._stream_stop = StopAfterRetry()
    def connect():
        source.last_error = reason
        return False
    source.connect = connect
    assert source.start_stream([dict(exchange_type=1, token="index", exchange="NSE", symbol="NIFTY")], lambda *args: None)
    source._stream_thread.join(1)
    assert not source._stream_thread.is_alive()
    assert source.health()["last_error"] == reason


def test_stream_reports_missing_session_tokens_without_raw_exception():
    source = AngelOneIndexQuoteSource()
    source._stream_stop = StopAfterRetry()
    source.connect = lambda: True
    source.start_stream([dict(exchange_type=1, token="index", exchange="NSE", symbol="NIFTY")], lambda *args: None)
    source._stream_thread.join(1)
    assert source.health()["last_error"] == "stream_session_tokens_missing"


def test_raw_sdk_exception_remains_sanitized_in_stream_health():
    source = AngelOneIndexQuoteSource()
    source._stream_stop = StopAfterRetry()
    source.connect = lambda: True
    source._jwt_token, source._feed_token = "dummy-jwt", "dummy-feed"
    def failed_socket():
        raise RuntimeError("AUDIT_PRIVATE_HEADER_SENTINEL")
    source._load_stream_sdk = failed_socket
    source.start_stream([dict(exchange_type=1, token="index", exchange="NSE", symbol="NIFTY")], lambda *args: None)
    source._stream_thread.join(1)
    assert source.health()["last_error"] == "stream_disconnected"
    assert "AUDIT_PRIVATE_HEADER_SENTINEL" not in str(source.health())
