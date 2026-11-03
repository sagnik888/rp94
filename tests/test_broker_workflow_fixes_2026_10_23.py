"""Constructor safety, fair index recovery and measured option-volume evidence."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import logging
import math
import ssl
import threading
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from data.angelone_market_data import AngelOneIndexQuoteSource, ANGELONE_INDEX_TARGETS
from data.angelone_readonly import ReadOnlyBudget
from data.kite_market_data import KiteDataSource
from data.market_data import MarketDataProvider
from scanner import Scanner
from tests.test_kite_angel_replacement_2026_10_23 import chain_provider, chain_frame
from tests.test_three_broker_priority_2026_10_23 import native_fyers_chain


def index_source(quote):
    source = AngelOneIndexQuoteSource()
    source.connected = True
    source._budget = ReadOnlyBudget()
    source._api = SimpleNamespace(getMarketData=quote)
    return source


def full_rows(tokens, **fields):
    return dict(status=True, data=dict(fetched=[dict(exchange=exchange, symbolToken=token,
        ltp=100, **fields) for exchange, values in tokens.items() for token in values]))


def test_fixed_order_index_recovery_serves_every_index_with_one_bounded_batch(monkeypatch):
    clock = [0.0]
    base = pd.Timestamp("2026-10-23 09:15", tz="Asia/Kolkata").timestamp()
    monkeypatch.setattr(time, "time", lambda: base + clock[0])
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    calls, reads = [], Counter()

    def quote(mode, exchangeTokens):
        assert mode == "FULL"
        calls.append(clock[0])
        assert sum(map(len, exchangeTokens.values())) == 4
        return full_rows(exchangeTokens, exchFeedTime=pd.Timestamp(
            base + clock[0], unit="s", tz="UTC").isoformat())

    source = index_source(quote)
    for step in range(150):
        clock[0] = step * 0.2
        for name in ANGELONE_INDEX_TARGETS:
            row = source.get_index_quote(name)
            if row:
                reads[name] += 1
                assert 0 <= time.time() - row["received_at"] <= 2
    assert reads == Counter({name: 150 for name in ANGELONE_INDEX_TARGETS})
    assert len(calls) <= 15
    assert all(second - first >= 1.05 for first, second in zip(calls, calls[1:]))


def test_concurrent_index_reads_share_one_inflight_batch():
    calls = []
    def quote(mode, exchangeTokens):
        calls.append(exchangeTokens)
        time.sleep(0.03)
        return full_rows(exchangeTokens)
    source = index_source(quote)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(source.get_index_quote, list(ANGELONE_INDEX_TARGETS) * 2))
    assert len(calls) == 1
    assert all(row and row["ltp"] == 100 for row in rows)


def test_partial_index_batch_never_fabricates_or_renews_missing_rows(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    calls = []
    def quote(mode, exchangeTokens):
        calls.append(exchangeTokens)
        response = full_rows(exchangeTokens)
        if len(calls) == 1:
            response["data"]["fetched"] = [row for row in response["data"]["fetched"] if row["exchange"] != "BSE"]
        return response
    source = index_source(quote)
    source._budget = SimpleNamespace(reserve=lambda *args, **kwargs: len(calls) != 1)
    assert source.get_index_quote("NIFTY")
    assert source.get_index_quote("SENSEX") is None
    assert source.get_index_quote("BANKNIFTY")
    clock[0] += 3
    source._budget = SimpleNamespace(reserve=lambda *args, **kwargs: True)
    assert source.get_index_quote("SENSEX")["received_at"] == 1003
    assert len(calls) == 2


def test_expired_exchange_clock_is_not_reblessed_by_fresh_cache_receipt(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    source = index_source(lambda mode, exchangeTokens: full_rows(exchangeTokens))
    source._index_rest_cache["SENSEX"] = dict(ltp=100, received_at=1000, exchange_at=994.5)
    source._budget = SimpleNamespace(reserve=lambda *args, **kwargs: False)
    clock[0] = 1001
    assert source.get_index_quote("SENSEX") is None


def test_real_sdk_constructors_cannot_reenable_secret_logs(tmp_path, monkeypatch, capsys):
    import logzero
    import requests
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: SimpleNamespace(text="127.0.0.1"))
    log = logzero.logger
    previous_disabled, previous_level = log.disabled, log.level
    previous_handlers = list(log.handlers)
    secrets = ("AUDIT_API_SENTINEL", "AUDIT_TOKEN_SENTINEL", "AUDIT_PASSWORD_SENTINEL")
    try:
        log.disabled = False
        rest_type = AngelOneIndexQuoteSource._load_sdk()
        for _ in range(2):
            api = rest_type(api_key=secrets[0], access_token=secrets[1])
            assert log.disabled
            socket_type = AngelOneIndexQuoteSource._load_stream_sdk()
            socket = socket_type("dummy-jwt", "dummy-api", "dummy-client", "dummy-feed", max_retry_attempt=0)
            import websocket
            options = []
            monkeypatch.setattr(websocket, "WebSocketApp", lambda *args, **kwargs:
                                SimpleNamespace(run_forever=lambda **kw: options.append(kw)))
            socket.connect()
            assert options[0]["sslopt"]["cert_reqs"] == ssl.CERT_REQUIRED
            assert options[0]["ping_payload"] == socket.HEART_BEAT_MESSAGE
            assert log.disabled
            monkeypatch.setattr(requests, "request", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
            with pytest.raises(RuntimeError, match="offline"):
                api.generateSession("dummy-client", secrets[2], "123456")
        for handler in log.handlers:
            handler.flush()
        output = capsys.readouterr()
        written = output.out + output.err + "".join(path.read_text(errors="replace") for path in tmp_path.rglob("*.log"))
        assert all(secret not in written for secret in secrets)
    finally:
        for handler in list(log.handlers):
            if handler not in previous_handlers:
                log.removeHandler(handler)
                handler.close()
        log.disabled = previous_disabled
        log.setLevel(previous_level)


@pytest.mark.parametrize("value", [None, "", "invalid", float("nan"), float("inf"), -1])
def test_invalid_option_volume_is_unavailable(value):
    assert math.isnan(AngelOneIndexQuoteSource._option_volume({"tradeVolume": value}))


def test_missing_option_volume_is_unavailable():
    assert math.isnan(AngelOneIndexQuoteSource._option_volume({}))


@pytest.mark.parametrize("value", [0, 12345, "12345"])
def test_measured_option_volume_including_zero_is_preserved(value):
    assert AngelOneIndexQuoteSource._option_volume({"tradeVolume": value}) == float(value)


@pytest.mark.parametrize("payload,available", [({}, False), ({"tradeVolume": 0}, True)])
def test_actual_angel_pairs_keep_missing_and_zero_distinct(payload, available):
    source = AngelOneIndexQuoteSource()
    source.connected, source._api = True, object()
    source._option_identity = lambda index, contract, side: dict(exchange="NFO", symbol=side, query="same")
    source._load_option_master = lambda: {("NFO", side): dict(exchange="NFO", symbol=side, token=side)
                                        for side in ("CE", "PE")}
    source.get_full_quotes = lambda tokens, **kwargs: {("NFO", side): dict(ltp=10, opnInterest=100, **payload)
                                                     for side in ("CE", "PE")}
    pairs = source.get_option_pairs("NIFTY", [(dict(strike=2500000), dict(strike=2500000))])
    assert len(pairs) == 1
    for side in ("CE", "PE"):
        assert pairs[0][side]["volume_available"] is available
        assert pairs[0][side]["volume"] == 0 if available else math.isnan(pairs[0][side]["volume"])


@pytest.mark.parametrize("payload,expected", [({}, None), ({"volume": 0, "volume_traded": 999}, 0),
    ({"volume_traded": 123}, 123), ({"volume": "invalid", "volume_traded": 999}, None)])
def test_actual_kite_chain_preserves_volume_evidence(payload, expected):
    expiry = pd.Timestamp("2026-10-30").date()
    quotes = {"NFO:" + side: dict(last_price=10, oi=100, **payload) for side in ("CE", "PE")}
    source = SimpleNamespace(
        _lock=threading.RLock(),
        _instruments={"NFO": [dict(name="NIFTY", expiry=expiry, instrument_type=side,
                                    strike=25000, tradingsymbol=side) for side in ("CE", "PE")]},
        available_strikes=lambda *args, **kwargs: [25000],
        _key=KiteDataSource._key, _book_is_usable=KiteDataSource._book_is_usable,
        _call=lambda *args, **kwargs: quotes,
    )
    chain = KiteDataSource.get_option_chain(source, "NIFTY", spot=25000, expiry=expiry)
    for side in ("call", "put"):
        assert bool(chain.iloc[0][side + "_volume_available"]) is (expected is not None)
        assert chain.iloc[0][side + "_volume"] == expected if expected is not None else pd.isna(chain.iloc[0][side + "_volume"])


@pytest.mark.parametrize("provider", ["kite", "angelone"])
def test_missing_preferred_volume_reaches_fyers_backup(tmp_path, monkeypatch, provider):
    result = chain_provider(tmp_path)
    result._market_data_route = "angel_fyers"
    calls = native_fyers_chain(result, monkeypatch)
    result.kite.get_option_chain = lambda *args, **kwargs: chain_frame().assign(call_volume=float("nan"))
    def pairs(index, contracts):
        return [dict(strike=ce["strike"] / 100,
            CE=dict(ltp=10, oi=100, volume=float("nan"), volume_available=False,
                    symbol="ACE", token="a", exchange_at=time.time()),
            PE=dict(ltp=10, oi=100, volume=20, volume_available=True,
                    symbol="APE", token="b", exchange_at=time.time())) for ce, pe in contracts]
    if provider == "angelone":
        result.kite.get_option_chain = lambda *args, **kwargs: pd.DataFrame()
    result.angel_index.get_option_pairs = pairs
    chain = result.get_option_chain("NIFTY")
    assert len(chain) == 3 and calls == ["fyers"]
    assert result._option_chain_meta["NIFTY"]["source"] == "fyers"
    assert chain["call_volume"].eq(10).all()


@pytest.mark.parametrize("missing", ["volume_complete", "coverage_complete", "exchange_clocks_fresh"])
def test_uncertified_volume_stays_context_only_on_deferred_cache_reads(tmp_path, missing):
    chain = chain_frame().assign(call_volume=float("nan"))
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    meta = dict(source="kite", fallback=False, fetched_at=time.time(), expiry_aligned=True,
                coverage_complete=True, exchange_clocks_fresh=True, volume_complete=True)
    meta[missing] = False
    quality = scanner._assess_chain_quality("NIFTY", chain, meta)
    assert not quality["intelligence_usable"]
    scanner._chain_cache = {"NIFTY": chain}
    scanner._chain_quality_cache = {"NIFTY": quality}
    scanner._last_chain_fetch = {"NIFTY": time.time() - 100}
    scanner.data = SimpleNamespace()
    cached, deferred = scanner._get_option_chain_cached("NIFTY", {}, allow_fetch=False)
    assert cached is chain
    assert deferred[missing] is False
    assert not deferred["intelligence_usable"]


def test_explicit_zero_volume_can_be_certified(tmp_path):
    result = chain_provider(tmp_path)
    result.kite.get_option_chain = lambda *args, **kwargs: chain_frame().assign(call_volume=0, put_volume=0)
    result.get_option_chain("NIFTY")
    assert result._option_chain_meta["NIFTY"]["volume_complete"]
    assert result._option_chain_meta["NIFTY"]["auto_certified"]


@pytest.mark.parametrize("backup", [False, True])
@pytest.mark.parametrize("variant", ["missing_column", "missing_value", "invalid", "infinite", "negative", "zero", "positive"])
def test_native_fyers_volume_never_fabricates_zero_or_certifies_missing(tmp_path, monkeypatch, backup, variant):
    result = chain_provider(tmp_path)
    result._market_data_route = "angel_fyers" if backup else "fyers"
    native_fyers_chain(result, monkeypatch)
    original = result._call_fyers_bounded

    def quote(*args, **kwargs):
        ok, response = original(*args, **kwargs)
        rows = response["data"]["optionsChain"]
        if variant == "missing_column":
            for row in rows:
                row.pop("volume", None)
        elif variant == "missing_value":
            rows[0].pop("volume", None)
        else:
            rows[0]["volume"] = {"invalid": "bad", "infinite": float("inf"),
                                  "negative": -1, "zero": 0, "positive": 10}[variant]
        return ok, response

    result._call_fyers_bounded = quote
    result.kite.get_option_chain = lambda *args, **kwargs: pd.DataFrame()
    result.angel_index.get_option_pairs = lambda *args, **kwargs: []
    chain = result.get_option_chain("NIFTY")
    valid = variant in {"zero", "positive"}
    meta = result._option_chain_meta["NIFTY"]
    assert meta["volume_complete"] is valid
    assert chain.attrs["volume_complete"] is valid
    if backup:
        assert meta["auto_certified"] is valid
    assert bool(Scanner.__new__(Scanner)._assess_chain_quality("NIFTY", chain, meta)["intelligence_usable"]) is valid
    if not valid:
        assert pd.isna(chain.iloc[0]["call_volume"])
        assert not chain.iloc[0]["call_volume_available"]
    elif variant == "zero":
        assert chain.iloc[0]["call_volume"] == 0
        assert chain.iloc[0]["call_volume_available"]


@pytest.mark.parametrize("volume,available,valid", [(float("nan"), False, False),
    ("bad", False, False), (20, False, False), (0, True, True)])
def test_legacy_angel_chain_keeps_volume_refusal(volume, available, valid):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.fyers = None
    provider._option_chain_meta = {}
    provider.get_latest_price = lambda name: 24450
    provider.get_option_token = lambda name, strike, side: dict(strike=strike*100, expiry="15SEP2026")

    def pairs(name, contracts):
        return [dict(strike=ce["strike"]/100, expiry=ce["expiry"], **{
            side: dict(ltp=10, oi=100, volume=volume, volume_available=available, symbol=side, token=side)
            for side in ("CE", "PE")}) for ce, pe in contracts]

    provider.angel_index = SimpleNamespace(get_option_pairs=pairs)
    provider._apply_option_oi_snapshot_deltas = lambda name, frame, **kwargs: frame.assign(oi_change_source="angelone_snapshot")
    chain = provider.get_option_chain("NIFTY")
    meta = provider._option_chain_meta["NIFTY"]
    assert meta["volume_complete"] is valid
    assert meta["auto_certified"] is valid
    assert bool(Scanner.__new__(Scanner)._assess_chain_quality("NIFTY", chain, meta)["intelligence_usable"]) is valid


def test_unknown_nullable_volume_flag_cannot_certify_available_numbers():
    chain = pd.DataFrame(dict(call_volume=[10], put_volume=[20],
                              call_volume_available=pd.Series([pd.NA], dtype="boolean")))
    assert not MarketDataProvider._chain_volume_is_usable(chain)
