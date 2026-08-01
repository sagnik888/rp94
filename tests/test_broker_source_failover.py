import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import data.market_data as market_data_module
from data.market_data import FYERS_INDEX_SYMBOLS, MarketDataProvider
from scanner import Scanner
from trading.broker import SmartApiBroker


def _provider(monkeypatch):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._ws_cache = {}
    provider._ltp_cache = {}
    provider._yahoo_request_count = 0
    provider._yahoo_last_used_at = None
    provider._last_ltp_time = 0.0
    provider._last_angel_ws_tick = 0.0
    provider._last_fyers_ws_tick = 0.0
    provider._angel_rest_backoff_until = 0.0
    provider._angel_rest_backoff_failures = 0
    provider._last_angel_rest_backoff_reason = ""
    provider._fyers_auth_required = False
    provider._fyers_auth_reason = ""
    provider._fyers_refresh_disabled = False
    provider.nse_holidays = set()
    provider.nse_special_trading_days = set()
    provider.is_connected = False
    provider._rest_lock = __import__("threading").Lock()
    provider.smart_api = None
    provider.fyers = None
    monkeypatch.setattr(provider, "is_market_open", lambda dt=None: True)
    return provider


def test_source_health_keeps_yahoo_idle_when_main_brokers_are_fresh(monkeypatch):
    provider = _provider(monkeypatch)
    now = time.time()
    provider.is_connected = True
    provider.smart_api = object()
    provider.fyers = object()
    provider._last_angel_ws_tick = now
    provider._last_fyers_ws_tick = now

    health = provider.get_source_health()

    assert health["kite"] is True
    assert health["fyers"] is True
    assert health["broker_degraded"] is False
    assert health["all_brokers_unavailable"] is False
    assert health["yahoo_mode"] == "idle_standby"
    assert health["yahoo_request_count"] == 0


def test_yahoo_mode_returns_to_idle_after_brokers_recover(monkeypatch):
    provider = _provider(monkeypatch)
    now = time.time()
    provider.is_connected = True
    provider.smart_api = object()
    provider.fyers = object()
    provider._last_angel_ws_tick = now
    provider._last_fyers_ws_tick = now
    provider._yahoo_request_count = 3
    provider._yahoo_last_used_at = now - 30

    health = provider.get_source_health()

    assert health["all_brokers_unavailable"] is False
    assert health["yahoo_mode"] == "idle_standby"
    assert health["yahoo_request_count"] == 3


def test_source_health_marks_single_broker_degraded_but_not_total_outage(monkeypatch):
    provider = _provider(monkeypatch)
    now = time.time()
    provider.fyers = object()
    provider._last_fyers_ws_tick = now

    health = provider.get_source_health()

    assert health["kite"] is False
    assert health["fyers"] is True
    assert health["broker_degraded"] is True
    assert health["all_brokers_unavailable"] is False


def test_source_health_marks_total_outage_for_live_market(monkeypatch):
    provider = _provider(monkeypatch)
    provider._yahoo_request_count = 1

    health = provider.get_source_health()

    assert health["kite"] is False
    assert health["fyers"] is False
    assert health["broker_degraded"] is True
    assert health["all_brokers_unavailable"] is True
    assert health["yahoo_mode"] == "emergency_used"


def test_market_snapshot_allows_entries_only_from_fresh_kite_authority(monkeypatch):
    provider = _provider(monkeypatch)
    now = time.time()
    provider._ws_cache["99926000"] = {"ltp": 23250.0, "time": now, "source": "kite_ws"}

    snapshot = provider.get_market_source_snapshot("NIFTY")

    assert snapshot["entry_eligible"] is True
    assert snapshot["freshest"]["source"] == "kite_ws"
    assert snapshot["freshest"]["order_authority"] is True


def test_market_snapshot_uses_fyers_for_context_but_blocks_new_entries(monkeypatch):
    provider = _provider(monkeypatch)
    now = time.time()
    provider._ws_cache[FYERS_INDEX_SYMBOLS["NIFTY"]] = {
        "ltp": 23250.0,
        "time": now,
        "source": "fyers_ws",
    }

    snapshot = provider.get_market_source_snapshot("NIFTY")

    assert snapshot["entry_eligible"] is False
    assert snapshot["freshest"]["source"] == "fyers_ws"
    assert snapshot["freshest"]["latency_class"] == "REALTIME"
    assert snapshot["freshest"]["order_authority"] is False


