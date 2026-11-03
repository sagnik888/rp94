"""Publisher snapshots, completed cash-volume recovery and authentication backoff."""
import asyncio
from dataclasses import replace
from datetime import date, timedelta
import json
from types import SimpleNamespace
import time
import threading

import pandas as pd
import pytest

from data.angelone_market_data import AngelOneIndexQuoteSource
from data.constituent_volume import ConstituentVolumeService
from data.index_membership import BSE_MASTER, SOURCES, ensure_current_membership
from data.kite_angel_migration import KiteAngelMigration
from scanner import Scanner
from data.market_data import MarketDataProvider
from tests.test_kite_angel_replacement_2026_10_23 import provider

DAY = date(2026, 10, 23)


def publisher_responses():
    responses = {}
    for index, (url, count) in SOURCES.items():
        if index != "SENSEX":
            responses[url] = "Company Name,Industry,Symbol,Series,ISIN Code\n" + "".join(
                f"Company{i},Sector,{index}{i},EQ,INE{i:09d}\n" for i in range(count))
        else:
            responses[url] = json.dumps(dict(Table=[dict(index_Code=16, SCRIP_CODE=str(500000+i)) for i in range(count)]))
            responses[BSE_MASTER] = "exchange_token,tradingsymbol,exchange,instrument_type\n" + "".join(
                f"{500000+i},SENSEX{i},BSE,EQ\n" for i in range(count))
    return responses


def test_current_publisher_snapshot_never_backdates_membership(tmp_path):
    path = tmp_path / "members.json"
    responses = publisher_responses()
    assert ensure_current_membership(path, day=DAY, fetch=responses.__getitem__)
    service = ConstituentVolumeService(path)
    for index, (_, count) in SOURCES.items():
        assert len(service.membership(index, DAY).members) == count
        assert service.membership(index, DAY-timedelta(days=1)) is None
        assert service.membership(index, DAY+timedelta(days=1)) is None
    def unexpected_fetch(url): raise AssertionError("valid membership downloaded again")
    assert not ensure_current_membership(path, day=DAY, fetch=unexpected_fetch)


def test_next_day_snapshot_preserves_previous_session_membership(tmp_path):
    path = tmp_path / "members.json"
    responses = publisher_responses()
    ensure_current_membership(path, day=DAY, fetch=responses.__getitem__)
    old = ConstituentVolumeService(path).membership("NIFTY", DAY)
    ensure_current_membership(path, day=DAY+timedelta(days=1), fetch=responses.__getitem__)
    service = ConstituentVolumeService(path)
    assert service.membership("NIFTY", DAY) == old
    assert service.membership("NIFTY", DAY+timedelta(days=1)).version != old.version


@pytest.mark.parametrize("variant", ["short", "duplicate", "wrong_venue", "missing_bse_identity", "network"])
def test_bad_membership_download_never_replaces_last_verified_file(tmp_path, variant):
    path = tmp_path / "members.json"
    responses = publisher_responses()
    ensure_current_membership(path, day=DAY, fetch=responses.__getitem__)
    original = path.read_bytes()
    key = SOURCES["NIFTY"][0]
    if variant == "short": responses[key] = "\n".join(responses[key].splitlines()[:-1])
    if variant == "duplicate": responses[key] = responses[key].replace("NIFTY1,EQ", "NIFTY0,EQ")
    if variant == "wrong_venue": responses[key] = responses[key].replace(",EQ,", ",BE,")
    if variant == "missing_bse_identity": responses[BSE_MASTER] = "exchange_token,tradingsymbol,exchange,instrument_type\n"
    def fetch(url):
        if variant == "network": raise TimeoutError("offline")
        return responses[url]
    with pytest.raises((ValueError, KeyError, TimeoutError)):
        ensure_current_membership(path, day=DAY+timedelta(days=1), fetch=fetch)
    assert path.read_bytes() == original


def preferred_cash(volume=10):
    return pd.DataFrame(dict(open=[100], high=[101], low=[99], close=[100], volume=[volume],
        volume_complete=[True], volume_basis=["constituent_shares"], volume_members_observed=[2],
        volume_members_expected=[2], membership_version=["verified-day"], volume_provider=["kite"],
        source=["kite_constituent_history"]), index=pd.to_datetime(["2026-10-23 10:00"]))


