"""The background intel worker must not run analyze() on the scanner's event loop.

py-spy on the live process, 2026-09-23: the scanner loop thread was busy ~17s of every 30s,
and ~72% of that was `_background_intel_worker` calling `self.intel.analyze(...)` inline,
three timeframes per index. Everything else scheduled on that loop queued behind it; the
dashboard's `periodic_trades_updater` gives the loop 5s, and logged "TimeoutError" 341 times
before a mid-session restart and 31 times in the first ten minutes after it.
"""
import asyncio
import inspect
import threading

from scanner import Scanner


def _worker_source():
    return inspect.getsource(Scanner._background_intel_worker)


def test_analyze_is_dispatched_to_the_executor():
    """Source pin: the call site sits inside run_in_executor, not on the loop."""
    src = _worker_source()
    at = src.index("self.intel.analyze(")
    window = src[at - 400:at]

    assert "run_in_executor" in window, "background analyze() is back on the event loop"
    assert "intel_tf = self.intel.analyze(" not in src


def test_executor_dispatch_really_leaves_the_loop_thread():
    """The pattern used above does run the callable on another thread."""

    async def probe():
        loop_thread = threading.get_ident()
        worker_thread = await asyncio.get_running_loop().run_in_executor(
            None, lambda kw={}: threading.get_ident()
        )
        return loop_thread, worker_thread

    loop_thread, worker_thread = asyncio.run(probe())

    assert loop_thread != worker_thread
