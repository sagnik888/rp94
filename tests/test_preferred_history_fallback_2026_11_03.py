"""Routine live history must not manufacture preferred-provider failures."""
import asyncio
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from scanner import Scanner
from tests.test_kite_angel_replacement_2026_10_23 import provider
from tests.test_preferred_history_priority_2026_10_30 import history


def cached_source(tmp_path):
    source = provider(tmp_path, route="angel_fyers")
    source._replacement_stop.set()
    target = pd.Timestamp("2026-10-23 00:00")
    source._replacement_history[("NIFTY", 1, target.isoformat(), False)] = (time.time(), history())
    source.is_market_open = lambda: True
    source.fyers = object()
    return source, target


@pytest.mark.parametrize("interval,minutes", [("1min", 1), ("5min", 5), ("15min", 15)])
def test_verified_history_avoids_backup_at_every_timeframe(tmp_path, interval, minutes):
    source, target = cached_source(tmp_path)
    calls = []
    source._call_fyers_bounded = lambda *a, **k: calls.append(k) or (False, None)
    result = source.get_historical_candles("99926000", "NSE", interval,
        days_back=1, instrument_name="NIFTY", to_date=target.to_pydatetime(), persist_cache=False)
    assert calls == []
    assert source._preferred_history_complete(result, interval, 1, target)
    assert result.loc[:"2026-10-22 15:00", "volume"].eq(123. * minutes).all()
    assert result.loc[:"2026-10-22 15:00", "volume_members_observed"].eq(50).all()


def test_routine_force_schedule_uses_verified_preferred_result(tmp_path):
    source, target = cached_source(tmp_path)
    calls = []
    source._call_fyers_bounded = lambda *a, **k: calls.append(k) or (False, None)
    scanner = Scanner.__new__(Scanner)
    scanner.mode, scanner._last_data_fetch, scanner._data_fetch_interval = "REAL", {"NIFTY_1min": time.time()}, 60
    results = []

    async def fetch(name, config, interval, **kwargs):
        results.append(source.get_historical_candles("99926000", "NSE", interval,
            days_back=1, instrument_name=name, to_date=target.to_pydatetime(), persist_cache=False,
            **{key: value for key, value in kwargs.items() if key == "force_refresh"}))

    scanner._fetch_one = fetch
    asyncio.run(scanner._fetch_all_data({"NIFTY": {}}, force=True, timeframes=["1min"]))
    assert calls == []
    assert len(results) == 1 and len(results[0]) == 375


@pytest.mark.parametrize("column,value", [("volume_members_observed", 49),
    ("volume_complete", False), ("membership_version", ""), ("volume", -1.)])
@pytest.mark.parametrize("interval", ["5min", "15min"])
def test_bad_minute_cannot_gain_certificate_through_resampling(tmp_path, interval, column, value):
    source, target = cached_source(tmp_path)
    base = source._replacement_history[("NIFTY", 1, target.isoformat(), False)][1]
    base.loc[base.index[2], column] = value
    result = source._request_replacement_history("NIFTY", interval, 1, target, persist_cache=False)
    assert not result.iloc[0]["volume_complete"]
    assert pd.isna(result.iloc[0]["volume"])
    assert not source._preferred_history_complete(result, interval, 1, target)


@pytest.mark.parametrize("column,value", [("membership_version", "other-version"),
    ("volume_provider", "kite"), ("volume_members_expected", 49)])
def test_mixed_certificates_do_not_become_single_verified_bar(tmp_path, column, value):
    source, target = cached_source(tmp_path)
    base = source._replacement_history[("NIFTY", 1, target.isoformat(), False)][1]
    base.loc[base.index[2], column] = value
    result = source._request_replacement_history("NIFTY", "5min", 1, target, persist_cache=False)
    assert not result.iloc[0]["volume_complete"]


def test_explicit_provider_force_still_refuses_cached_result(tmp_path):
    source, target = cached_source(tmp_path)
    result = source._request_replacement_history("NIFTY", "1min", 1, target,
        persist_cache=False, force_refresh=True)
    assert result.empty


@pytest.mark.parametrize("minutes_missing,accepted", [(1, True), (2, True), (3, False), (45, False)])
def test_background_grace_is_bounded(tmp_path, minutes_missing, accepted):
    source, _ = cached_source(tmp_path)
    old = history().loc["2026-10-22 10:00":]
    current = history().iloc[:45 - minutes_missing].copy()
    current.index = pd.date_range("2026-10-23 09:15", periods=len(current), freq="min")
    assert source._preferred_history_complete(pd.concat([old, current]), "1min", 1,
        "2026-10-23 10:00") is accepted


@pytest.mark.parametrize("column,value", [("volume_members_observed", 49), ("volume_complete", False),
    ("membership_version", "")])
def test_invalid_certificate_does_not_erase_known_membership(tmp_path, column, value):
    source, target = cached_source(tmp_path)
    frame = history()
    frame.loc[frame.index[2], column] = value
    assert not source._preferred_history_complete(frame, "1min", 1, target)


def test_timezone_aware_certificate_keeps_local_session_alignment(tmp_path):
    source, target = cached_source(tmp_path)
    frame = history()
    frame.index = frame.index.tz_localize("Asia/Kolkata").tz_convert("UTC")
    assert source._preferred_history_complete(frame, "1min", 1, target)


@pytest.mark.parametrize("interval", ["1min", "5min", "15min"])
def test_grace_allows_same_missing_minutes_after_resampling(tmp_path, interval):
    source, _ = cached_source(tmp_path)
    target = pd.Timestamp("2026-10-23 10:01")
    old = history().loc["2026-10-22 10:01":]
    current = history().iloc[:44].copy()
    current.index = pd.date_range("2026-10-23 09:15", periods=44, freq="min")
    source._replacement_history[("NIFTY", 1, target.isoformat(), False)] = (time.time(), pd.concat([old, current]))
    result = source._request_replacement_history("NIFTY", interval, 1, target, persist_cache=False)
    assert source._preferred_history_complete(result, interval, 1, target)


def test_live_api_cache_expires_before_minute_schedule(tmp_path, monkeypatch):
    source, target = cached_source(tmp_path)
    monkeypatch.setattr("config.settings.get_settings", lambda: SimpleNamespace(
        history_cache_ttl_seconds=240., live_1min_history_cache_ttl_seconds=60.))
    kwargs = dict(days_back=1, instrument_name="NIFTY", to_date=target.to_pydatetime(), persist_cache=False)
    source.get_historical_candles("99926000", "NSE", "1min", **kwargs)
    key = next(iter(source._api_hist_cache))
    source._api_hist_cache[key] = history().assign(close=999.)
    source._last_api_hist_fetch[key] = time.time() - 50.
    result = source.get_historical_candles("99926000", "NSE", "1min", **kwargs)
    assert result.close.eq(100.).all()
