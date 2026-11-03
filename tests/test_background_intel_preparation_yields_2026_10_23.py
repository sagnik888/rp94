"""An already completed scan must be deliverable while background Intel prepares."""
import asyncio
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from scanner import Scanner
from tests.test_intel_refresh_schedule_2026_09_25 import _shell


@pytest.mark.parametrize("slow_phase", ["depth", "frame", "volatility", "previous_close",
    "change", "expiry", "contexts", "iv_observations", "iv_record", "snapshot"])
def test_background_preparation_never_prevents_scan_completion(monkeypatch, slow_phase):
    async def run():
        loop, loop_thread = asyncio.get_running_loop(), threading.get_ident()
        source = _shell()
        source.is_running, source._initial_setup_completed = True, True
        source.config = {"indices": {"NIFTY": {"strike_interval": 50}}}
        source.active_indices = ["NIFTY"]
        source._background_intel_due = lambda *_: True
        calls, cache_threads, frames = [], [], []
        def phase(name, result):
            def work(*args, **kwargs):
                served = threading.Event()
                if name == slow_phase:
                    # Model a scan's completion callback queued while preparation
                    # holds its thread. It must run before preparation returns.
                    loop.call_soon_threadsafe(served.set)
                    calls.append((name, threading.get_ident(), served.wait(1)))
                return result(*args, **kwargs) if callable(result) else result
            return work
        source._get_option_chain_cached = lambda *_, **kw: (pd.DataFrame({"strike": [100]}), {})
        source._get_depth_context = phase("depth", {})
        source._live_analysis_window = phase("frame", lambda frame, tf: frame)
        source._regime_history_ready = lambda *_: True
        source._get_live_volatility_context = phase("volatility", {})
        source._previous_close_for = phase("previous_close", 100.)
        source._price_change_pct_for = phase("change", (0., 0.))
        source._days_to_expiry_for = phase("expiry", 3.)
        source._build_intelligence_contexts = phase("contexts", {})
        source._instrument_iv_observations = phase("iv_observations", [])
        source._record_instrument_iv = phase("iv_record", None)
        source._record_bar_aligned_intelligence = phase("snapshot", None)
        def analyze(**kwargs):
            frames.append(kwargs["timeframe"])
            return {"regime": {"regime": "TRENDING_UP", "candle_count": 50}}
        source.intel = SimpleNamespace(analyze=analyze)
        original_cache = source._cache_timeframe_intel
        def cache(*args, **kwargs):
            cache_threads.append(threading.get_ident())
            original_cache(*args, **kwargs)
        source._cache_timeframe_intel = cache
        original_sleep = asyncio.sleep
        async def stop_after_pass(seconds):
            if seconds >= 1:
                await asyncio.gather(*source._background_intel_analysis_tasks.values())
                source.is_running = False
            await original_sleep(0)
        monkeypatch.setattr(asyncio, "sleep", stop_after_pass)
        await Scanner._background_intel_worker(source)
        assert calls and all(thread != loop_thread and served for _, thread, served in calls)
        assert frames == ["1min", "5min", "15min"]
        assert cache_threads == [loop_thread]*3
        assert source._background_intel_inflight is False
    asyncio.run(run())