def test_yahoo_is_emergency_delayed_context_and_never_entry_eligible(monkeypatch):
    provider = _provider(monkeypatch)
    provider._ltp_cache["NIFTY"] = {
        "ltp": 23200.0,
        "time": time.time(),
        "source": "yahoo",
    }

    snapshot = provider.get_market_source_snapshot("NIFTY")

    assert snapshot["entry_eligible"] is False
    assert snapshot["freshest"]["source"] == "yahoo"
    assert snapshot["freshest"]["latency_class"] == "DELAYED"


def test_price_bridge_does_not_touch_yahoo_when_fyers_ws_is_fresh(monkeypatch):
    provider = _provider(monkeypatch)
    provider._ws_cache[FYERS_INDEX_SYMBOLS["NIFTY"]] = {
        "ltp": 23275.0,
        "time": time.time(),
        "source": "fyers_ws",
    }

    def fail_yahoo(*args, **kwargs):
        raise AssertionError("Yahoo should remain idle while a broker feed is fresh")

    monkeypatch.setattr("data.market_data.yf.Ticker", fail_yahoo)

    price = provider.get_latest_price_by_token("99926000", "NIFTY", "NSE")

    assert price == 23275.0
    assert provider._yahoo_request_count == 0


def test_market_session_label_distinguishes_live_preopen_and_closed(monkeypatch):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.nse_holidays = set()
    provider.nse_special_trading_days = set()

    assert provider.get_market_session_label(datetime(2026, 6, 16, 9, 10)) == "PRE-OPEN"
    assert provider.get_market_session_label(datetime(2026, 6, 16, 9, 15)) == "LIVE"
    assert provider.get_market_session_label(datetime(2026, 6, 16, 15, 31)) == "CLOSED"


def test_index_quote_marks_fresh_ws_price_as_live(monkeypatch):
    provider = _provider(monkeypatch)
    now = time.time()
    provider._ws_cache["99926000"] = {
        "ltp": 23275.0,
        "change": 25.0,
        "change_pct": 0.11,
        "time": now - 1.25,
        "source": "kite_ws",
    }
    monkeypatch.setattr(provider, "get_market_session_label", lambda dt=None: "LIVE")

    quote = provider.get_index_quote("NIFTY")

    assert quote["ltp"] == 23275.0
    assert quote["quote_label"] == "LIVE"
    assert quote["session"] == "LIVE"
    assert quote["is_preopen"] is False
    assert 1.0 <= quote["quote_age_seconds"] <= 2.0


def test_index_quote_keeps_fresh_kite_source_when_fyers_tick_is_newer(monkeypatch):
    provider = _provider(monkeypatch)
    now = time.time()
    provider._ws_cache["99926000"] = {
        "ltp": 23275.0,
        "time": now - 0.5,
        "source": "kite_ws",
    }
    provider._ws_cache[FYERS_INDEX_SYMBOLS["NIFTY"]] = {
        "ltp": 23190.0,
        "time": now - 0.01,
        "source": "fyers_ws",
    }

    quote = provider.get_index_quote("NIFTY")

    assert quote["ltp"] == 23275.0
    assert quote["source"] == "kite_ws"


def test_index_quote_fails_over_to_fyers_when_kite_tick_is_stale(monkeypatch):
    provider = _provider(monkeypatch)
    now = time.time()
    provider._ws_cache["99926000"] = {
        "ltp": 23200.0,
        "time": now - 7.0,
        "source": "kite_ws",
    }
    provider._ws_cache[FYERS_INDEX_SYMBOLS["NIFTY"]] = {
        "ltp": 23280.0,
        "time": now - 0.25,
        "source": "fyers_ws",
    }

    quote = provider.get_index_quote("NIFTY")

    assert quote["ltp"] == 23280.0
    assert quote["source"] == "fyers_ws"


def test_index_quote_recomputes_ws_change_from_fyers_previous_close(monkeypatch):
    provider = _provider(monkeypatch)
    # Freeze the clock. The tick is staged 0.25s old against a 6s Angel-authoritative
    # window (_select_index_ws_quote), but the code re-reads time.time() itself — so
    # any stall between the two reads (the suite runs a sqlite VACUUM) aged the tick
    # past 6s and failed the test intermittently. Nothing here is about elapsed time.
    now = time.time()
    monkeypatch.setattr(market_data_module.time, "time", lambda: now)
    provider._ws_cache["99926009"] = {
        "ltp": 58429.25,
        "time": now - 0.25,
        "source": "kite_ws",
    }
    provider._prev_close_global_cache = {"BANKNIFTY": 57938.50}
    provider._prev_close_cache_time = {"BANKNIFTY": now}
    provider._prev_close_cache_source = {"BANKNIFTY": "kite_rest"}

    class FakeFyers:
        def quotes(self, payload):
            assert payload == {"symbols": FYERS_INDEX_SYMBOLS["BANKNIFTY"]}
            return {
                "s": "ok",
                "d": [{"v": {"lp": 58461.35, "ch": 169.85, "chp": 0.29, "prev_close_price": 58291.50}}],
            }

    provider.fyers = FakeFyers()

    quote = provider.get_index_quote("BANKNIFTY")

    assert quote["ltp"] == 58429.25
    assert quote["source"] == "kite_ws"
    assert quote["previous_close"] == 58291.50
    assert quote["change"] == pytest.approx(137.75)
    assert quote["change_pct"] == pytest.approx(137.75 / 58291.50 * 100.0)