@pytest.mark.parametrize("volume", [0, 123])
def test_certified_constituent_volume_fills_only_missing_cash_interval(volume):
    preferred = preferred_cash(volume)
    result = KiteAngelMigration._merge_preferred_index_history(preferred, pd.DataFrame())
    assert result.iloc[0]["volume"] == volume
    assert result.iloc[0]["volume_complete"]
    assert result.iloc[0]["volume_provider"] == "kite"


@pytest.mark.parametrize("changes", [dict(volume_complete=False), dict(volume_members_observed=1),
    dict(membership_version=""), dict(volume_provider="unverified"), dict(volume=float("inf"))])
def test_unverified_constituent_data_cannot_repair_cash_volume(changes):
    preferred = preferred_cash().assign(**changes)
    result = KiteAngelMigration._merge_preferred_index_history(preferred, pd.DataFrame())
    assert pd.isna(result.iloc[0]["volume"])
    assert not result.iloc[0]["volume_complete"]


def test_certified_constituent_cash_volume_precedes_fyers_backup():
    preferred = preferred_cash(123)
    backup = preferred.assign(volume=456, volume_basis="fyers_index")
    result = KiteAngelMigration._merge_preferred_index_history(preferred, backup)
    assert result.iloc[0]["volume"] == 123
    assert result.iloc[0]["volume_provider"] == "kite"


def test_unknown_past_membership_never_triggers_futile_second_broker_recovery(tmp_path):
    source = provider(tmp_path)
    service = source._constituent_volume
    service.memberships = tuple(replace(m, effective_from=DAY, effective_until=DAY+timedelta(days=1)) for m in service.memberships)
    bars = pd.to_datetime(["2026-10-22 10:00", "2026-10-23 10:00"])
    frame = pd.DataFrame(dict(open=[100,100], high=[101,101], low=[99,99], close=[100,100], volume=[10,10]), index=bars)
    source.kite = SimpleNamespace(get_historical=lambda *a, **k: frame.copy(), instrument_token=lambda *a: 1)
    calls = []
    source.angel_index = SimpleNamespace(resolve_market_instruments=lambda members: calls.append(members) or [])
    target = pd.Timestamp("2026-10-23 10:02")
    key = ("NIFTY", 2, target.isoformat(), False)
    source._build_replacement_history(key, target)
    recovered = source._replacement_history[key][1]
    assert calls == []
    assert not recovered.iloc[0]["volume_complete"]
    assert recovered.iloc[1]["volume_complete"]


def test_forced_live_schedule_keeps_verified_provider_cache_eligible():
    scanner = Scanner.__new__(Scanner)
    scanner.mode, scanner._last_data_fetch, scanner._data_fetch_interval = "REAL", {}, 60
    calls = []
    async def fetch(name, cfg, tf, **kwargs): calls.append((tf, kwargs.get("force_refresh", False)))
    scanner._fetch_one = fetch
    asyncio.run(scanner._fetch_all_data({"NIFTY": {}}, force=True, timeframes=["1min", "5min"]))
    assert calls == [("1min", False), ("5min", False)]


def test_fetch_one_forwards_force_to_real_broker_cache(monkeypatch):
    calls = []
    async def run():
        async def noop(*args): pass
        monkeypatch.setattr(asyncio, "sleep", noop)
        def history(*args, **kwargs): calls.append(kwargs); return pd.DataFrame()
        scanner = SimpleNamespace(mode="REAL", backtest_days=2, _api_semaphore=asyncio.Semaphore(1),
            _history_rate_limiter=SimpleNamespace(consume=noop), data=SimpleNamespace(get_historical_candles=history),
            _record_scan_phase_ms=lambda *args, **kwargs: None)
        await Scanner._fetch_one(scanner, "NIFTY", dict(token="1", exchange="NSE"), "1min", force_refresh=True)
    asyncio.run(run())
    assert calls[0]["force_refresh"] is True


