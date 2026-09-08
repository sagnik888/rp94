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
import asyncio
import threading
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


@pytest.mark.asyncio
async def test_an_asyncio_caller_never_waits_for_the_sdk(provider):
    """A scanner-loop call must return immediately, not after its nominal timeout."""
    release = threading.Event()
    ticks = []

    def hangs(**_kwargs):
        release.wait(2.0)
        return {"s": "ok"}

    async def heartbeat():
        await asyncio.sleep(0.02)
        ticks.append(time.perf_counter())

    beat = asyncio.create_task(heartbeat())
    started = time.perf_counter()
    ok, result = provider._call_bounded(hangs, timeout=0.5, label="async-hang")
    elapsed = time.perf_counter() - started
    await beat
    release.set()

    assert (ok, result) == (False, None)
    assert elapsed < 0.05, f"event loop waited {elapsed:.3f}s for the SDK"
    assert ticks, "the SDK call starved an unrelated scanner-loop heartbeat"


def test_repeated_timeouts_keep_one_single_flight_worker(provider):
    """A stuck SDK call owns the only slot; retries may not leak helper threads."""
    release = threading.Event()
    calls = []

    def hangs(**_kwargs):
        calls.append(time.perf_counter())
        release.wait(2.0)
        return {"s": "ok"}

    for _ in range(12):
        ok, result = provider._call_bounded(hangs, timeout=0.01, label="same-history")
        assert (ok, result) == (False, None)

    assert len(calls) == 1, "each timeout launched another concurrent Fyers SDK call"
    release.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        ok, result = provider._call_bounded(hangs, timeout=0.01, label="same-history")
        if ok:
            break
        time.sleep(0.01)
    assert (ok, result) == (True, {"s": "ok"})


def test_concurrent_first_use_cannot_install_two_single_flight_locks():
    first_lookup = threading.Barrier(2)
    release = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    class RacingProvider(MarketDataProvider):
        _first_lock_lookup = threading.local()

        def __getattribute__(self, name):
            if (
                name == "_bounded_call_lock"
                and threading.current_thread().name.startswith("first-use-caller")
                and not getattr(
                type(self)._first_lock_lookup, "seen", False
                )
            ):
                type(self)._first_lock_lookup.seen = True
                first_lookup.wait(timeout=1.0)
                raise AttributeError(name)
            return super().__getattribute__(name)

    racing = RacingProvider.__new__(RacingProvider)

    def sdk_call():
        with calls_lock:
            calls.append(threading.get_ident())
        release.wait(1.0)
        return {"s": "ok"}

    results = []

    def caller():
        results.append(
            racing._call_bounded(sdk_call, timeout=0.05, label="first-use-race")
        )

    callers = [
        threading.Thread(target=caller, name=f"first-use-caller-{index}")
        for index in range(2)
    ]
    for thread in callers:
        thread.start()
    for thread in callers:
        thread.join(timeout=1.0)
    release.set()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with racing._bounded_call_lock:
            if racing._bounded_call_active is None:
                break
        time.sleep(0.005)

    assert len(results) == 2
    assert len(calls) == 1, "concurrent lazy initialization launched two SDK workers"
    with racing._bounded_call_lock:
        assert racing._bounded_call_active is None


def test_healthy_different_synchronous_calls_queue_within_their_deadlines(provider):
    """Concurrent index workers must serialize, not report false provider outages."""
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    results = {}

    def first_call():
        first_entered.set()
        release_first.wait(1.0)
        return {"request": "first"}

    def second_call():
        second_entered.set()
        return {"request": "second"}

    first = threading.Thread(
        target=lambda: results.setdefault(
            "first",
            provider._call_bounded(first_call, timeout=0.75, label="first-chain"),
        )
    )
    second = threading.Thread(
        target=lambda: results.setdefault(
            "second",
            provider._call_bounded(second_call, timeout=0.75, label="second-chain"),
        )
    )

    first.start()
    assert first_entered.wait(0.5)
    second.start()
    assert not second_entered.wait(0.05), "the provider SDK calls overlapped"
    release_first.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert results == {
        "first": (True, {"request": "first"}),
        "second": (True, {"request": "second"}),
    }


