from types import SimpleNamespace

from data.market_data import MarketDataProvider


def _provider():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.start_streams = False
    provider.fyers = object()
    provider._fyers_auth_required = False
    provider.is_connected = True
    provider.kite = SimpleNamespace(is_connected=True)
    return provider


def test_replay_provider_starts_both_sockets_when_live_mode_is_enabled(monkeypatch):
    provider = _provider()
    calls = []
    monkeypatch.setattr(
        provider,
        "_read_fyers_token_file",
        lambda: {"access_token": "fresh-token"},
    )
    monkeypatch.setattr(
        provider,
        "start_fyers_websocket",
        lambda token: calls.append(("fyers", token)),
    )
    monkeypatch.setattr(
        provider,
        "start_kite_ticker",
        lambda: calls.append(("kite", None)) or True,
    )

    assert provider.enable_live_streams() is True
    assert provider.start_streams is True
    assert calls == [("kite", None), ("fyers", "fresh-token")]


def test_live_stream_activation_is_idempotent(monkeypatch):
    provider = _provider()
    provider.start_streams = True
    monkeypatch.setattr(
        provider,
        "start_fyers_websocket",
        lambda _token: (_ for _ in ()).throw(AssertionError("duplicate Fyers start")),
    )
    monkeypatch.setattr(
        provider,
        "start_kite_ticker",
        lambda: (_ for _ in ()).throw(AssertionError("duplicate Kite start")),
    )

    assert provider.enable_live_streams() is False


def test_missing_fyers_token_waits_for_login_but_starts_kite(monkeypatch):
    provider = _provider()
    calls = []
    monkeypatch.setattr(provider, "_read_fyers_token_file", lambda: {})
    monkeypatch.setattr(
        provider,
        "start_fyers_websocket",
        lambda _token: calls.append("fyers"),
    )
    monkeypatch.setattr(
        provider,
        "start_kite_ticker",
        lambda: calls.append("kite") or True,
    )

    assert provider.enable_live_streams() is True
    assert calls == ["kite"]
    assert provider.start_streams is True


def test_scanner_live_mode_switch_activates_provider_streams():
    source = open("scanner.py", encoding="utf-8").read()

    assert "if requested_runtime_mode.uses_live_market_data:" in source
    assert 'getattr(self.data, "enable_live_streams", None)' in source


def test_primary_stream_starts_when_backup_connection_flag_is_false(monkeypatch):
    provider = _provider()
    provider.is_connected = False
    calls = []
    monkeypatch.setattr(provider, "_prewarm_angelone_fallback", lambda: None)
    monkeypatch.setattr(provider, "_read_fyers_token_file", lambda: {})
    monkeypatch.setattr(provider, "start_kite_ticker", lambda: calls.append("kite") or True)
    assert provider.enable_live_streams() is True
    assert calls == ["kite"]