def test_previous_close_prefers_fyers_index_quote_over_stale_angel_cache(monkeypatch):
    provider = _provider(monkeypatch)
    now = time.time()
    provider._prev_close_global_cache = {"BANKNIFTY": 57938.50}
    provider._prev_close_cache_time = {"BANKNIFTY": now}
    provider._prev_close_cache_source = {"BANKNIFTY": "kite_rest"}
    provider.is_connected = True

    class StaleAngel:
        def ltpData(self, exchange, symbol, token):
            return {"status": True, "data": {"close": 57938.50}}

    class FakeFyers:
        def quotes(self, payload):
            assert payload == {"symbols": FYERS_INDEX_SYMBOLS["BANKNIFTY"]}
            return {"s": "ok", "d": [{"v": {"lp": 58461.35, "prev_close_price": 58291.50}}]}

    provider.smart_api = StaleAngel()
    provider.fyers = FakeFyers()

    assert provider.get_previous_close("BANKNIFTY") == 58291.50
    assert provider._prev_close_global_cache["BANKNIFTY"] == 58291.50
    assert provider._prev_close_cache_source["BANKNIFTY"] == "fyers_rest"


def test_preopen_index_quote_prefers_fyers_rest_over_fresh_reference_angel_ws(monkeypatch):
    provider = _provider(monkeypatch)
    now = time.time()
    provider._ws_cache["99926000"] = {
        "ltp": 23200.0,
        "time": now - 0.5,
        "source": "kite_ws",
    }
    monkeypatch.setattr(provider, "get_market_session_label", lambda dt=None: "PRE-OPEN")
    monkeypatch.setattr(provider, "_preopen_rest_quote_allowed", lambda *args, **kwargs: True)

    class FakeFyers:
        calls = 0

        def quotes(self, payload):
            self.calls += 1
            assert payload == {"symbols": FYERS_INDEX_SYMBOLS["NIFTY"]}
            return {"s": "ok", "d": [{"v": {"lp": 23284.5, "ch": 84.5, "chp": 0.36}}]}

    provider.fyers = FakeFyers()

    quote = provider.get_index_quote("NIFTY")

    assert quote["ltp"] == 23284.5
    assert quote["change"] == 84.5
    assert quote["change_pct"] == 0.36
    assert quote["source"] == "fyers_rest_preopen"
    assert quote["quote_label"] == "PRE-OPEN"
    assert quote["is_preopen"] is True
    assert provider.fyers.calls == 1


def test_preopen_rest_quote_allowed_ends_at_configured_settlement_time(monkeypatch):
    provider = _provider(monkeypatch)

    assert provider._preopen_rest_quote_allowed(
        datetime(2026, 6, 16, 9, 7, 59),
        "PRE-OPEN",
    ) is True
    assert provider._preopen_rest_quote_allowed(
        datetime(2026, 6, 16, 9, 8, 0),
        "PRE-OPEN",
    ) is True
    assert provider._preopen_rest_quote_allowed(
        datetime(2026, 6, 16, 9, 8, 1),
        "PRE-OPEN",
    ) is False
    assert provider._preopen_rest_quote_allowed(
        datetime(2026, 6, 16, 9, 7, 59),
        "LIVE",
    ) is False


def test_preopen_index_quote_uses_fresh_ws_after_settlement(monkeypatch):
    provider = _provider(monkeypatch)
    now = time.time()
    provider._ws_cache["99926000"] = {
        "ltp": 23295.0,
        "change": 95.0,
        "change_pct": 0.41,
        "time": now - 0.5,
        "source": "kite_ws",
    }
    monkeypatch.setattr(provider, "get_market_session_label", lambda dt=None: "PRE-OPEN")
    monkeypatch.setattr(provider, "_preopen_rest_quote_allowed", lambda *args, **kwargs: False)

    class ShouldNotCallFyers:
        def quotes(self, payload):
            raise AssertionError("Fyers pre-open REST should stop after settlement")

    provider.fyers = ShouldNotCallFyers()

    quote = provider.get_index_quote("NIFTY")

    assert quote["ltp"] == 23295.0
    assert quote["change"] == 95.0
    assert quote["change_pct"] == 0.41
    assert quote["source"] == "kite_ws"
    assert quote["quote_label"] == "PRE-OPEN"
    assert quote["is_preopen"] is True
    assert quote["preopen_settlement"] is False


