"""Kite session lifecycle: expiry must be detected before a trading day, not during it.

Kite issues an access token that dies every morning. If the system only discovers that
on the first order of the session, the day is already lost — which is exactly how the
AngelOne AG7002 outage played out on 2026-07-31. The token cache is therefore validated
against the morning cutoff at load time, and a token belonging to a different api_key
(regenerated app, swapped credentials) is rejected rather than used.

The account password never appears in this module: Kite has no password-based API
login, so storing one would be liability with no capability.
"""
from datetime import datetime, timedelta

import json

import pytest
import pytz

from data.kite_auth import IST, KiteAuth, most_recent_token_cutoff

MORNING = datetime(2026, 7, 31, 9, 15)
BEFORE_DAWN = datetime(2026, 7, 31, 5, 30)


def _auth(tmp_path, api_key="APIKEY123", secret="SECRET456"):
    return KiteAuth(api_key=api_key, api_secret=secret, token_file=tmp_path / "kite_token.json")


def _write_token(auth, *, issued_at, api_key="APIKEY123", access_token="AT-1"):
    auth.token_file.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "user_id": "JOQ915",
                "issued_at": IST.localize(issued_at).isoformat(),
                "api_key": api_key,
            }
        ),
        encoding="utf-8",
    )


def test_cutoff_is_this_morning_during_market_hours():
    cutoff = most_recent_token_cutoff(IST.localize(MORNING))
    assert cutoff.date() == MORNING.date() and cutoff.hour == 6


def test_cutoff_rolls_back_a_day_before_dawn():
    """A 05:30 restart must still accept last night's token."""
    cutoff = most_recent_token_cutoff(IST.localize(BEFORE_DAWN))
    assert cutoff.date() == (BEFORE_DAWN - timedelta(days=1)).date()


def _verdict(monkeypatch, value, reason=""):
    """Control what Kite says about the cached token."""
    monkeypatch.setattr(KiteAuth, "_verify_live", staticmethod(lambda client: (value, reason)))


def test_a_token_kite_accepts_is_used(tmp_path, monkeypatch):
    auth = _auth(tmp_path)
    _write_token(auth, issued_at=datetime(2026, 7, 31, 8, 45))
    _verdict(monkeypatch, True)
    assert auth.load_cached_session() is True
    assert auth.status()["auth_required"] is False
    assert auth.kite is not None


def test_a_token_kite_rejects_forces_a_fresh_login(tmp_path, monkeypatch):
    """The clock said this token was fine; Kite is the authority, not the clock.

    A token issued at 04:48 was declared dead at 06:16 the same morning by the old
    cutoff heuristic, forcing a second login before the open — while a genuinely
    revoked token passed the same check because it sat on the right side of the line.
    """
    auth = _auth(tmp_path)
    _write_token(auth, issued_at=datetime(2026, 7, 31, 8, 45))   # "fresh" by the clock
    _verdict(monkeypatch, False, "Kite rejected the cached token: Invalid `access_token`")
    assert auth.load_cached_session() is False
    assert auth.status()["auth_required"] is True
    assert "rejected" in auth.status()["reason"]
    assert auth.kite is None


def test_an_early_morning_token_survives_the_same_day_cutoff(tmp_path, monkeypatch):
    """Issued 04:48, loaded 06:16 — the old rule killed it; Kite says it is alive."""
    auth = _auth(tmp_path)
    _write_token(auth, issued_at=datetime(2026, 8, 1, 4, 48))
    _verdict(monkeypatch, True)
    assert auth.load_cached_session() is True
    assert auth.status()["auth_required"] is False


def test_unreachable_kite_keeps_a_plausible_token(tmp_path, monkeypatch):
    """A network blip must not demand a browser login nobody is there to complete."""
    auth = _auth(tmp_path)
    _write_token(auth, issued_at=datetime(2026, 7, 31, 8, 45))
    monkeypatch.setattr(
        "data.kite_auth.most_recent_token_cutoff",
        lambda now=None: IST.localize(datetime(2026, 7, 31, 6, 0)),
    )
    _verdict(monkeypatch, None, "NetworkException: timed out")
    assert auth.load_cached_session() is True


