import inspect
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from data.angelone_market_data import AngelOneIndexQuoteSource
from data.market_data import MarketDataProvider
from engine.pre_candle_predictor import _one_min_metrics
from scanner import Scanner


class _FakeSmartApi:
    def __init__(self, **_kwargs):
        self.calls = []

    def generateSession(self, client_id, password, totp):
        assert client_id and password and totp
        return {"status": True, "data": {"jwtToken": "never-log-me"}}

    def ltpData(self, exchange, symbol, token):
        self.calls.append((exchange, symbol, token))
        return {
            "status": True,
            "data": {
                "ltp": 75636.9,
                "close": 76132.81,
                "open": 75970.28,
                "high": 76012.11,
                "low": 75627.17,
            },
        }


class _FakeSmartApiWithOptions(_FakeSmartApi):
    instances = 0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        type(self).instances += 1

    def searchScrip(self, exchange, query):
        raise AssertionError("option resolution must not use unreliable searchScrip")

    def getMarketData(self, mode, exchangeTokens):
        assert mode == "FULL"
        assert exchangeTokens == {"NFO": ["47337", "47338"]}
        return {
            "status": True,
            "data": {
                "fetched": [
                    {"symbolToken": "47337", "ltp": 212.5, "opnInterest": 120000, "totTrdQty": 34000},
                    {"symbolToken": "47338", "ltp": 198.2, "opnInterest": 135000, "totTrdQty": 41000},
                ]
            },
        }


class _BatchSmartApi(_FakeSmartApi):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.market_data_calls = []

    def searchScrip(self, *_args, **_kwargs):
        raise AssertionError("option resolution must use daily instrument master")

    def getMarketData(self, mode, exchangeTokens):
        self.market_data_calls.append((mode, exchangeTokens))
        tokens = [token for values in exchangeTokens.values() for token in values]
        return {
            "status": True,
            "data": {
                "fetched": [
                    {
                        "symbolToken": token,
                        "ltp": 100.0 + position,
                        "opnInterest": 1000 + position,
                        "totTrdQty": 2000 + position,
                    }
                    for position, token in enumerate(tokens)
                ]
            },
        }


def test_angel_sensex_uses_full_bse_index_token(monkeypatch):
    source = AngelOneIndexQuoteSource(
        api_key="key", client_id="client", password="pin", totp_secret="JBSWY3DPEHPK3PXP"
    )
    monkeypatch.setattr(source, "_load_sdk", lambda: _FakeSmartApi)

    assert source.connect() is True
    quote = source.get_index_quote("SENSEX")

    assert quote["ltp"] == pytest.approx(75636.9)
    assert quote["source"] == "angel_rest"
    assert quote["volume_available"] is False
    assert source._api.calls == [("BSE", "SENSEX", "99919000")]


def test_angel_connection_is_idempotent_and_option_tokens_are_independent(monkeypatch):
    _FakeSmartApiWithOptions.instances = 0
    source = AngelOneIndexQuoteSource(
        api_key="key", client_id="client", password="pin", totp_secret="JBSWY3DPEHPK3PXP"
    )
    monkeypatch.setattr(source, "_load_sdk", lambda: _FakeSmartApiWithOptions)
    assert source.connect() is True
    assert source.connect() is True
    assert _FakeSmartApiWithOptions.instances == 1
    source._option_master_date = source._master_day()
    source._option_master_by_symbol = {
        ("NFO", "NIFTY15SEP2624450CE"): {"exchange": "NFO", "symbol": "NIFTY15SEP2624450CE", "token": "47337"},
        ("NFO", "NIFTY15SEP2624450PE"): {"exchange": "NFO", "symbol": "NIFTY15SEP2624450PE", "token": "47338"},
    }

    ce = {"symbol": "NIFTY2691524450CE", "token": "kite-ce", "expiry": "15SEP2026", "strike": 2445000, "exch_seg": "NFO"}
    pe = {"symbol": "NIFTY2691524450PE", "token": "kite-pe", "expiry": "15SEP2026", "strike": 2445000, "exch_seg": "NFO"}
    pair = source.get_option_pair("NIFTY", ce, pe)

    assert pair["CE"]["token"] == "47337"
    assert pair["PE"]["token"] == "47338"
    assert pair["CE"]["token"] != ce["token"]
    assert pair["CE"]["ltp"] == pytest.approx(212.5)
    assert pair["PE"]["oi"] == pytest.approx(135000)


