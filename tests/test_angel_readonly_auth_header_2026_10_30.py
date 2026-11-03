"""Regression lock for the Angel One REST ``Authorization`` header.

``generateSession`` answers with ``data.jwtToken`` already prefixed ("Bearer eyJ...")
while SmartApi's own ``api.access_token`` is the bare token. ``_angel_post`` used to
build ``"Bearer " + self._jwt_token`` unconditionally, so the wire header was
"Bearer Bearer eyJ...", which Angel One answers with HTTP 200 + AG8001 "Invalid
Token" on every route. ``get_full_quotes`` read AG8001 as a dead session and forced a
re-authentication every ~2s (the observed online/offline flap) while no quote or
candle ever landed. These tests pin the header to exactly one prefix.
"""

import pytest

from data.angelone_market_data import AngelOneIndexQuoteSource


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _CapturingSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, *, data=None, headers=None, timeout=None, proxies=None):
        self.calls.append({"url": url, "data": data, "headers": dict(headers or {})})
        return _FakeResponse(self.payload)


class _FakeApi:
    def __init__(self, *, access_token="", prefix_duplicated=False):
        self.access_token = access_token
        self._prefix_duplicated = prefix_duplicated
        self.proxies = None

    def requestHeaders(self):
        auth = self.access_token
        if self._prefix_duplicated:
            auth = f"Bearer {auth}"
        return {"Authorization": auth, "X-PrivateKey": "test-key"}


def _source(monkeypatch, session, *, jwt_token, access_token, prefix_duplicated=False):
    source = AngelOneIndexQuoteSource()
    source._jwt_token = jwt_token
    source._api = _FakeApi(access_token=access_token, prefix_duplicated=prefix_duplicated)
    monkeypatch.setattr("data.angelone_readonly._pooled_session", lambda: session)
    return source


def test_prefixed_jwt_is_sent_with_exactly_one_bearer(monkeypatch):
    payload = {"status": True, "message": "SUCCESS", "data": {"fetched": []}}
    session = _CapturingSession(payload)
    source = _source(
        monkeypatch,
        session,
        jwt_token="Bearer eyJ-app",
        access_token="eyJ-app",
        prefix_duplicated=True,
    )

    result = source._angel_post("api.market.data", {"mode": "FULL"}, lambda: {})

    assert result == payload
    header = session.calls[0]["headers"]["Authorization"]
    assert header == "Bearer eyJ-app"
    assert "Bearer Bearer" not in header


def test_bare_jwt_is_also_sent_with_exactly_one_bearer(monkeypatch):
    payload = {"status": True, "data": {"fetched": []}}
    session = _CapturingSession(payload)
    source = _source(monkeypatch, session, jwt_token="eyJ-bare", access_token="eyJ-bare")

    source._angel_post("api.market.data", {"mode": "FULL"}, lambda: {})

    assert session.calls[0]["headers"]["Authorization"] == "Bearer eyJ-bare"


def test_missing_jwt_falls_back_to_sdk_access_token(monkeypatch):
    payload = {"status": True, "data": {"fetched": []}}
    session = _CapturingSession(payload)
    source = _source(monkeypatch, session, jwt_token="", access_token="eyJ-sdk")

    source._angel_post("api.market.data", {"mode": "FULL"}, lambda: {})

    assert session.calls[0]["headers"]["Authorization"] == "Bearer eyJ-sdk"


def test_pooled_transport_failure_falls_back_to_sdk_call(monkeypatch):
    class _BrokenSession:
        def post(self, *args, **kwargs):
            raise OSError("connection reset")

    sentinel = {"status": True, "data": {"fetched": [{"symbolToken": "99919000"}]}}
    source = _source(monkeypatch, _BrokenSession(), jwt_token="Bearer eyJ-app", access_token="eyJ-app")

    result = source._angel_post("api.market.data", {"mode": "FULL"}, lambda: sentinel)

    assert result is sentinel


def test_error_type_payload_raises_like_the_sdk(monkeypatch):
    session = _CapturingSession({"error_type": "AG8001", "message": "Invalid Token"})
    source = _source(monkeypatch, session, jwt_token="Bearer eyJ-app", access_token="eyJ-app")

    with pytest.raises(RuntimeError):
        source._angel_post("api.market.data", {"mode": "FULL"}, lambda: {})