def test_price_bridge_uses_yahoo_only_after_both_brokers_fail(monkeypatch):
    provider = _provider(monkeypatch)

    class FakeTicker:
        fast_info = SimpleNamespace(last_price=23111.5)

    monkeypatch.setattr("data.market_data.yf.Ticker", lambda ticker: FakeTicker())

    price = provider.get_latest_price_by_token("99926000", "NIFTY", "NSE")

    assert price == 23111.5
    assert provider._yahoo_request_count == 1
    assert provider._ltp_cache["NIFTY"]["source"] == "yahoo"


def test_angel_rest_rate_limit_enters_backoff_and_skips_next_call(monkeypatch):
    provider = _provider(monkeypatch)
    provider.is_connected = True
    provider._ltp_cache["NIFTY"] = {
        "ltp": 23000.0,
        "time": datetime.now() - timedelta(seconds=10),
        "source": "angel",
    }

    class RateLimitedApi:
        calls = 0

        def ltpData(self, *args, **kwargs):
            self.calls += 1
            return {"status": False, "message": "Access denied because of exceeding access rate"}

    api = RateLimitedApi()
    provider.smart_api = api
    monkeypatch.setattr(provider, "_throttle_angel_rest", lambda context: True)
    monkeypatch.setattr("data.market_data.yf.Ticker", lambda ticker: (_ for _ in ()).throw(AssertionError("Yahoo should not be needed")))

    price = provider.get_latest_price_by_token("99926000", "NIFTY", "NSE")

    assert price == 23000.0
    assert api.calls == 1
    assert provider._angel_rest_backoff_until > time.time()

    class ShouldNotCallApi:
        def ltpData(self, *args, **kwargs):
            raise AssertionError("Angel REST should be skipped while backoff is active")

    provider.smart_api = ShouldNotCallApi()
    monkeypatch.setattr(provider, "_throttle_angel_rest", lambda context: provider._angel_rest_ready(context))

    assert provider.get_latest_price_by_token("99926000", "NIFTY", "NSE") == 23000.0


def test_broker_position_rate_limit_uses_cooldown():
    class RateLimitedPositions:
        calls = 0

        def position(self):
            self.calls += 1
            return {"status": False, "message": "Access denied because of exceeding access rate"}

    api = RateLimitedPositions()
    broker = SmartApiBroker(api)

    assert broker.get_positions() is None
    assert api.calls == 1
    assert broker._position_backoff_until > time.time()

    api.position = lambda: (_ for _ in ()).throw(AssertionError("position should be skipped"))
    assert broker.get_positions() is None


def test_stale_index_websocket_tick_falls_through_to_fyers_rest(monkeypatch):
    provider = _provider(monkeypatch)
    provider._ws_cache["99926000"] = {
        "ltp": 23000.0,
        "time": time.time() - 60,
        "source": "kite_ws",
    }

    class FakeFyers:
        def quotes(self, payload):
            return {"s": "ok", "d": [{"v": {"lp": 23123.5}}]}

    provider.fyers = FakeFyers()

    price = provider.get_latest_price_by_token("99926000", "NIFTY", "NSE")

    assert price == 23123.5


def test_scanner_blocks_new_entries_on_context_only_sources_but_keeps_exits():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._diagnostics = {"source_fallback": {"delayed_entry_blocked": 0}}
    scanner._diagnostic_unique_events = set()
    entry = SimpleNamespace(
        action="ENTRY",
        timeframe="5min",
        signal_timestamp=123,
        direction="LONG",
    )
    exit_signal = SimpleNamespace(
        action="EXIT",
        timeframe="5min",
        signal_timestamp=124,
        direction="LONG",
    )

    allowed = Scanner._filter_live_candidates_by_source(
        scanner,
        "NIFTY",
        [entry, exit_signal],
        {"entry_eligible": False},
    )

    assert allowed == [exit_signal]
    assert scanner._diagnostics["source_fallback"]["delayed_entry_blocked"] == 1


def test_scanner_allows_entries_when_angel_order_authority_is_fresh():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._diagnostics = {"source_fallback": {"delayed_entry_blocked": 0}}
    candidate = SimpleNamespace(action="ENTRY")

    allowed = Scanner._filter_live_candidates_by_source(
        scanner,
        "NIFTY",
        [candidate],
        {"entry_eligible": True},
    )

    assert allowed == [candidate]