@pytest.mark.parametrize(
    ("name", "exchange", "kite_symbol", "expiry", "strike", "expected"),
    [
        ("NIFTY", "NFO", "NIFTY2691524450CE", "15SEP2026", 2445000, "NIFTY15SEP2624450CE"),
        ("BANKNIFTY", "NFO", "BANKNIFTY26SEP56800CE", "29SEP2026", 5680000, "BANKNIFTY29SEP2656800CE"),
        ("MIDCPNIFTY", "NFO", "MIDCPNIFTY26SEP14675CE", "29SEP2026", 1467500, "MIDCPNIFTY29SEP2614675CE"),
        ("SENSEX", "BFO", "SENSEX2691075600CE", "10SEP2026", 7560000, "SENSEX2691075600CE"),
    ],
)
def test_angel_option_identity_covers_all_four_active_expiries(
    name, exchange, kite_symbol, expiry, strike, expected
):
    identity = AngelOneIndexQuoteSource._option_identity(
        name,
        {"symbol": kite_symbol, "expiry": expiry, "strike": strike, "exch_seg": exchange},
        "CE",
    )
    assert identity["symbol"] == expected
    assert identity["exchange"] == exchange


def test_angel_daily_master_cache_filters_and_reuses_current_day(tmp_path, monkeypatch):
    rows = [
        {"exch_seg": "NFO", "symbol": "BANKNIFTY29SEP2656800CE", "token": "69808"},
        {"exch_seg": "NFO", "symbol": "BANKNIFTY29SEP2656800PE", "token": "69810"},
        {"exch_seg": "NSE", "symbol": "SBIN-EQ", "token": "3045"},
    ]
    calls = []

    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return rows

    monkeypatch.setattr(
        "data.angelone_market_data.requests.get",
        lambda *_args, **_kwargs: calls.append(True) or Response(),
    )
    source = AngelOneIndexQuoteSource()
    source._option_master_cache_path = tmp_path / "angel-master.json"
    first = source._load_option_master()
    second = source._load_option_master()
    assert len(first) == 2
    assert second == first
    assert calls == [True]

    restored = AngelOneIndexQuoteSource()
    restored._option_master_cache_path = source._option_master_cache_path
    monkeypatch.setattr(
        "data.angelone_market_data.requests.get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("daily cache must avoid download")),
    )
    assert restored._load_option_master() == first


