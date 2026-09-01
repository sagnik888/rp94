"""An expired option contract must not log the operator out of Kite.

Kite spends the word "token" on two unrelated things. `historical_data` takes an
INSTRUMENT token as its first argument and rejects an unknown or expired one with
`InputException: invalid token`; a dead session says "Invalid `access_token`" or raises
TokenException. `is_kite_auth_failure` matched the bare phrase, so the first reading won
and a live session was torn down and its cached token deleted.

That is not a rare shape. main.py's session archiver back-fills contract history for
old sessions every few minutes with force_refresh=True, and those contracts have long
since expired — so on 2026-08-30 every login died within minutes of being issued:

    17:02:05  Kite session established for JOQ915
    17:06:38  Kite session invalidated: historical_data: InputException: invalid token

while the WebSocket — authenticated with that same access token — kept ticking and
reconnected cleanly an hour later. The dashboard KITE badge reads `kite_session_live`,
so it went red on a session that was never actually dead.
"""
import pytest

from data.kite_auth import KiteAuth, is_kite_auth_failure


class InputException(Exception):
    """Shaped like kiteconnect.exceptions.InputException (matched by class name)."""


class TokenException(Exception):
    """Shaped like kiteconnect.exceptions.TokenException."""


class _Client:
    """A Kite client whose historical_data always refuses the instrument token."""

    def __init__(self, error):
        self._error = error

    def historical_data(self, token, start, end, interval, oi=False):
        raise self._error


def _live_auth(tmp_path, error):
    """A KiteAuth holding a good session, wired to a client that will raise `error`."""
    auth = KiteAuth(api_key="APIKEY123", api_secret="SECRET456",
                    token_file=tmp_path / "kite_token.json")
    auth.token_file.write_text("{}", encoding="utf-8")
    client = _Client(error)
    auth._kite = client
    auth._auth_required = False
    auth._auth_failure_latched = False
    return auth


def _fetch(auth):
    from data.kite_market_data import KiteDataSource

    return KiteDataSource(auth=auth).get_historical(12345, "5min")


# ── the defect ───────────────────────────────────────────────────────────────
def test_an_expired_instrument_token_leaves_the_session_alone(tmp_path):
    auth = _live_auth(tmp_path, InputException("invalid token"))

    assert _fetch(auth).empty, "the candle request still fails; only the verdict changes"

    assert auth.kite is not None, "a live session was torn down by an expired contract"
    assert auth.status()["auth_required"] is False
    assert auth.token_file.exists(), "the daily token was deleted and the login lost"


def test_the_dashboard_badge_stays_green_through_it(tmp_path):
    """`kite_session_live` — what the KITE badge reads — is the observable that moved."""
    from data.kite_market_data import KiteDataSource

    auth = _live_auth(tmp_path, InputException("invalid token"))
    source = KiteDataSource(auth=auth)
    assert source.is_connected is True

    source.get_historical(12345, "5min")

    assert source.is_connected is True, "badge goes red on a session that is still alive"


# ── the defect is not fixed by going blind ───────────────────────────────────
def test_a_genuinely_dead_token_still_ends_the_session(tmp_path):
    """TokenException is unambiguous in any context and must still log us out."""
    auth = _live_auth(tmp_path, TokenException("Incorrect `api_key` or `access_token`."))

    assert _fetch(auth).empty
    assert auth.kite is None
    assert auth.status()["auth_required"] is True


def test_history_naming_the_access_token_still_ends_the_session(tmp_path):
    """Kite has emitted InputException for a dead session; that phrasing names it."""
    auth = _live_auth(tmp_path, InputException("Invalid `access_token`"))

    assert _fetch(auth).empty
    assert auth.kite is None
    assert auth.status()["auth_required"] is True


# ── classification, held to the call that produced the error ─────────────────
@pytest.mark.parametrize("context", ["", "place_order", "orders", "profile"])
def test_bare_invalid_token_still_condemns_a_session_elsewhere(context):
    """No order call takes an instrument token, so "invalid token" means the session."""
    assert is_kite_auth_failure(InputException("invalid token"), context) is True


def test_only_the_ambiguous_phrase_is_forgiven_on_the_history_path():
    assert is_kite_auth_failure(InputException("invalid token"), "historical_data") is False
    for message in ("Invalid `access_token`", "Token is invalid or has expired",
                    "The access token has expired", "session expired"):
        assert is_kite_auth_failure(InputException(message), "historical_data") is True, message
    assert is_kite_auth_failure(TokenException("dead"), "historical_data") is True
