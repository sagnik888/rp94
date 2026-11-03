"""A Kite session reset that answers with HTTP 403 "Insufficient permission" (2026-09-25).

Timeline: a 03:55 login was reset at 04:30 (TokenException, handled). The operator logged
in again at 04:49; around 05:05 Kite reset that session too, but this time every call -
ltp, historical, profile and the ticker's WebSocket upgrade - came back as
PermissionException / 403. Only TokenException was treated as a dead session, so:

- the running process logged "Kite ltp failed: PermissionException" every minute and
  never raised "Kite login required";
- the 05:20 restart verified the cached token with profile(), got the same refusal, and
  kept the token "provisionally" - Kite reported connected, the broker "LIVE Ready";
- the ticker retried the refused upgrade forever.

A 403 on one API is also what an app gets for an add-on it lacks, on a healthy session,
so a refusal is settled by profile(), which every session may call.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from data.kite_auth import IST, KiteAuth, is_kite_permission_refusal
from data.kite_market_data import KiteDataSource
from data.market_data import MarketDataProvider


class PermissionException(Exception):
    pass


class NetworkException(Exception):
    pass


PERMISSION = "Insufficient permission for that call."


class _Client:
    def __init__(self, profile_error=None, ltp_error=None):
        self.profile_error = profile_error
        self.ltp_error = ltp_error
        self.profile_calls = 0
        self.access_token = "AT-1"

    def set_access_token(self, token):
        self.access_token = token

    def profile(self):
        self.profile_calls += 1
        if self.profile_error is not None:
            raise self.profile_error
        return {"user_id": "JOQ915"}

    def ltp(self, keys):
        if self.ltp_error is not None:
            raise self.ltp_error
        return {k: {"last_price": 100.0} for k in keys}


def _auth(tmp_path):
    return KiteAuth(api_key="APIKEY123", api_secret="SECRET456", token_file=tmp_path / "kite_token.json")


def _write_token(auth, issued_at):
    import json

    auth.token_file.write_text(json.dumps({
        "access_token": "AT-1", "user_id": "JOQ915",
        "issued_at": IST.localize(issued_at).isoformat(), "api_key": "APIKEY123",
    }), encoding="utf-8")


def _patch_client(monkeypatch, client):
    monkeypatch.setattr("kiteconnect.KiteConnect", lambda **_kwargs: client)


def test_a_permission_refusal_is_recognised_by_its_type():
    assert is_kite_permission_refusal(PermissionException(PERMISSION)) is True
    assert is_kite_permission_refusal(NetworkException("timed out")) is False


def test_a_restart_does_not_keep_a_token_kite_refuses_with_a_403(tmp_path, monkeypatch):
    auth = _auth(tmp_path)
    _write_token(auth, datetime.now(IST).replace(tzinfo=None))
    _patch_client(monkeypatch, _Client(profile_error=PermissionException(PERMISSION)))
    assert auth.load_cached_session() is False
    status = auth.status()
    assert status["auth_required"] is True and "Insufficient permission" in status["reason"]
    assert not auth.token_file.exists()                      # a dead token is not reused


def test_a_network_error_still_keeps_a_plausible_token_provisionally(tmp_path, monkeypatch):
    auth = _auth(tmp_path)
    _write_token(auth, datetime.now(IST).replace(tzinfo=None))
    _patch_client(monkeypatch, _Client(profile_error=NetworkException("read timed out")))
    assert auth.load_cached_session() is True
    assert auth.status()["auth_required"] is False


def _live_session(tmp_path, client):
    auth = _auth(tmp_path)
    auth._kite = client
    auth._auth_required = False
    auth._auth_reason = ""
    return auth


def test_a_session_kite_reset_is_declared_dead_on_the_first_refused_call(tmp_path):
    client = _Client(profile_error=PermissionException(PERMISSION), ltp_error=PermissionException(PERMISSION))
    auth = _live_session(tmp_path, client)
    src = KiteDataSource(auth=auth)
    assert src.get_ltp("NFO", "NIFTY26SEP23100PE") is None
    assert auth.status()["auth_required"] is True             # login required is raised
    assert auth.kite is None


def test_one_missing_api_permission_does_not_kill_a_healthy_session(tmp_path):
    client = _Client(profile_error=None, ltp_error=PermissionException(PERMISSION))
    auth = _live_session(tmp_path, client)
    src = KiteDataSource(auth=auth)
    assert src.get_ltp("NFO", "X") is None
    assert src.get_ltp("NFO", "X") is None
    assert auth.status()["auth_required"] is False and auth.kite is client
    assert client.profile_calls == 1                           # one probe per minute, not per call


class _Ticker:
    def __init__(self):
        self.stopped = False
        self.closed = False

    def stop_retry(self):
        self.stopped = True

    def close(self, *a, **k):
        self.closed = True


def _provider_with(auth):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.kite = SimpleNamespace(_auth=auth)
    provider._kite_ws_connected = True
    return provider


def test_a_refused_ticker_upgrade_stops_the_reconnect_loop_when_the_session_is_dead(tmp_path):
    auth = _live_session(tmp_path, _Client(profile_error=PermissionException(PERMISSION)))
    provider, ws = _provider_with(auth), _Ticker()
    assert provider._on_kite_ticker_forbidden(ws, "WebSocket connection upgrade failed (403 - Forbidden)") is True
    assert ws.stopped is True and auth.status()["auth_required"] is True
    assert provider._kite_ws_connected is False


def test_a_403_with_a_healthy_session_leaves_the_ticker_retrying(tmp_path):
    auth = _live_session(tmp_path, _Client(profile_error=None))
    provider, ws = _provider_with(auth), _Ticker()
    assert provider._on_kite_ticker_forbidden(ws, "403 - Forbidden") is False
    assert ws.stopped is False and auth.status()["auth_required"] is False


def test_the_live_ticker_hands_a_403_to_the_session_check(monkeypatch):
    import threading

    created = []

    class FakeTicker:
        MODE_FULL = "full"

        def __init__(self, api_key, access_token):
            created.append(self)

        def connect(self, threaded=True):
            return None

    monkeypatch.setattr("kiteconnect.KiteTicker", FakeTicker)
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.start_streams = True
    provider.kite = SimpleNamespace(_auth=SimpleNamespace(api_key="APIKEY123", kite=SimpleNamespace(access_token="AT-1")))
    provider._active_kite_tokens = set()
    provider.kws = None
    seen = threading.Event()
    calls = []

    def forbidden(ws, reason):
        calls.append(reason)
        seen.set()

    provider._on_kite_ticker_forbidden = forbidden
    assert provider.start_kite_ticker() is True
    ticker = created[-1]
    ticker.on_error(ticker, 1006, "connection was closed uncleanly (WebSocket connection upgrade failed (403 - Forbidden))")
    assert seen.wait(2.0) and "403" in calls[0]
    calls.clear()
    seen.clear()
    ticker.on_error(ticker, 1006, "WebSocket closing handshake timeout")      # not a refusal
    assert not seen.wait(0.3) and calls == []