def test_angel_stale_master_never_resolves_live_contracts(tmp_path, monkeypatch):
    path = tmp_path / "angel-master.json"
    path.write_text(
        '{"date":"2000-01-01","contracts":[{"exchange":"NFO","symbol":"NIFTY15SEP2624450CE","token":"bad"}]}',
        encoding="utf-8",
    )
    source = AngelOneIndexQuoteSource()
    source._option_master_cache_path = path
    monkeypatch.setattr(
        "data.angelone_market_data.requests.get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    assert source._load_option_master() == {}
    assert source.last_error == "option_master_download_failed"


def test_angel_five_pairs_use_one_market_data_batch(monkeypatch):
    source = AngelOneIndexQuoteSource(
        api_key="key", client_id="client", password="pin", totp_secret="JBSWY3DPEHPK3PXP"
    )
    monkeypatch.setattr(source, "_load_sdk", lambda: _BatchSmartApi)
    assert source.connect()
    pairs = []
    master = {}
    for strike in (56800, 56700, 56900, 56600, 57000):
        ce = {"symbol": f"BANKNIFTY26SEP{strike}CE", "expiry": "29SEP2026", "strike": strike * 100, "exch_seg": "NFO"}
        pe = {"symbol": f"BANKNIFTY26SEP{strike}PE", "expiry": "29SEP2026", "strike": strike * 100, "exch_seg": "NFO"}
        pairs.append((ce, pe))
        for side, token_suffix in (("CE", "1"), ("PE", "2")):
            symbol = f"BANKNIFTY29SEP26{strike}{side}"
            master[("NFO", symbol)] = {"exchange": "NFO", "symbol": symbol, "token": f"{strike}{token_suffix}"}
    source._option_master_date = source._master_day()
    source._option_master_by_symbol = master

    result = source.get_option_pairs("BANKNIFTY", pairs)

    assert len(result) == 5
    assert len(source._api.market_data_calls) == 1
    mode, exchange_tokens = source._api.market_data_calls[0]
    assert mode == "FULL"
    assert len(exchange_tokens["NFO"]) == 10
    assert all(row["CE"]["ltp"] > 0 and row["PE"]["oi"] > 0 for row in result)


def test_angel_option_fallback_builds_five_real_rows_in_one_batch():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.fyers = None
    provider._option_chain_meta = {}
    provider.get_latest_price = lambda _name: 24450.0
    provider.get_option_token = lambda _name, strike, side: {
        "symbol": f"NIFTY26915{int(strike)}{side}",
        "token": f"kite-{int(strike)}-{side}",
        "expiry": "15SEP2026",
        "strike": int(strike) * 100,
        "exch_seg": "NFO",
    }

    def pairs_for(_name, pairs):
        rows = []
        for ce, _pe in pairs:
            strike = ce["strike"] / 100
            rows.append({
                "strike": strike,
                "expiry": ce["expiry"],
                "CE": {"symbol": f"NIFTY15SEP26{int(strike)}CE", "token": f"a-{strike}-CE", "ltp": 212.5, "oi": 120000, "volume": 34000},
                "PE": {"symbol": f"NIFTY15SEP26{int(strike)}PE", "token": f"a-{strike}-PE", "ltp": 198.2, "oi": 135000, "volume": 41000},
            })
        return rows

    provider.angel_index = SimpleNamespace(
        last_error="",
        get_option_pairs=pairs_for,
    )
    provider._apply_option_oi_snapshot_deltas = lambda _name, frame, **_kwargs: frame.assign(oi_change_source="angelone_reported")

    chain = provider.get_option_chain("NIFTY")

    assert len(chain) == 5
    assert chain.iloc[0]["call_ltp"] == pytest.approx(212.5)
    assert chain.iloc[0]["put_oi"] == pytest.approx(135000)
    assert "call_iv" not in chain.columns
    assert "put_iv" not in chain.columns
    assert "call_gamma" not in chain.columns
    assert "put_gamma" not in chain.columns
    assert provider._option_chain_meta["NIFTY"]["source"] == "angelone_near_atm_realtime"
    assert provider._option_chain_meta["NIFTY"]["auto_certified"] is True


def test_angel_option_fallback_fails_closed_without_real_quotes():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.fyers = None
    provider._option_chain_meta = {}
    provider.get_latest_price = lambda _name: 24450.0
    provider.get_option_token = lambda *_args: {"symbol": "X", "token": "kite", "expiry": "15SEP2026", "strike": 2445000, "exch_seg": "NFO"}
    provider.angel_index = SimpleNamespace(last_error="non_positive_option_quote", get_option_pairs=lambda *_args: [])

    chain = provider.get_option_chain("NIFTY")

    assert chain.empty
    assert provider._option_chain_meta["NIFTY"]["strike_count"] == 0
    assert provider._option_chain_meta["NIFTY"]["auto_certified"] is False
    assert provider._option_chain_meta["NIFTY"]["expiry_aligned"] is False


def test_angel_option_fallback_uses_nearest_complete_pair_without_crossing_strikes():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.fyers = None
    provider._option_chain_meta = {}
    provider.get_latest_price = lambda _name: 24450.0
    provider.get_option_token = lambda _name, strike, side: {
        "symbol": f"NIFTY26915{int(strike)}{side}",
        "token": f"kite-{int(strike)}-{side}",
        "expiry": "15SEP2026",
        "strike": int(strike) * 100,
        "exch_seg": "NFO",
    }

    def pair_for(_name, pairs):
        for ce, pe in pairs:
            assert ce["strike"] == pe["strike"]
            if ce["strike"] == 2440000:
                return [{
                    "strike": 24400,
                    "expiry": "15SEP2026",
                    "CE": {"symbol": "NIFTY15SEP2624400CE", "token": "41", "ltp": 230.0, "oi": 10, "volume": 20},
                    "PE": {"symbol": "NIFTY15SEP2624400PE", "token": "42", "ltp": 180.0, "oi": 30, "volume": 40},
                }]
        return []

    provider.angel_index = SimpleNamespace(last_error="", get_option_pairs=pair_for)
    provider._apply_option_oi_snapshot_deltas = lambda _name, frame, **_kwargs: frame.assign(oi_change_source="angelone_reported")

    chain = provider.get_option_chain("NIFTY")

    assert chain.iloc[0]["strike"] == 24400
    assert provider._option_chain_meta["NIFTY"]["requested_atm"] == 24450
    assert provider._option_chain_meta["NIFTY"]["resolved_strikes"] == [24400.0]


def test_verified_angel_multi_strike_fallback_is_intelligence_usable(monkeypatch):
    import scanner as scanner_module

    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(option_chain_entry_max_age_seconds=120.0),
    )
    scanner = Scanner.__new__(Scanner)
    frame = pd.DataFrame({"strike": [100, 200, 300, 400, 500]})
    quality = scanner._assess_chain_quality(
        "NIFTY",
        frame,
        {
            "source": "angelone_near_atm_realtime",
            "fetched_at": time.time() - 20,
            "fallback": True,
            "auto_certified": True,
            "active_expiry": "15SEP2026",
            "expiry_aligned": True,
        },
    )
    assert quality["score"] == 75
    assert quality["fallback"] is True
    assert quality["auto_certified"] is True
    assert quality["intelligence_usable"] is True


