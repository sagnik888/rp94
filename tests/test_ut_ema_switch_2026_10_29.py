"""Held UT/EMA exits keep their observed tick state across scanner selection."""
from types import SimpleNamespace

import pandas as pd
import pytest

from engine.multi_timeframe import MultiTimeframeEngine


@pytest.fixture
def settings(monkeypatch):
    value = SimpleNamespace(scanner_engine="ut_ema", trading_mode="REAL")
    monkeypatch.setattr("config.settings.get_settings", lambda: value)
    return value


def _data():
    return pd.DataFrame({"open": [1000.] * 31, "high": [1000.2] * 31,
        "low": [999.8] * 31, "close": [1000.] * 31},
        index=pd.date_range("2026-10-29 09:15", periods=31, freq="5min"))


def test_switch_to_native_waits_for_stream_instead_of_consuming_snapshot(settings):
    host=MultiTimeframeEngine()
    host._utema_stream_service_installed=True
    host.set_scanner_engine("ema")
    host.set_scanner_engine("ut_ema")
    adapter=host.engines["NIFTY_5min"]
    assert adapter._utema_stream_owned
    result=adapter.process(_data(),"NIFTY","5min",as_of="2026-10-29 12:00:01",realtime=True)
    assert result["utema_entry_eligible"] is False
    assert adapter._native._timeline is None


def _held_engine():
    host = MultiTimeframeEngine()
    data = _data()
    now = data.index[-1] + pd.Timedelta(seconds=1)
    adapter = host.engines["NIFTY_5min"]
    adapter.process(data, "NIFTY", "5min", as_of=now, realtime=True)
    adapter._native._timeline.enter("EMA", 1, 1000.)
    result = adapter.process(data, "NIFTY", "5min", as_of=now + pd.Timedelta(seconds=1), realtime=True)
    host._result_cache["NIFTY_5min"] = result
    identity = next(iter(result["utema_records"]))
    return host, adapter, data, now, identity


def test_switch_preserves_original_tick_position_and_first_native_exit(settings):
    host, adapter, data, now, identity = _held_engine()
    timeline = adapter._native._timeline
    host.set_scanner_engine("ema")
    assert host.engines["NIFTY_5min"] is not adapter
    assert host._retired_utema_engines["NIFTY_5min"] is adapter
    assert host._retired_utema_results["NIFTY_5min"]["utema_records"][identity]["exit_time"] == ""
    generic_cache = {"source_engine": "ema", "sentinel": object()}
    host._result_cache["NIFTY_5min"] = generic_cache
    data.loc[data.index[-1], ["low", "close"]] = [997., 997.]
    observed = host.get_utema_owner_result("NIFTY", "5min", data, as_of=now + pd.Timedelta(seconds=3))
    assert adapter._native._timeline is timeline
    assert observed["utema_records"][identity]["exit_reason"] == "EMA STOP"
    assert observed["utema_records"][identity]["exit_price"] == 997.
    assert host._result_cache["NIFTY_5min"] is generic_cache
    assert settings.scanner_engine == "ema"
    assert host._retired_utema_results["NIFTY_5min"]["utema_records"][identity]["exit_reason"] == "EMA STOP"


def test_active_owner_read_copies_existing_native_result_without_processing(settings, monkeypatch):
    host, adapter, data, now, identity = _held_engine()
    def forbidden(*args, **kwargs):
        raise AssertionError("Active owner lookup must reuse the current native observation")
    monkeypatch.setattr(adapter, "process", forbidden)
    actual = host.get_utema_owner_result("NIFTY", "5min", data, as_of=now)
    actual["utema_records"][identity]["exit_reason"] = "mutation"
    assert host._result_cache["NIFTY_5min"]["utema_records"][identity]["exit_reason"] == ""


def test_switch_back_reuses_adapter_then_correction_resets_its_native_prefix(settings):
    host, adapter, data, now, identity = _held_engine()
    timeline = adapter._native._timeline
    host.set_scanner_engine("ema")
    host.set_scanner_engine("ut_ema")
    assert host.engines["NIFTY_5min"] is adapter
    unchanged = host.get_utema_owner_result("NIFTY", "5min", data, as_of=now + pd.Timedelta(seconds=3))
    assert adapter._native._timeline is timeline
    assert unchanged["utema_records"][identity]["exit_time"] == ""
    data.loc[data.index[20], ["open", "high", "low", "close"]] = [998., 998.2, 997.8, 998.]
    host.get_utema_owner_result("NIFTY", "5min", data, as_of=now + pd.Timedelta(seconds=4))
    assert adapter._native._timeline is not timeline


def test_missing_original_adapter_returns_explicit_historical_replay_only(settings):
    host = MultiTimeframeEngine()
    host.set_scanner_engine("ema")
    host._retired_utema_engines.clear()
    data = _data()
    result = host.get_utema_owner_result("NIFTY", "5min", data,
        as_of=data.index[-1] + pd.Timedelta(seconds=1))
    assert result["utema_replay_fallback"] is True
    assert result["utema_live_path_available"] is False
    assert result["state"]["bar_count"] == len(data) - 1
    assert "NIFTY_5min" in host._retired_utema_engines
    assert host._retired_utema_engines["NIFTY_5min"]._native._timeline is None
    assert "NIFTY_5min" not in host._result_cache


def test_retired_owner_obeys_historical_setting_and_missing_input(settings):
    host, adapter, data, now, identity = _held_engine()
    host.set_scanner_engine("ema")
    assert host.get_utema_owner_result("NIFTY", "5min", None, as_of=now) is None
    settings.trading_mode = "HISTORICAL"
    result = host.get_utema_owner_result("NIFTY", "5min", data, as_of=now)
    assert result["state"]["bar_count"] == len(data) - 1
    assert adapter._native._timeline.basket["lifecycle_id"] == identity


def test_unobserved_adapter_cannot_claim_a_restored_trades_live_tick_path(settings):
    host = MultiTimeframeEngine()
    unobserved = host.engines["NIFTY_5min"]
    host.set_scanner_engine("ema")
    data = _data()
    result = host.get_utema_owner_result("NIFTY", "5min", data,
        as_of=data.index[-1] + pd.Timedelta(seconds=1))
    assert result["utema_replay_fallback"] is True
    assert result["utema_live_path_available"] is False
    assert unobserved._native._timeline is None
