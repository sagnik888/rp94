"""A throttled Kite endpoint was re-hit at full rate on the very next scan tick.

Every Kite data REST call funnels through KiteDataSource._call, which was:

    try:
        return fn(api)
    except Exception as exc:
        logger.warning(...)
        return default

So a 429 became an empty DataFrame, the scanner saw "no candles" rather than an error,
and nothing recorded that the endpoint was refusing — the identical burst re-fired on the
next tick, keeping the broker's window shut. AngelOne had exactly this protection
(_mark_angel_rest_backoff plus the SharedApiRateLimiter stack); the migration removed the
broker and left the rate-limit layer wired to dead code behind a permanently-None
smart_api.

DETECTION IS DELIBERATELY NARROW. Backing off on the wrong exception starves the data
path, which is worse than the hammering this stops, so anything unrecognised is NOT a
throttle and behaves exactly as before.

The cooldown is per ENDPOINT, not global: a throttled historical feed must not silence
quotes. Base 1s doubling to 8s, against Kite's per-second limits — the Angel figures
(12s/60s) would idle whole scan cycles to clear a sub-second window.
"""
import time
from types import SimpleNamespace

import pytest

from data.kite_market_data import KiteDataSource


class _Boom(Exception):
    pass


def _src():
    s = KiteDataSource(auth=None, kite=SimpleNamespace(name="stub"))
    return s


def _throttle(msg="Too many requests"):
    return _Boom(msg)


# ── what counts as a throttle ────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "Too many requests",
    "TOO MANY REQUESTS",
    "rate limit exceeded",
    "429 Client Error",
    "request throttled, slow down",
])
def test_a_rate_limit_refusal_is_recognised(msg):
    assert KiteDataSource._is_throttle(_Boom(msg)) is True


@pytest.mark.parametrize("msg", [
    "Incorrect api_key or access_token",
    "no data",
    "connection reset by peer",
    "Insufficient funds",
    "",
])
def test_an_ordinary_failure_is_not_a_throttle(msg):
    assert KiteDataSource._is_throttle(_Boom(msg)) is False


# ── the backoff ──────────────────────────────────────────────────────────────

def test_a_throttled_endpoint_is_not_called_again_immediately():
    s = _src()
    calls = []

    def fn(api):
        calls.append(1)
        raise _throttle()

    assert s._call("historical", fn, default="D") == "D"
    assert len(calls) == 1
    # next tick: must not reach the broker at all
    assert s._call("historical", fn, default="D") == "D"
    assert len(calls) == 1, "the throttled endpoint was re-hit on the next tick"


def test_an_ordinary_failure_does_not_start_a_cooldown():
    s = _src()
    calls = []

    def fn(api):
        calls.append(1)
        raise _Boom("no data")

    s._call("quote", fn, default=None)
    s._call("quote", fn, default=None)
    assert len(calls) == 2, "a non-throttle failure wrongly suppressed the next call"


def test_the_cooldown_is_per_endpoint():
    """A throttled history feed must not silence quotes."""
    s = _src()
    s._call("historical", lambda api: (_ for _ in ()).throw(_throttle()), default=None)

    reached = []
    assert s._call("quote", lambda api: reached.append(1) or "ok", default=None) == "ok"
    assert reached == [1]


def test_the_backoff_doubles_and_is_capped():
    s = _src()
    seen = []
    for _ in range(6):
        s._throttle_until.clear()          # allow the call through each round
        before = time.time()
        s._call("historical", lambda api: (_ for _ in ()).throw(_throttle()), default=None)
        seen.append(s._throttle_until["historical"] - before)

    assert seen[0] == pytest.approx(1.0, abs=0.3)
    assert seen[1] == pytest.approx(2.0, abs=0.3)
    assert seen[2] == pytest.approx(4.0, abs=0.3)
    assert all(v <= 8.5 for v in seen), f"backoff exceeded the ceiling: {seen}"


def test_a_success_clears_the_ladder():
    s = _src()
    s._call("historical", lambda api: (_ for _ in ()).throw(_throttle()), default=None)
    s._throttle_until.clear()
    assert s._call("historical", lambda api: "ok", default=None) == "ok"
    assert s._throttle_failures.get("historical") is None
    assert s._throttle_until.get("historical") is None


def test_the_cooldown_expires():
    s = _src()
    s._call("historical", lambda api: (_ for _ in ()).throw(_throttle()), default=None)
    s._throttle_until["historical"] = time.time() - 0.01
    assert s._call("historical", lambda api: "ok", default=None) == "ok"


# ── behaviour that must not change ───────────────────────────────────────────

def test_a_healthy_call_is_untouched():
    s = _src()
    assert s._call("quote", lambda api: {"x": 1}, default=None) == {"x": 1}


def test_no_session_still_short_circuits():
    s = KiteDataSource(auth=None, kite=None)
    assert s._call("quote", lambda api: "never", default="D") == "D"


def test_a_token_exception_still_invalidates_the_session():
    """The auth path must survive the rewrite."""
    invalidated = []

    class TokenException(Exception):
        pass

    auth = SimpleNamespace(
        invalidate=lambda reason: invalidated.append(reason),
        kite=SimpleNamespace(name="stub"),
    )
    s = KiteDataSource(auth=auth)
    s._call("quote", lambda api: (_ for _ in ()).throw(TokenException("bad token")), default=None)
    assert invalidated, "TokenException no longer invalidates the session"


# ── it is visible ────────────────────────────────────────────────────────────

def test_the_throttle_is_reported():
    """The health payload carried Angel throttle fields frozen at False behind a
    permanently-None smart_api; a throttled Kite feed showed a green pacing light."""
    s = _src()
    assert s.throttle_status()["kite_rest_backoff_active"] is False

    s._call("historical", lambda api: (_ for _ in ()).throw(_throttle()), default=None)
    st = s.throttle_status()
    assert st["kite_rest_backoff_active"] is True
    assert "historical" in st["kite_rest_backoff_contexts"]
    assert st["kite_rest_backoff_failures"] >= 1


def test_the_settings_resolve():
    from config.settings import get_settings

    s = get_settings()
    assert float(getattr(s, "kite_rest_backoff_seconds")) > 0
    assert float(getattr(s, "kite_rest_backoff_max_seconds")) >= float(
        getattr(s, "kite_rest_backoff_seconds")
    )