@pytest.mark.parametrize(
    ("inst_type", "age", "iv_percentile", "regime", "blocked"),
    [
        ("OPT", 119.9, 50.0, "TRENDING", False),
        ("OPT", 120.1, 50.0, "TRENDING", True),
        ("OPT", 89.9, 75.0, "TRENDING", False),
        ("OPT", 90.1, 75.0, "TRENDING", True),
        ("OPT", 90.1, 20.0, "VOLATILE", True),
        ("FUT", 999.0, 99.0, "VOLATILE", False),
    ],
)
def test_option_entry_chain_age_uses_normal_and_high_volatility_bands(
    monkeypatch, inst_type, age, iv_percentile, regime, blocked
):
    import scanner as scanner_module

    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(
            option_chain_entry_max_age_seconds=120.0,
            option_chain_high_vol_entry_max_age_seconds=90.0,
            option_chain_high_vol_percentile=70.0,
        ),
    )
    scanner = Scanner.__new__(Scanner)
    candidate = SimpleNamespace(
        inst_type=inst_type,
        iv_percentile=iv_percentile,
        chain_quality={"age_seconds": age, "stale": False},
    )
    reason = scanner._option_chain_entry_freshness_blocker(candidate, regime)
    assert bool(reason) is blocked


def test_live_option_entry_fails_closed_when_chain_age_evidence_is_missing():
    scanner = Scanner.__new__(Scanner)
    candidate = SimpleNamespace(
        inst_type="OPT",
        data_provenance="LIVE",
        chain_quality={},
    )

    reason = scanner._option_chain_entry_freshness_blocker(candidate, "TRENDING")

    assert "missing" in reason


def test_angel_prewarm_runs_once_without_blocking_caller():
    connected = threading.Event()

    class Source:
        configured = True

        @staticmethod
        def connect():
            connected.set()
            return True

    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.angel_index = Source()
    provider._angel_prewarm_lock = threading.Lock()
    provider._angel_prewarm_started = False

    started = time.monotonic()
    assert provider._prewarm_angelone_fallback() is True
    assert provider._prewarm_angelone_fallback() is False
    assert time.monotonic() - started < 0.1
    assert connected.wait(1.0)


def test_price_bridge_uses_angel_before_yahoo(monkeypatch):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._ws_cache = {}
    provider._ltp_cache = {}
    provider._index_quote_lock = threading.RLock()
    provider._index_price_route = {}
    provider._prev_close_global_cache = {}
    provider._prev_close_cache_time = {}
    provider._prev_close_cache_source = {}
    provider._yahoo_request_count = 0
    provider._yahoo_last_used_at = None
    provider.kite = SimpleNamespace(is_connected=False)
    provider.fyers = None
    provider.smart_api = None
    provider.is_connected = False
    provider.angel_index = SimpleNamespace(
        get_index_quote=lambda _symbol: {
            "ltp": 75636.9,
            "close": 76132.81,
            "previous_close": 76132.81,
            "received_at": 1_788_849_000.0,
            "source": "angel_rest",
        }
    )
    provider._select_index_ws_quote = lambda *_args, **_kwargs: (None, None)
    monkeypatch.setattr(
        "data.market_data.yf.Ticker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Yahoo must not run when AngelOne answered")
        ),
    )

    price = provider.get_latest_price_by_token("99919000", "SENSEX", "BSE")

    assert price == pytest.approx(75636.9)
    assert provider._ltp_cache["SENSEX"]["source"] == "angel_rest"
    assert provider._yahoo_request_count == 0


