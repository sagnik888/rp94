"""A live token is not a live price feed, and only the token was ever checked.

build_source_health set `kite_health = bool(provider.is_connected)`, and is_connected is
assigned `auth.kite is not None` at connect time and never revoked. So for the life of the
process kite_health is a constant True:

  * broker_degraded = is_live and not (kite_health and fyers_health) could only ever fire
    on the FYERS half — a silent Kite stream read as a clean broker;
  * all_brokers_unavailable = is_live and not kite_health and not fyers_health required
    `not kite_health` and therefore could not fire AT ALL. That flag is a preflight
    BLOCKER and the trigger for the Yahoo emergency path, so both were unreachable.

kite_tick_age_seconds was computed and published for precisely this question, and then
read by nobody — the twelfth instance in this codebase of a correct primitive wired to no
consumer.

The session flag is deliberately left alone: a valid token is what determines whether
ORDERS work, and a quiet stream must not read as "cannot trade". The data question is
asked separately, and a stalled websocket is not automatically a dead feed because the
REST LTP path keeps serving.
"""
from datetime import datetime, timedelta

import pytest

from data.market_source_health import build_source_health

NOW = 1_754_400_000.0


class _Provider:
    def __init__(self, *, kite_tick_age=1.0, fyers_tick_age=1.0, live=True,
                 connected=True, fyers=True, kite_rest_age=None):
        self.is_connected = connected
        self._last_kite_ws_tick = 0.0 if kite_tick_age is None else NOW - kite_tick_age
        self._last_fyers_ws_tick = 0.0 if fyers_tick_age is None else NOW - fyers_tick_age
        self.fyers = object() if fyers else None
        self._fyers_auth_required = False
        self._fyers_auth_reason = ""
        self._fyers_refresh_disabled = False
        self._yahoo_request_count = 0
        self._yahoo_last_used_at = 0
        self._live = live
        self._ltp_cache = {}
        if kite_rest_age is not None:
            self._ltp_cache["NIFTY"] = {
                "ltp": 24500.0,
                "time": datetime.fromtimestamp(NOW) - timedelta(seconds=kite_rest_age),
                "source": "kite",
            }

    def is_market_open(self):
        return self._live


def _health(**kw):
    return build_source_health(_Provider(**kw), now_ts=NOW)


# ── the session flag keeps its original meaning ──────────────────────────────

def test_a_quiet_stream_does_not_report_the_broker_as_down():
    """Orders still work on a valid token; this must not regress."""
    assert _health(kite_tick_age=600.0)["kite"] is True


def test_a_lost_session_still_reports_the_broker_as_down():
    assert _health(connected=False)["kite"] is False


# ── the data question, which nothing used to ask ─────────────────────────────

def test_a_ticking_stream_is_not_stalled():
    h = _health(kite_tick_age=2.0)
    assert h["kite_stream_stalled"] is False
    assert h["kite_data_fresh"] is True


def test_a_silent_stream_is_reported_stalled():
    h = _health(kite_tick_age=600.0)
    assert h["kite_stream_stalled"] is True, (
        "ten minutes without a Kite tick during a live session read as perfectly healthy"
    )


def test_a_stream_that_never_ticked_is_stalled_once_we_have_waited():
    """Silence only counts as evidence after we have actually waited for it —
    see test_a_cold_start_inside_market_hours_does_not_block_real_mode below."""
    p = _Provider(kite_tick_age=None)
    p._provider_started_at = NOW - 300.0
    assert build_source_health(p, now_ts=NOW)["kite_stream_stalled"] is True


def test_nothing_is_stalled_outside_market_hours():
    """A quiet socket at 20:00 is the socket behaving correctly."""
    h = _health(kite_tick_age=None, live=False)
    assert h["kite_stream_stalled"] is False
    assert h["kite_data_fresh"] is True


def test_a_recent_rest_fill_keeps_the_data_fresh():
    """A stalled websocket is not a dead feed while REST is still serving prices."""
    h = _health(kite_tick_age=600.0, kite_rest_age=3.0)
    assert h["kite_stream_stalled"] is True
    assert h["kite_data_fresh"] is True
    assert h["kite_rest_age_seconds"] == pytest.approx(3.0, abs=1.0)


def test_a_stale_rest_fill_does_not_rescue_the_feed():
    h = _health(kite_tick_age=600.0, kite_rest_age=600.0)
    assert h["kite_data_fresh"] is False


def test_a_fyers_rest_fill_does_not_count_as_kite_data():
    p = _Provider(kite_tick_age=600.0)
    p._ltp_cache["NIFTY"] = {
        "ltp": 24500.0, "time": datetime.fromtimestamp(NOW), "source": "fyers",
    }
    h = build_source_health(p, now_ts=NOW)
    assert h["kite_rest_age_seconds"] is None
    assert h["kite_data_fresh"] is False


# ── the consequences: degraded, and the blocker that could never fire ────────