def test_different_synchronous_call_times_out_without_starting_behind_a_hang(provider):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_calls = []

    def first_call():
        first_entered.set()
        release_first.wait(1.0)
        return {"request": "first"}

    def second_call():
        second_calls.append(True)
        return {"request": "second"}

    first = threading.Thread(
        target=lambda: provider._call_bounded(
            first_call, timeout=0.75, label="blocking-first-chain"
        )
    )
    first.start()
    assert first_entered.wait(0.5)

    started = time.perf_counter()
    result = provider._call_bounded(
        second_call, timeout=0.05, label="bounded-second-chain"
    )
    elapsed = time.perf_counter() - started
    release_first.set()
    first.join(timeout=1.0)

    assert result == (False, None)
    assert elapsed < 0.25
    assert second_calls == [], "a timed-out queued request still touched the SDK"


def test_a_synchronous_error_does_not_poison_the_next_retry(provider):
    calls = []

    def flaky():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return {"s": "ok"}

    assert provider._call_bounded(flaky, timeout=1.0, label="sync-retry") == (
        False,
        None,
    )
    assert provider._call_bounded(flaky, timeout=1.0, label="sync-retry") == (
        True,
        {"s": "ok"},
    )
    assert calls == [1, 2]


def test_completion_is_published_before_a_waiter_is_signalled(provider, monkeypatch):
    """A synchronous consumer must not race ahead of the late-result publication."""
    import data.market_data as market_data_module

    real_event_factory = threading.Event
    published_when_signalled = []

    class PublicationProbeEvent:
        def __init__(self):
            self._inner = real_event_factory()

        def set(self):
            with provider._bounded_call_lock:
                published_when_signalled.append(bool(provider._bounded_call_completed))
            self._inner.set()

        def wait(self, timeout=None):
            return self._inner.wait(timeout)

    def first_event_factory():
        # Restore the real factory immediately so threading.Thread can construct its
        # own internal events normally; only _call_bounded's completion event is probed.
        market_data_module.threading.Event = real_event_factory
        return PublicationProbeEvent()

    monkeypatch.setattr(market_data_module.threading, "Event", first_event_factory)

    ok, result = provider._call_bounded(
        lambda: {"s": "ok"}, timeout=1.0, label="publication-order"
    )

    assert (ok, result) == (True, {"s": "ok"})
    assert published_when_signalled == [True]
    with provider._bounded_call_lock:
        assert provider._bounded_call_completed == {}, (
            "the synchronously-consumed result was written back after its waiter woke"
        )


def test_late_completed_result_expires_instead_of_replaying_stale_history(provider):
    provider._bounded_call_completed_ttl_seconds = 0.02
    release_first = threading.Event()
    calls = []

    def history_call():
        call_number = len(calls) + 1
        calls.append(call_number)
        if call_number == 1:
            release_first.wait(1.0)
        return {"call": call_number}

    assert provider._call_bounded(
        history_call, timeout=0.01, label="expiring-history"
    ) == (False, None)
    release_first.set()

    deadline = time.monotonic() + 1.0
    published = False
    active = object()
    while time.monotonic() < deadline:
        with provider._bounded_call_lock:
            published = bool(provider._bounded_call_completed)
            active = provider._bounded_call_active
        if published and active is None:
            break
        time.sleep(0.005)
    assert published and active is None

    time.sleep(0.05)
    ok, result = provider._call_bounded(
        history_call, timeout=1.0, label="expiring-history"
    )

    assert (ok, result) == (True, {"call": 2})
    assert calls == [1, 2], "an expired pre-outage response was replayed"


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


def test_fyers_sdk_transport_gets_a_real_default_timeout():
    calls = []

    class Session:
        def request(self, method, url, *args, **kwargs):
            calls.append((method, url, kwargs.get("timeout")))
            return object()

    session = Session()
    client = type("Client", (), {"service": type("Service", (), {"session": session})()})()

    assert MarketDataProvider._configure_fyers_http_timeout(client, 6.5) is True
    session.request("GET", "https://example.test/history")
    session.request("GET", "https://example.test/profile", timeout=2.0)

    assert calls == [
        ("GET", "https://example.test/history", 6.5),
        ("GET", "https://example.test/profile", 2.0),
    ]


def test_every_fyers_client_creation_installs_the_transport_timeout():
    import inspect

    src = inspect.getsource(MarketDataProvider)
    assert src.count("fyersModel.FyersModel(") == 4
    assert src.count("self._configure_fyers_http_timeout(") == 4


def test_recurring_fyers_rest_calls_all_use_the_provider_single_flight():
    import inspect

    src = inspect.getsource(MarketDataProvider)
    assert "self.fyers.quotes(" not in src
    assert "self.fyers.optionchain(" not in src
    assert "def _call_fyers_bounded" in src


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