def test_angel_quote_is_fresh_in_dashboard_cache_lane():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    now = time.time()
    provider._ws_cache = {}
    provider._ltp_cache = {
        "SENSEX": {
            "ltp": 75636.9,
            "previous_close": 76132.81,
            "received_at": now,
            "time": now,
            "source": "angel_rest",
        }
    }
    provider._index_quote_lock = threading.RLock()
    provider._index_price_route = {}
    provider._kite_index_names = {}
    provider._prev_close_global_cache = {"SENSEX": 76132.81}
    provider.get_market_session_label = lambda _dt=None: "LIVE"

    quote = provider.get_cached_index_quote_snapshot("SENSEX", now_ts=now + 0.2)

    assert quote["ltp"] == pytest.approx(75636.9)
    assert quote["quote_source"] == "angel_rest"
    assert quote["quote_fresh"] is True
    assert quote["quote_age_seconds"] == pytest.approx(0.2)


def _futures_provider():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._index_futures_minute_lock = threading.RLock()
    provider._index_futures_minute_state = {}
    return provider


def test_futures_minute_ratio_has_contracts_only_baseline():
    provider = _futures_provider()
    base = 1_788_849_000.0
    cumulative = 10_000.0
    future = {"tradingsymbol": "NIFTY26SEPFUT", "volume": cumulative}
    provider._record_index_futures_volume_snapshot("NIFTY", future, now_ts=base + 5)
    for minute in range(7):
        cumulative += 100.0
        future["volume"] = cumulative
        provider._record_index_futures_volume_snapshot(
            "NIFTY", future, now_ts=base + minute * 60 + 50
        )
        if minute < 6:
            provider._record_index_futures_volume_snapshot(
                "NIFTY", future, now_ts=base + (minute + 1) * 60 + 5
            )

    context = provider.get_index_futures_minute_volume_context(
        "NIFTY", now_ts=base + 6 * 60 + 50
    )

    assert context["unit"] == "contracts"
    assert context["reliable"] is True
    assert context["completed_samples"] >= 5
    assert context["baseline_volume"] == pytest.approx(100.0)
    assert context["ratio"] == pytest.approx(1.0)
    assert context["projected_ratio"] == pytest.approx(1.2)


def test_contract_rollover_resets_futures_baseline():
    provider = _futures_provider()
    provider._record_index_futures_volume_snapshot(
        "NIFTY", {"tradingsymbol": "NIFTY26SEPFUT", "volume": 1000}, now_ts=1_788_849_005
    )
    provider._record_index_futures_volume_snapshot(
        "NIFTY", {"tradingsymbol": "NIFTY26OCTFUT", "volume": 50}, now_ts=1_788_849_065
    )
    context = provider.get_index_futures_minute_volume_context(
        "NIFTY", now_ts=1_788_849_066
    )
    assert context["tradingsymbol"] == "NIFTY26OCTFUT"
    assert context["completed_samples"] == 0
    assert context["reliable"] is False


def test_fyers_forming_cash_volume_is_current_minute_only_and_projected():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._index_intrabar_cash_volume_lock = threading.RLock()
    provider._index_intrabar_cash_volume = {}
    now = datetime.now().replace(second=20, microsecond=0)
    index = pd.DatetimeIndex([now.replace(second=0)])
    provider._record_index_intrabar_cash_volume(
        "NIFTY", pd.DataFrame({"volume": [300.0]}, index=index), received_at=now
    )
    current = provider.get_index_intrabar_cash_volume("NIFTY", now_dt=now + timedelta(seconds=1))
    expired = provider.get_index_intrabar_cash_volume("NIFTY", now_dt=now + timedelta(minutes=1))

    assert current["available"] is True
    assert current["volume"] == pytest.approx(300.0)
    assert current["progress"] == pytest.approx(0.35)
    assert expired["available"] is False

    candles = pd.DataFrame(
        {
            "open": [1] * 6, "high": [2] * 6, "low": [0] * 6, "close": [2] * 6,
            "volume": [1000.0] * 5 + [0.0],
        },
        index=pd.date_range(end=now.replace(second=0), periods=6, freq="1min"),
    )
    metrics = _one_min_metrics(
        candles,
        "LONG",
        {"volume_available": True, "volume_reliable": True, "intrabar_cash_volume": 300.0, "intrabar_cash_progress": 0.3},
    )
    assert metrics["volume_available"] is True
    assert metrics["volume_reliable"] is True
    assert metrics["volume_ratio"] == pytest.approx(1.0)