def test_a_silent_kite_feed_now_reads_as_degraded():
    h = _health(kite_tick_age=600.0, fyers_tick_age=1.0)
    assert h["broker_degraded"] is True, (
        "a dead Kite price feed reported a clean broker because the flag only ever "
        "looked at Fyers"
    )


def test_two_healthy_feeds_are_not_degraded():
    assert _health(kite_tick_age=1.0, fyers_tick_age=1.0)["broker_degraded"] is False


def test_the_all_brokers_blocker_can_finally_fire():
    """This gates the preflight blocker AND the Yahoo emergency path."""
    h = _health(kite_tick_age=600.0, fyers_tick_age=600.0)
    assert h["all_brokers_unavailable"] is True, (
        "both feeds silent and the emergency blocker still read False, because it "
        "required `not is_connected` and is_connected is never revoked"
    )


def test_one_live_feed_is_not_all_brokers_unavailable():
    assert _health(kite_tick_age=600.0, fyers_tick_age=1.0)["all_brokers_unavailable"] is False
    assert _health(kite_tick_age=1.0, fyers_tick_age=600.0)["all_brokers_unavailable"] is False


def test_rest_prices_keep_the_blocker_from_firing():
    """Degraded, yes. Unable to trade at all, no — REST is still quoting."""
    h = _health(kite_tick_age=600.0, fyers_tick_age=600.0, kite_rest_age=2.0)
    assert h["broker_degraded"] is True
    assert h["all_brokers_unavailable"] is False


def test_a_closed_market_never_blocks():
    h = _health(kite_tick_age=None, fyers_tick_age=None, live=False)
    assert h["all_brokers_unavailable"] is False
    assert h["broker_degraded"] is False


# ── the operator-facing surfaces ─────────────────────────────────────────────

def test_the_preflight_names_the_stall_separately():
    """"Degraded" reads as a Fyers problem to anyone who has seen it before."""
    from config.settings import get_settings
    from engine.safe_mode import validate_real_mode_readiness

    provider = _Provider(kite_tick_age=600.0)
    provider.get_source_health = lambda: build_source_health(provider, now_ts=NOW)
    provider.is_market_holiday = lambda now: False

    result = validate_real_mode_readiness(get_settings(), data_provider=provider)
    text = " | ".join(result.get("warnings") or [])
    assert "Kite tick stream stalled" in text
    assert "REST prices stale too" in text


def test_the_preflight_no_longer_blames_angelone():
    """AngelOne is retired; is_connected is the Kite session now."""
    from config.settings import get_settings
    from engine.safe_mode import validate_real_mode_readiness

    provider = _Provider(connected=False)
    provider.get_source_health = lambda: build_source_health(provider, now_ts=NOW)
    provider.is_market_holiday = lambda now: False

    result = validate_real_mode_readiness(get_settings(), data_provider=provider)
    text = " | ".join(result.get("blockers") or [])
    assert "AngelOne" not in text
    assert any(x in text for x in ("Upstox market data provider not connected", "Kite broker not connected"))


# ── a cold start is not a dead feed ──────────────────────────────────────────

def test_a_cold_start_inside_market_hours_does_not_block_real_mode():
    """The regression this file's own fix introduced, caught before it shipped.

    At 09:15 neither socket has ticked yet. Reading that as a stall made
    all_brokers_unavailable True — a REAL-mode preflight BLOCKER — so the system
    would refuse to start until a tick happened to land first. "No tick has EVER
    arrived" is not the claim "ticks stopped arriving".
    """
    p = _Provider(kite_tick_age=None, fyers_tick_age=None)
    p._provider_started_at = NOW - 2.0          # up for two seconds
    h = build_source_health(p, now_ts=NOW)
    assert h["kite_stream_stalled"] is False
    assert h["all_brokers_unavailable"] is False, (
        "a two-second-old process was told every broker was unavailable"
    )


def test_waiting_past_the_threshold_with_no_tick_is_a_stall():
    """Once we HAVE waited, silence is evidence."""
    p = _Provider(kite_tick_age=None, fyers_tick_age=None)
    p._provider_started_at = NOW - 300.0
    h = build_source_health(p, now_ts=NOW)
    assert h["kite_stream_stalled"] is True
    assert h["all_brokers_unavailable"] is True


def test_a_provider_that_cannot_say_when_it_started_is_never_accused():
    """No start time means no way to prove we waited; unknown, not stalled."""
    p = _Provider(kite_tick_age=None, fyers_tick_age=None)
    assert build_source_health(p, now_ts=NOW)["kite_stream_stalled"] is False


def test_a_tick_that_arrived_and_went_quiet_is_still_a_stall_regardless_of_uptime():
    p = _Provider(kite_tick_age=600.0)
    p._provider_started_at = NOW - 1.0          # just restarted, but a tick DID arrive
    assert build_source_health(p, now_ts=NOW)["kite_stream_stalled"] is True
