"""The index-volume fetch must not be able to hold the scanner's event loop.

2026-08-31, market open. The symptom the operator saw was a stream of

    ERROR | Error in periodic trades updater: TimeoutError:

every six seconds -- a 1s poll plus a 5s _call_on_scanner_loop budget, timing out at the
FULL budget every time. That is a symptom; the loop was the casualty, not the cause.

Measured from the session log, 09:10-09:29:

    92 dashboard timeouts in nine minutes, sustained from 09:15 (the open) onward
    scan cycle fell from ~740ms to roughly one cycle per four minutes
    the longest gaps between consecutive log lines were 45s, 25s and 11s, and the line
    immediately BEFORE them was, three times over,
        "Prioritizing Fyers history for NSE:NIFTY50-INDEX to get volume..."

`self.fyers.history(data=data)` takes no timeout argument and its transport has no
default, so a degraded upstream blocks its CALLER for as long as it likes -- and that
caller runs on the scanner's asyncio loop, the same loop that runs the scan cycle, the
100ms trade monitor and every dashboard call.

This is the same failure shape as 2026-08-05
(tests/test_hft_worker_never_blocks_the_loop_2026_08_05.py), which recorded the remedy:
"memory-only on the hot path, HTTPS dispatched off-thread, throttled per token".

The call is now bounded on a daemon helper, exactly as shutdown_streams already bounds
the SDK close calls in the same file. A miss falls through to the existing path, which
already says out loud that index volume is unavailable.
"""
import time

import pytest

from config.settings import Settings
from data.market_data import MarketDataProvider


@pytest.fixture
def provider():
    return MarketDataProvider.__new__(MarketDataProvider)


def test_a_hung_upstream_is_abandoned_at_the_bound(provider):
    """The regression: an unbounded call held the loop for 45 seconds."""
    def hangs(**_kwargs):
        time.sleep(30.0)
        return {"s": "ok"}

    started = time.perf_counter()
    ok, result = provider._call_bounded(hangs, timeout=0.5, label="hangs")
    elapsed = time.perf_counter() - started

    assert ok is False
    assert result is None
    assert elapsed < 5.0, f"the caller was held for {elapsed:.1f}s despite a 0.5s bound"


def test_a_healthy_call_passes_straight_through(provider):
    """The bound must cost a well-behaved upstream nothing."""
    payload = {"s": "ok", "candles": [[1, 2, 3, 4, 5, 6]]}

    started = time.perf_counter()
    ok, result = provider._call_bounded(lambda **k: payload, timeout=8.0, label="fast")
    elapsed = time.perf_counter() - started

    assert ok is True
    assert result is payload
    assert elapsed < 0.5


def test_a_raising_call_is_reported_as_a_miss_not_a_crash(provider):
    def boom(**_kwargs):
        raise RuntimeError("fyers said no")

    ok, result = provider._call_bounded(boom, timeout=8.0, label="boom")

    assert ok is False
    assert result is None


def test_arguments_reach_the_wrapped_call(provider):
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return {"s": "ok"}

    ok, _ = provider._call_bounded(
        capture, timeout=8.0, label="capture", data={"symbol": "NSE:NIFTY50-INDEX"}
    )

    assert ok is True
    assert seen == {"data": {"symbol": "NSE:NIFTY50-INDEX"}}


def test_the_index_history_call_is_the_bounded_one():
    """A bare self.fyers.history(...) is the whole defect; it must not come back."""
    import inspect

    src = inspect.getsource(MarketDataProvider.get_historical_candles)

    assert "self.fyers.history(data=data)" not in src, (
        "the index-history call is unbounded again"
    )
    assert "_call_bounded(" in src
    assert "fyers_history_timeout_seconds" in src


def test_the_timeout_is_generous_against_a_healthy_call():
    """The aim is to bound a pathological call, not to trim a slow one: every miss
    falls through to a source that has no index volume at all."""
    timeout = float(Settings().fyers_history_timeout_seconds)

    assert 3.0 <= timeout <= 20.0
