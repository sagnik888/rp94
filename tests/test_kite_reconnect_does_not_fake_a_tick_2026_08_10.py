"""A reconnecting Kite socket stamped a tick it never received.

`on_connect` did `self._last_kite_ws_tick = time.time()` before any tick arrived. That
field is the ONLY input to the stream-staleness judgement in build_source_health, so a
socket that reconnected faster than the 45s freshness bound while delivering nothing read
as a perfectly healthy feed:

    11:00:00  socket drops
    11:00:25  reconnects, delivers nothing  -> on_connect stamps "fresh"
    11:00:50  reconnects, delivers nothing  -> stamps "fresh" again
    ...        kite_age never exceeds 45s, kite_stream_stalled stays False

so `broker_degraded`, `all_brokers_unavailable`, the "Kite feed silent" critical and the
Yahoo emergency arm all stay quiet on a feed delivering nothing.

WHY NOT "MIRROR FYERS AND SET 0.0" — that was tried and is WRONG here. Fyers pairs its
0.0 with `_fyers_ws_connected_since` and a stall predicate that measures the no-tick case
from the connect time. build_source_health has no such pairing: when kite_age is None it
falls back to provider UPTIME, which mid-session is hours. Zeroing the field on a routine
reconnect therefore forces the uptime branch and instantly reports a stalled feed — a
false critical on every healthy reconnect.

The defect is only the STAMP. Leaving the last real tick alone makes every case correct:

    cold start, never ticked      kite_tick 0        -> uptime branch, grace   (unchanged)
    reconnect, feed healthy       tick seconds ago   -> fresh
    reconnect, delivers nothing   tick ages          -> STALLED after 45s
    flapping from cold            kite_tick 0        -> uptime grows -> STALLED

`kite_data_fresh` still forgives a stalled socket while REST is serving, so this does not
manufacture an outage out of a quiet websocket.
"""
import time
from types import SimpleNamespace

import pytest

from data.market_source_health import build_source_health


# ── the health layer, given an un-stamped tick ───────────────────────────────

def _provider(*, kite_tick, uptime=7200.0, now, rest_age=None, market_open=True):
    ltp_cache = {}
    if rest_age is not None:
        ltp_cache = {"X": {"source": "kite", "time": now - rest_age}}
    return SimpleNamespace(
        is_market_open=lambda: market_open,
        is_connected=True,
        kite=SimpleNamespace(is_connected=True),
        fyers=None,
        _fyers_auth_required=True,
        _fyers_auth_reason="",
        _fyers_refresh_disabled=False,
        _last_fyers_ws_tick=0.0,
        _last_kite_ws_tick=kite_tick,
        _provider_started_at=now - uptime,
        _ltp_cache=ltp_cache,
        _yahoo_request_count=0,
        _yahoo_last_used_at=0.0,
    )


def test_a_socket_that_stopped_ticking_reads_as_stalled():
    now = time.time()
    h = build_source_health(_provider(kite_tick=now - 120.0, now=now), now_ts=now)
    assert h["kite_stream_stalled"] is True
    assert h["kite_tick_age_seconds"] == pytest.approx(120.0, abs=1.0)


def test_a_recent_tick_is_not_stalled():
    now = time.time()
    h = build_source_health(_provider(kite_tick=now - 5.0, now=now), now_ts=now)
    assert h["kite_stream_stalled"] is False


def test_a_cold_start_that_has_never_ticked_is_not_yet_an_accusation():
    """The documented invariant: 'no tick has EVER arrived' is not 'ticks stopped'."""
    now = time.time()
    h = build_source_health(_provider(kite_tick=0.0, uptime=3.0, now=now), now_ts=now)
    assert h["kite_stream_stalled"] is False


def test_a_long_uptime_with_no_tick_ever_is_stalled():
    now = time.time()
    h = build_source_health(_provider(kite_tick=0.0, uptime=600.0, now=now), now_ts=now)
    assert h["kite_stream_stalled"] is True


def test_a_stalled_socket_is_still_fresh_while_rest_is_serving():
    """A quiet websocket is not a dead broker; this must not manufacture an outage."""
    now = time.time()
    h = build_source_health(
        _provider(kite_tick=now - 120.0, now=now, rest_age=2.0), now_ts=now
    )
    assert h["kite_stream_stalled"] is True
    assert h["kite_data_fresh"] is True
    assert h["all_brokers_unavailable"] is False


