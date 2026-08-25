"""A connected Kite socket that has stopped ticking must be alerted and resubscribed.

2026-08-21: the Kite websocket connected at 10:23:05 and reported no error until
15:30:19 — after the close. In between it went silent. The entry gate's own log is
the only place it surfaced, and only because a signal happened to arrive:

    14:35:33  kite_ws[context/38.03s]
    15:02:47  kite_ws[context/1672.49s]     <- 27.9 minutes

`kite_stream_stalled` (market_source_health.py:62) had been True that whole time and
is already consumed twice — engine/safe_mode.py:276 adds a readiness WARNING and
engine/scanner_telemetry.py:757 adds a dashboard issue. Neither writes a CRITICAL,
neither raises a desktop alert, and nothing attempts recovery. So 28 minutes of a
dead feed produced no line in ut1.log of its own.

Recovery is the missing half. on_close/on_error never fire on a silent-but-open
socket, so kiteconnect's own reconnect never runs and the subscription is never
replayed. Re-issuing subscribe + set_mode for the active tokens is idempotent, does
not disturb a healthy stream, and is exactly what on_connect (data/market_data.py:1675)
already does.

Deliberately NOT done here: tightening any trading gate on this. The entry gate
already refuses entries without an order-authority source, which is the correct and
sufficient safety response. This only makes the condition audible and tries to fix it.
"""
from types import SimpleNamespace

import pytest

from data.market_data import MarketDataProvider


class _Ticker:
    MODE_FULL = "full"

    def __init__(self, fail=False):
        self.fail = fail
        self.subscribed = []
        self.modes = []

    def subscribe(self, tokens):
        if self.fail:
            raise RuntimeError("socket write failed")
        self.subscribed.append(list(tokens))

    def set_mode(self, mode, tokens):
        self.modes.append((mode, list(tokens)))


def _provider(tokens=("256265", "260105"), connected=True, ticker=None):
    p = MarketDataProvider.__new__(MarketDataProvider)
    p._active_kite_tokens = set(tokens)
    p._kite_ws_connected = connected
    p.kws = _Ticker() if ticker is None else ticker
    return p


def test_a_silent_socket_is_resubscribed_to_every_active_token():
    p = _provider()

    assert p.resubscribe_kite_tokens() is True
    assert p.kws.subscribed == [[256265, 260105]]
    assert p.kws.modes == [("full", [256265, 260105])]


def test_it_replays_tokens_subscribe_tokens_would_skip():
    """subscribe_tokens() filters out anything already active, so it cannot recover."""
    p = _provider()
    p.subscribe_tokens(["256265"])          # already active -> no-op by design
    assert p.kws.subscribed == []

    p.resubscribe_kite_tokens()
    assert p.kws.subscribed == [[256265, 260105]]


def test_a_disconnected_socket_is_not_touched():
    """on_connect will replay the tokens itself; writing to a dead socket is not our job."""
    p = _provider(connected=False)

    assert p.resubscribe_kite_tokens() is False
    assert p.kws.subscribed == []


def test_no_ticker_is_handled():
    p = _provider()
    p.kws = None

    assert p.resubscribe_kite_tokens() is False


def test_no_active_tokens_is_a_no_op():
    p = _provider(tokens=())

    assert p.resubscribe_kite_tokens() is False
    assert p.kws.subscribed == []


def test_a_failing_socket_reports_false_and_does_not_raise():
    """The watchdog calls this; it must never take the watchdog thread down."""
    p = _provider(ticker=_Ticker(fail=True))

    assert p.resubscribe_kite_tokens() is False


def test_non_numeric_tokens_are_ignored():
    p = _provider(tokens=("256265", "NIFTY", ""))

    assert p.resubscribe_kite_tokens() is True
    assert p.kws.subscribed == [[256265]]


def test_the_watchdog_alerts_and_recovers_on_a_stalled_stream():
    """The helper is worthless if the watchdog never asks.

    Runtime behaviour of the recovery itself is covered above; this pins that the
    watchdog loop consults the stall flag, alerts, and calls the recovery.
    """
    import inspect

    import main

    src = inspect.getsource(main.start_runtime_watchdog)
    assert "kite_stream_stalled" in src, "watchdog does not consult the stall flag"
    assert "resubscribe_kite_tokens" in src, "watchdog does not attempt recovery"
    stall_at = src.index("kite_stream_stalled")
    assert "_alert(" in src[stall_at:], "stall is detected but never alerted"