def test_prediction_context_uses_tagged_intrabar_cash_volume_only_with_fresh_baseline():
    now = datetime.now().replace(second=30, microsecond=0)
    frame = pd.DataFrame(
        {
            "open": [1] * 10, "high": [2] * 10, "low": [0] * 10, "close": [2] * 10,
            "volume": [1000.0] * 9 + [0.0],
            "volume_basis": ["fyers_index"] * 9 + [""],
            "source": ["fyers_history"] * 9 + ["live_tick"],
            "tick_count": [10] * 10,
        },
        index=pd.date_range(end=now.replace(second=0), periods=10, freq="1min"),
    )
    scanner = Scanner.__new__(Scanner)
    scanner.data = SimpleNamespace(
        _index_cum_volume_source={}, _index_cum_volume={},
        _index_futures_fallback={},
        get_index_tick_volume_diagnostics=lambda: {},
        get_index_futures_minute_volume_context=lambda _name: {},
        get_index_intrabar_cash_volume=lambda _name: {
            "available": True, "volume": 500.0, "progress": 0.5,
            "age_seconds": 1.0, "basis": "fyers_index",
        },
    )

    context = scanner._prediction_volume_context("NIFTY", frame)

    assert context["threshold_volume_available"] is True
    assert context["intrabar_volume_available"] is True
    assert context["volume_available"] is True
    assert context["volume_reliable"] is True
    assert context["intrabar_cash_volume"] == pytest.approx(500.0)


def test_live_scan_analysis_is_network_free_for_index_price():
    source = inspect.getsource(Scanner._process_instrument)
    assert "get_cached_index_quote_snapshot" in source
    assert "self.data.get_previous_close" not in source


def test_fyers_quote_does_not_queue_behind_history_lane():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._bounded_call_lock = threading.Lock()
    provider._bounded_call_completed = {}
    provider._bounded_call_active = {
        "key": ("history",),
        "label": "fyers.history(NIFTY 1)",
        "outcome": {},
        "done": threading.Event(),
        "started": time.monotonic(),
    }
    called = []

    started = time.monotonic()
    ok, value = provider._call_bounded(
        lambda: called.append(True),
        timeout=8.0,
        label="Fyers price quote",
        wait_if_busy=False,
    )

    assert (ok, value) == (False, None)
    assert called == []
    assert time.monotonic() - started < 0.1
    bridge_source = inspect.getsource(MarketDataProvider.get_latest_price_by_token)
    assert "timeout_seconds=2.0" in bridge_source
    assert "wait_if_busy=False" in bridge_source


def test_ltp_worker_skips_fresh_websocket_quotes_but_polls_rest_fallbacks():
    scanner = Scanner.__new__(Scanner)
    scanner.data = SimpleNamespace(
        get_cached_index_quote_snapshot=lambda _name: {
            "ltp": 24000.0,
            "quote_source": "kite_ws",
            "quote_fresh": True,
            "receipt_age_seconds": 0.1,
        }
    )
    assert scanner._index_ltp_poll_due("NIFTY") is False

    scanner.data.get_cached_index_quote_snapshot = lambda _name: {
        "ltp": 24000.0,
        "quote_source": "angel_rest",
        "quote_fresh": True,
        "receipt_age_seconds": 2.0,
    }
    assert scanner._index_ltp_poll_due("NIFTY") is True


def test_dashboard_uses_angelone_badge_and_keeps_quote_lane_cache_only():
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")
    js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")
    css = Path("dashboard/static/css/style.css").read_text(encoding="utf-8")

    assert 'id="gw_angelone"' in html
    assert 'id="gw_yahoo"' not in html
    assert "angelone_index_quotes" in js
    assert "gw_angelone" in js
    assert ".gateway-badge.standby" in css
    assert "setInterval(refreshIndexQuotes, 500)" in js


def test_live_history_worker_avoids_redundant_higher_timeframe_polling():
    source = inspect.getsource(Scanner._background_history_worker)
    real_branch = source.split('if self.mode == "REAL":', 1)[1].split("else:", 1)[0]
    assert 'timeframes=["1min"]' in real_branch
    assert 'timeframes=["5min", "15min"]' not in real_branch
    assert "next_minute" in real_branch