def test_a_silent_socket_with_no_rest_and_no_fyers_does_escalate():
    now = time.time()
    h = build_source_health(_provider(kite_tick=now - 120.0, now=now), now_ts=now)
    assert h["kite_data_fresh"] is False
    assert h["all_brokers_unavailable"] is True


def test_nothing_is_stalled_when_the_market_is_shut():
    now = time.time()
    h = build_source_health(
        _provider(kite_tick=now - 9999.0, now=now, market_open=False), now_ts=now
    )
    assert h["kite_stream_stalled"] is False


# ── on_connect must not stamp a tick it never received ───────────────────────

class _StubTicker:
    """Captures the callbacks start_kite_ticker attaches."""

    def __init__(self, api_key, access_token):
        self.on_ticks = self.on_connect = self.on_close = None
        self.on_error = self.on_reconnect = self.on_noreconnect = None
        self.subscribed = []

    def connect(self, threaded=True):
        return True

    def subscribe(self, tokens):
        self.subscribed.extend(tokens)

    def set_mode(self, mode, tokens):
        pass

    MODE_FULL = "full"


def _wire_ticker(monkeypatch, *, last_tick):
    """Run the real start_kite_ticker against a stub socket and hand back the provider."""
    kiteconnect = pytest.importorskip("kiteconnect")
    monkeypatch.setattr(kiteconnect, "KiteTicker", _StubTicker, raising=False)

    from data.market_data import MarketDataProvider

    p = MarketDataProvider.__new__(MarketDataProvider)
    p.start_streams = True
    p.kite = SimpleNamespace(
        _auth=SimpleNamespace(api_key="k", kite=SimpleNamespace(access_token="t"))
    )
    p._close_ws_socket = lambda *a, **k: None
    p._active_kite_tokens = {256265}
    p._kite_index_names = {"256265": "NIFTY"}
    p._last_kite_ws_tick = last_tick
    p._kite_ws_connected = False
    p._ws_cache = {}

    assert p.start_kite_ticker() is True
    assert p.kws.on_connect is not None
    return p


def test_on_connect_does_not_overwrite_the_last_real_tick(monkeypatch):
    """The whole bug. A reconnect must not claim a tick arrived."""
    old = time.time() - 120.0
    p = _wire_ticker(monkeypatch, last_tick=old)

    p.kws.on_connect(p.kws, {})

    assert p._last_kite_ws_tick == pytest.approx(old, abs=0.5), (
        "reconnect stamped a fake tick; a flapping silent socket reads as healthy"
    )


def test_on_connect_still_marks_the_socket_connected_and_subscribes(monkeypatch):
    """The rest of on_connect's job must be untouched."""
    p = _wire_ticker(monkeypatch, last_tick=0.0)
    p.kws.on_connect(p.kws, {})
    assert p._kite_ws_connected is True
    assert 256265 in p.kws.subscribed


def test_a_reconnect_that_never_ticked_leaves_the_field_at_zero(monkeypatch):
    """So the uptime branch — not a fabricated age — decides the cold case."""
    p = _wire_ticker(monkeypatch, last_tick=0.0)
    p.kws.on_connect(p.kws, {})
    assert p._last_kite_ws_tick == 0.0


def test_a_flapping_silent_socket_is_eventually_reported(monkeypatch):
    """End to end: reconnect repeatedly, deliver nothing, and the health layer must
    still notice. This is the failure the stamp concealed."""
    now = time.time()
    p = _wire_ticker(monkeypatch, last_tick=now - 200.0)
    for _ in range(8):                      # eight reconnects, zero ticks
        p.kws.on_connect(p.kws, {})

    p.is_market_open = lambda: True
    p.is_connected = True
    p.fyers = None
    p._fyers_auth_required = True
    p._fyers_auth_reason = ""
    p._fyers_refresh_disabled = False
    p._last_fyers_ws_tick = 0.0
    p._provider_started_at = now - 7200.0
    p._ltp_cache = {}
    p._yahoo_request_count = 0
    p._yahoo_last_used_at = 0.0

    h = build_source_health(p, now_ts=now)
    assert h["kite_stream_stalled"] is True, (
        "eight silent reconnects still read as a healthy feed"
    )