def test_history_worker_wakes_for_real_mode_without_five_minute_gap(monkeypatch):
    scanner = SimpleNamespace(is_running=True, mode="HISTORICAL", active_indices=["NIFTY"],
        config=dict(indices={"NIFTY": {}}), _data_fetch_interval=60, _initial_setup_completed=True)
    sleeps, calls = [], []
    async def sleep(seconds):
        sleeps.append(seconds)
        scanner.mode = "REAL"
    async def fetch(indices, **kwargs):
        calls.append(kwargs)
        scanner.is_running = False
    scanner._fetch_all_data = fetch
    monkeypatch.setattr(asyncio, "sleep", sleep)
    asyncio.run(Scanner._background_history_worker(scanner))
    assert sleeps[0] <= 5
    assert calls == [dict(force=True, timeframes=["1min"])]


@pytest.mark.parametrize("message,delay,reason", [
    ("Invalid password", 300, "authentication_rejected"),
    ("MPIN attempts exceeded", 300, "authentication_attempt_limit"),
    ("Temporary server error", 30, "authentication_rejected")])
def test_authentication_retries_do_not_repeat_rejected_pin(monkeypatch, message, delay, reason):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    source = AngelOneIndexQuoteSource(api_key="dummy", client_id="dummy", password="1234", totp_secret="JBSWY3DPEHPK3PXP")
    calls = []
    def login(*args): calls.append(1); return dict(status=False, errorcode="AB1007", message=message)
    source._load_sdk = lambda: lambda **kwargs: SimpleNamespace(generateSession=login)
    assert not source.connect()
    assert source.last_error == reason and source.health()["auth_error_code"] == "AB1007"
    clock[0] += delay-1
    assert not source.connect() and len(calls) == 1
    clock[0] += 1
    assert not source.connect() and len(calls) == 2


@pytest.mark.parametrize("offset,accepted", [(0.2, True), (0.8, True), (1.5, False), (-30, False)])
def test_angel_index_stream_allows_only_bounded_clock_skew(monkeypatch, offset, accepted):
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    source = AngelOneIndexQuoteSource()
    source._jwt_token, source._feed_token = "dummy", "dummy"
    source.connect = lambda: True
    ready, stop = threading.Event(), threading.Event()
    sockets, ticks = [], []
    class Socket:
        def __init__(self, *args, **kwargs): sockets.append(self)
        def subscribe(self, *args): pass
        def connect(self): self.on_open(self);ready.set();stop.wait(2)
        def close_connection(self): stop.set()
    source._load_stream_sdk = lambda: Socket
    try:
        source.start_stream([dict(exchange_type=1, token="index", exchange="NSE", symbol="NIFTY")],
                            lambda identity, row: ticks.append(row))
        assert ready.wait(1)
        sockets[0].on_data(None, dict(exchange_type=1, token="index", last_traded_price=10000,
                                     exchange_timestamp=(1000+offset)*1000))
        assert bool(ticks) is accepted
        assert bool(source.stream_quote("NSE", "NIFTY")) is accepted
    finally:
        source.stop_stream();source._stream_thread.join(1)


def test_backup_route_does_not_starve_history_with_unsupported_quote_probes():
    source = MarketDataProvider.__new__(MarketDataProvider)
    source._market_data_route = "angel_fyers"
    source.fyers, source._ws_cache = object(), {}
    calls = []
    source._call_fyers_bounded = lambda *args, **kwargs: calls.append(args) or (True, dict(s="ok", d=[dict(v=dict(volume=0))]))
    for second in range(60):
        for index in SOURCES:
            assert source._probe_fyers_volume(index, 1000+second) == 0
    assert calls == []


def test_legacy_volume_quote_recovery_is_preserved():
    source = MarketDataProvider.__new__(MarketDataProvider)
    source._market_data_route = "fyers"
    source.fyers, source._ws_cache = object(), {}
    source._call_fyers_bounded = lambda *args, **kwargs: (True, dict(s="ok", d=[dict(v=dict(volume=100,lp=10))]))
    assert source._probe_fyers_volume("NIFTY", 1000) == 100
