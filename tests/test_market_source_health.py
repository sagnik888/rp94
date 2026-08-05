from types import SimpleNamespace

from data.market_data import MarketDataProvider
from data.market_source_health import build_market_source_snapshot, build_source_health


TOKENS = {"NIFTY": ("99926000", "NSE")}
FYERS = {"NIFTY": "NSE:NIFTY50-INDEX"}


def _provider():
    return SimpleNamespace(
        _ws_cache={},
        _ltp_cache={},
        _last_angel_ws_tick=0.0,   # retired; Kite health is session-based
        _last_fyers_ws_tick=0.0,
        _fyers_auth_required=False,
        _fyers_auth_reason="",
        _fyers_refresh_disabled=False,
        _yahoo_request_count=0,
        _yahoo_last_used_at=None,
        is_connected=False,
        fyers=None,
        is_market_open=lambda: True,
    )


def test_source_health_uses_injected_clock_at_freshness_boundary():
    provider = _provider()
    provider.is_connected = True
    provider.fyers = object()
    provider._last_kite_ws_tick = 999.0
    provider._last_fyers_ws_tick = 955.0

    health = build_source_health(provider, now_ts=1000.0, freshness_threshold=45.0)

    assert health["kite"] is True   # session-based: is_connected means a live token
    assert health["fyers"] is False
    assert health["broker_degraded"] is True
    assert health["all_brokers_unavailable"] is False
    assert health["yahoo_mode"] == "idle_standby"


def test_a_live_token_alone_no_longer_keeps_the_emergency_path_shut():
    """The Kite half of this used to be `is_connected`, which is never revoked.

    Same fixture as above minus the Kite tick: a valid token, nothing arriving on
    either socket, no REST fill. That is genuinely no live price source, and it used
    to report all_brokers_unavailable=False — leaving the preflight blocker and the
    Yahoo emergency path unreachable for the life of the process.
    """
    provider = _provider()
    provider.is_connected = True
    provider.fyers = object()
    provider._last_fyers_ws_tick = 955.0
    provider._provider_started_at = 500.0     # up for 500s: we HAVE waited

    health = build_source_health(provider, now_ts=1000.0, freshness_threshold=45.0)

    assert health["kite"] is True            # orders would still go through
    assert health["kite_stream_stalled"] is True
    assert health["kite_data_fresh"] is False
    assert health["all_brokers_unavailable"] is True


def test_snapshot_keeps_only_kite_fresh_data_entry_eligible():
    provider = _provider()
    provider._ws_cache["99926000"] = {
        "ltp": 23000.0,
        "time": 999.0,
        "source": "kite_ws",
    }
    provider._ws_cache[FYERS["NIFTY"]] = {
        "ltp": 23001.0,
        "time": 1000.0,
        "source": "fyers_ws",
    }

    snapshot = build_market_source_snapshot(
        provider,
        "NIFTY",
        TOKENS,
        FYERS,
        now_ts=1000.0,
    )

    assert snapshot["entry_eligible"] is True
    assert snapshot["freshest"]["source"] == "kite_ws"
    fyers = next(row for row in snapshot["sources"] if row["source"] == "fyers_ws")
    assert fyers["order_authority"] is False
    assert fyers["entry_eligible"] is False


def test_snapshot_always_marks_yahoo_delayed_and_context_only():
    provider = _provider()
    provider._ltp_cache["NIFTY"] = {
        "ltp": 22950.0,
        "time": 1000.0,
        "source": "yahoo",
    }

    snapshot = build_market_source_snapshot(
        provider,
        "NIFTY",
        TOKENS,
        FYERS,
        now_ts=1000.0,
    )

    assert snapshot["entry_eligible"] is False
    assert snapshot["freshest"]["source"] == "yahoo"
    assert snapshot["freshest"]["latency_class"] == "DELAYED"
    assert snapshot["freshest"]["order_authority"] is False


def test_fyers_ws_reconnect_restarts_when_token_file_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fyers_token.json").write_text('{"access_token": "new-token"}', encoding="ascii")
    provider = MarketDataProvider.__new__(MarketDataProvider)
    restarted = []
    provider.start_fyers_websocket = lambda token: restarted.append(token)

    assert provider._fyers_ws_token_ready_for_reconnect("old-token", 1) is False
    assert restarted == ["new-token"]


def test_fyers_gateway_timeout_is_transient_with_capped_backoff():
    error = "Handshake status 504 Gateway Time-out -+-+- {'server': 'cloudflare'}"

    assert MarketDataProvider._fyers_ws_gateway_timeout(error) is True
    assert MarketDataProvider._fyers_ws_gateway_timeout("invalid token") is False
    assert MarketDataProvider._fyers_ws_backoff_seconds(1) == 2
    assert MarketDataProvider._fyers_ws_backoff_seconds(2) == 4
    assert MarketDataProvider._fyers_ws_backoff_seconds(8) == 60