def test_unreachable_kite_still_refuses_an_obviously_stale_token(tmp_path, monkeypatch):
    auth = _auth(tmp_path)
    _write_token(auth, issued_at=datetime(2026, 7, 30, 9, 0))
    monkeypatch.setattr(
        "data.kite_auth.most_recent_token_cutoff",
        lambda now=None: IST.localize(datetime(2026, 7, 31, 6, 0)),
    )
    _verdict(monkeypatch, None, "NetworkException: timed out")
    assert auth.load_cached_session() is False
    assert "unreachable" in auth.status()["reason"]


def test_token_for_a_different_api_key_is_refused(tmp_path):
    """A regenerated app leaves a token that authenticates as nothing."""
    auth = _auth(tmp_path, api_key="NEWKEY999")
    _write_token(auth, issued_at=datetime(2026, 7, 31, 8, 45), api_key="OLDKEY111")
    assert auth.load_cached_session() is False
    assert "different api_key" in auth.status()["reason"]


def test_missing_cache_is_reported_not_crashed(tmp_path):
    auth = _auth(tmp_path)
    assert auth.load_cached_session() is False
    assert auth.status()["auth_required"] is True


def test_corrupt_cache_is_reported_not_crashed(tmp_path):
    auth = _auth(tmp_path)
    auth.token_file.write_text("{not json", encoding="utf-8")
    assert auth.load_cached_session() is False
    assert "unreadable" in auth.status()["reason"]


def test_missing_credentials_are_named(tmp_path):
    assert KiteAuth("", "", tmp_path / "t.json").missing_credentials() == [
        "KITE_API_KEY",
        "KITE_API_SECRET",
    ]
    with pytest.raises(ValueError) as err:
        KiteAuth("", "", tmp_path / "t.json").login_url()
    assert "KITE_API_KEY" in str(err.value)


def test_completed_login_persists_a_token_without_the_secret(tmp_path, monkeypatch):
    auth = _auth(tmp_path)

    class _Client:
        # Mirrors the real KiteConnect signature rather than swallowing **kwargs, so a
        # wrong argument name here still fails instead of being silently absorbed.
        # proxies is what routes order traffic through a fixed address when the operator
        # configures one; a client built without it would quietly bypass that.
        def __init__(self, api_key=None, access_token=None, root=None, debug=False,
                     timeout=None, proxies=None, pool=None, disable_ssl=False):
            self.api_key = api_key
            self.proxies = proxies
        def generate_session(self, request_token, api_secret):
            assert request_token == "RT-1" and api_secret == "SECRET456"
            return {"access_token": "AT-NEW", "user_id": "JOQ915", "public_token": "PT"}
        def set_access_token(self, token): self.token = token

    monkeypatch.setattr("kiteconnect.KiteConnect", _Client)
    result = auth.complete_login("RT-1")

    assert result["user_id"] == "JOQ915"
    cached = json.loads(auth.token_file.read_text(encoding="utf-8"))
    assert cached["access_token"] == "AT-NEW"
    # The api_secret and any password must never reach disk.
    body = auth.token_file.read_text(encoding="utf-8")
    assert "SECRET456" not in body
    assert auth.status()["auth_required"] is False


def test_invalidate_forces_a_relogin(tmp_path, monkeypatch):
    auth = _auth(tmp_path)
    _write_token(auth, issued_at=datetime(2026, 7, 31, 8, 45))
    _verdict(monkeypatch, True)
    assert auth.load_cached_session() is True
    auth.invalidate("TokenException: Invalid access token")
    assert auth.kite is None
    assert auth.status()["auth_required"] is True
