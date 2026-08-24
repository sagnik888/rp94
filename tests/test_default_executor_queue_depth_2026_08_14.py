"""Surface how much work is WAITING for a default-executor thread, not just how many exist.

The scan cycle submits `_process_instrument` for every index through asyncio's default
executor. `max_workers` was already reported; queue depth was not — and only the depth
distinguishes "the pool is idle and something else is slow" from "four index tasks are
queued behind a state save".

Why it matters, measured live on 2026-08-14 over ~6,870 cycles: the median cycle was
904ms and p90 5,275ms, but p99 was 13,077ms and 2.2% of cycles blew the 9s per-index
budget on all four indices simultaneously. Scoring every logged operation by its
enrichment in the 8s before a spike put every I/O path at ~1.0x (option chain 0.9x, Fyers
history 1.0x, candle save 0.9x) and only symptoms high — the dashboard trades updater at
13.9x, the budget timeouts themselves at 9.1x. That is the shape of contention, not of
slow I/O, and this host has 16 logical CPUs against a 20-worker default pool, so the
remaining suspects are this queue and the GIL. A depth of 0 measured through a spike
rules the pool out and leaves the GIL.
"""
import asyncio
from types import SimpleNamespace

import pytest

from engine.scanner_telemetry import ScannerTelemetryMixin


class _Telemetry(ScannerTelemetryMixin):
    def __init__(self):
        self.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
        self._live_processing_futures = {}
        self.trades = SimpleNamespace(broker=None)


def test_no_running_loop_reports_unknown_rather_than_zero():
    """Zero would read as 'nothing queued', which is a different claim from 'unmeasured'."""
    assert _Telemetry()._default_executor_queue_depth() is None


@pytest.mark.asyncio
async def test_an_idle_pool_reports_no_queued_work():
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor(max_workers=2)
    )
    await loop.run_in_executor(None, lambda: None)
    assert _Telemetry()._default_executor_queue_depth() == 0


@pytest.mark.asyncio
async def test_work_waiting_for_a_thread_is_counted():
    """The saturation case: more submissions than workers leaves a measurable backlog."""
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)

    release = asyncio.Event()
    blocked = asyncio.Event()

    def _occupy():
        loop.call_soon_threadsafe(blocked.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)

    busy = loop.run_in_executor(None, _occupy)
    await asyncio.wait_for(blocked.wait(), timeout=5)
    queued = [loop.run_in_executor(None, lambda: None) for _ in range(3)]

    assert _Telemetry()._default_executor_queue_depth() == 3, (
        "queued index work was invisible, which is the whole point of the field"
    )

    loop.call_soon_threadsafe(release.set)
    await busy
    await asyncio.gather(*queued)
    executor.shutdown(wait=True)


def test_a_broken_executor_degrades_to_unknown_not_a_crash():
    """This runs inside the diagnostics payload; it must never be able to break it."""
    telemetry = _Telemetry()

    class _Exploding:
        @property
        def _work_queue(self):
            raise RuntimeError("no queue here")

    loop = SimpleNamespace(_default_executor=_Exploding())
    import engine.scanner_telemetry as mod

    original = mod.asyncio.get_running_loop
    mod.asyncio.get_running_loop = lambda: loop
    try:
        assert telemetry._default_executor_queue_depth() is None
    finally:
        mod.asyncio.get_running_loop = original


def test_the_payload_carries_the_field():
    payload = _Telemetry()._worker_resources_payload()
    assert "default_executor_queued_tasks" in payload