def test_fyers_ws_uses_single_outer_reconnect_loop_and_tick_freshness(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UT1_FYERS_LOG_DIR", "logs/fyers")
    monkeypatch.setattr("data.market_data.FYERS_LOGS_READY", False)

    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._fyers_ws_generation = 0
    provider._fyers_ws = None
    provider._last_fyers_ws_tick = 123.0
    provider._last_ws_tick = 999.0
    provider._fyers_auth_required = False
    provider._fyers_auth_reason = ""
    provider.is_market_open = lambda: True

    captured = {}

    class FakeFyersDataSocket:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.subscribed = []

        def connect(self):
            provider._fyers_ws_auth_failed = True

        def close(self):
            captured["closed"] = True

        def subscribe(self, symbols):
            self.subscribed = list(symbols)

    monkeypatch.setattr("data.market_data.data_ws.FyersDataSocket", FakeFyersDataSocket)

    provider.start_fyers_websocket("token")

    assert captured["reconnect"] is False
    assert captured["log_path"].replace("\\", "/").endswith("logs/fyers/")
    assert provider._last_fyers_ws_tick == 0.0
    assert provider._last_ws_tick == 999.0

    provider._fyers_ws.on_open()

    assert provider._fyers_ws_connected is True
    assert provider._last_fyers_ws_tick == 0.0
    assert provider._last_ws_tick == 999.0
    assert provider._fyers_ws.subscribed


def test_index_tick_volume_extraction_finds_alternate_provider_field():
    provider = MarketDataProvider.__new__(MarketDataProvider)

    vol, key, fields = provider._extract_index_tick_volume(
        {"symbol": "NSE:NIFTY50-INDEX", "vol_traded_today": 0, "volume": 12345},
        ("vol_traded_today", "volume"),
    )

    assert vol == 12345
    assert key == "volume"
    assert fields["vol_traded_today"] == 0
    assert fields["volume"] == 12345


def test_index_tick_volume_diagnostics_tracks_unavailable_volume_and_ticks():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._index_tick_volume_diag = {}

    provider._record_index_tick_volume_diag(
        "NIFTY",
        "fyers_ws",
        0.0,
        "vol_traded_today",
        {"vol_traded_today": 0.0},
        now_ts=120.0,
    )
    provider._record_index_tick_volume_diag(
        "NIFTY",
        "fyers_ws",
        0.0,
        "vol_traded_today",
        {"vol_traded_today": 0.0},
        now_ts=125.0,
    )

    diag = provider.get_index_tick_volume_diagnostics()["NIFTY"]
    assert diag["volume_available"] is False
    assert diag["volume_reliable"] is False
    assert diag["volume_key"] == "vol_traded_today"
    assert diag["tick_count_current_minute"] == 2


def test_fyers_ws_reconnect_refreshes_invalid_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fyers_token.json").write_text(
        '{"access_token": "stale-token", "refresh_token": "refresh-me"}',
        encoding="ascii",
    )

    class FakeFyersModel:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_profile(self):
            return {"s": "error", "code": -8}

    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._fyers_ws_generation = 1
    provider._fyers_refresh_disabled = False
    provider._fyers_auth_required = False
    provider._fyers_auth_reason = ""
    refreshed = []
    provider._refresh_fyers_token = lambda token_data: refreshed.append(token_data["refresh_token"]) or True
    monkeypatch.setattr("data.market_data.fyersModel.FyersModel", FakeFyersModel)

    assert provider._fyers_ws_token_ready_for_reconnect("stale-token", 1) is False
    assert refreshed == ["refresh-me"]


def test_fyers_load_preserves_missing_pin_refresh_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fyers_token.json").write_text(
        '{"access_token": "old-token", "refresh_token": "refresh-me"}',
        encoding="ascii",
    )
    monkeypatch.setattr(
        "config.settings.get_settings",
        lambda: SimpleNamespace(
            fyers_app_id="APP-100",
            fyers_secret_key="secret",
            fyers_password="",
        ),
    )

    class FakeFyersModel:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_profile(self):
            return {"s": "error", "code": -300}

    monkeypatch.setattr("data.market_data.fyersModel.FyersModel", FakeFyersModel)
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.start_streams = False
    provider._fyers_refresh_disabled = False
    provider._fyers_auth_required = False
    provider._fyers_auth_reason = ""
    provider.fyers = None

    provider._load_fyers_token()

    assert provider.fyers is None
    assert provider._fyers_auth_required is True
    assert "FYERS_PASSWORD" in provider._fyers_auth_reason
