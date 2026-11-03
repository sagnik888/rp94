"""Network recovery must not starve exact-timeframe intelligence publication."""
import asyncio
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

import scanner as scanner_module
from scanner import Scanner
from engine.signal_processor import SignalProcessor
from tests.test_intel_refresh_schedule_2026_09_25 import _shell


def _source():
    source = _shell()
    source.is_running = source._initial_setup_completed = True
    source.config = {"indices": {name: {} for name in (
        "NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")}}
    source.active_indices = list(source.config["indices"])
    source._get_depth_context = lambda *_: {}
    source._live_analysis_window = lambda frame, tf: frame
    source._get_live_volatility_context = lambda: {}
    source._previous_close_for = lambda *_: 22900.
    source._price_change_pct_for = lambda *a, **k: (0., 0.)
    source._days_to_expiry_for = lambda *_: 3.
    source._build_intelligence_contexts = lambda *a, **k: {}
    source._instrument_iv_observations = lambda *_: []
    source._record_instrument_iv = lambda *_: None
    source._record_bar_aligned_intelligence = lambda *_: None
    return source


def test_blocked_network_recovery_does_not_expire_any_timeframe(monkeypatch):
    async def run():
        source = _source()
        clock, fetch_calls, analyzed = [1000.], {}, []
        release, entered = threading.Event(), threading.Event()
        chain = pd.DataFrame({"strike": [23000.]})
        source._chain_cache = {name: chain for name in source.active_indices}
        def read_chain(name, cfg, *, allow_fetch=True):
            if allow_fetch:
                fetch_calls[name] = fetch_calls.get(name, 0) + 1
                if name == "NIFTY":
                    entered.set()
                    release.wait(5)
            # The network is unavailable after this clock jump. Never relabel its
            # cached observations as fresh merely because analysis can run again.
            stale = clock[0] >= 1030.
            return chain, {"stale": stale, "intelligence_usable": not stale}
        source._get_option_chain_cached = read_chain
        def analyze(**kw):
            analyzed.append((kw["instrument"], kw["timeframe"], dict(kw["chain_quality"])))
            return {"regime": {"regime": "TRENDING_UP", "candle_count": 40},
                    "aggregate": {"score": 25}, "data_quality": kw["chain_quality"]}
        source.intel = SimpleNamespace(analyze=analyze)
        settings = SimpleNamespace(intelligence_analysis_ttl_seconds=30.,
            intelligence_background_refresh_seconds=20., per_timeframe_grading_enabled=True)
        monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
        monkeypatch.setattr(scanner_module.time, "time", lambda: clock[0])
        real_sleep = asyncio.sleep
        async def poll(seconds):
            await real_sleep(.002 if seconds >= 1 else 0)
        monkeypatch.setattr(asyncio, "sleep", poll)
        worker = asyncio.create_task(source._background_intel_worker())
        async def published_at(epoch):
            async def wait():
                while not all(source._intel_cache_time.get(f"{name}_{tf}", 0) >= epoch
                    for name in source.active_indices for tf in ("1min", "5min", "15min")):
                    await real_sleep(.002)
            await asyncio.wait_for(wait(), 1.)
        try:
            await published_at(1000.)
            assert entered.is_set(), "The test must actually hold a network request"
            for epoch in (1021., 1042.):
                clock[0] = epoch
                await published_at(epoch)
                for name in source.active_indices:
                    assert set(SignalProcessor._build_timeframe_contexts(source, name, settings)) == {
                        "1min", "5min", "15min"}
            assert fetch_calls["NIFTY"] == 1, "Network recovery must remain single-flight"
            assert any(q["stale"] and not q["intelligence_usable"] for _, _, q in analyzed)
        finally:
            release.set()
            source.is_running = False
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("blocked_phase", ["context", "analysis"])
def test_slow_index_is_single_flight_and_cannot_publish_after_mode_change(monkeypatch, blocked_phase):
    async def run():
        source = _source()
        clock, release, entered = [1000.], threading.Event(), threading.Event()
        chain = pd.DataFrame({"strike": [23000.]})
        source._get_option_chain_cached = lambda *a, **kw: (chain, {})
        settings = SimpleNamespace(intelligence_analysis_ttl_seconds=30.,
            intelligence_background_refresh_seconds=20., per_timeframe_grading_enabled=True)
        monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
        monkeypatch.setattr(scanner_module.time, "time", lambda: clock[0])
        real_sleep = asyncio.sleep
        async def poll(seconds):
            await real_sleep(.002 if seconds >= 1 else 0)
        monkeypatch.setattr(asyncio, "sleep", poll)
        def hold(name, phase):
            if name == "NIFTY" and phase == blocked_phase:
                entered.set()
                release.wait(5)
        def contexts(name, *a, **kw):
            hold(name, "context")
            return {}
        def analyze(**kw):
            hold(kw["instrument"], "analysis")
            return {"regime": {"regime": "TRENDING_UP", "candle_count": 40}}
        source._build_intelligence_contexts = contexts
        source.intel = SimpleNamespace(analyze=analyze)
        worker = asyncio.create_task(source._background_intel_worker())
        async def ready(epoch):
            while not (entered.is_set() and all(
                source._intel_cache_time.get(f"{name}_1min", 0) >= epoch
                for name in source.active_indices if name != "NIFTY")):
                await real_sleep(.002)
        try:
            await asyncio.wait_for(ready(1000.), 1.)
            original = source._background_intel_analysis_tasks["NIFTY"]
            clock[0] = 1042.
            await asyncio.wait_for(ready(1042.), 1.)
            assert source._background_intel_analysis_tasks["NIFTY"] is original
            source.mode = "HISTORICAL"
            source.active_indices = []
            release.set()
            await asyncio.wait_for(asyncio.shield(original), 1.)
            assert "NIFTY_1min" not in source._intel_cache
        finally:
            release.set()
            source.is_running = False
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert not source._background_intel_analysis_tasks
        assert not source._background_intel_chain_tasks
        assert source._background_intel_inflight is False
    asyncio.run(run())


def test_recovered_chain_is_used_without_waiting_for_next_analysis_interval(monkeypatch):
    async def run():
        source = _source()
        source.active_indices = ["NIFTY"]
        source.config = {"indices": {"NIFTY": {}}}
        old = pd.DataFrame({"strike": [23000.], "version": [0]})
        new = pd.DataFrame({"strike": [23000.], "version": [1]})
        source._chain_cache, source._last_chain_fetch = {"NIFTY": old}, {"NIFTY": 999.}
        release, old_seen, new_seen = threading.Event(), threading.Event(), threading.Event()
        def read_chain(name, cfg, *, allow_fetch=True):
            if allow_fetch and source._chain_cache[name] is old:
                release.wait(5)
                source._chain_cache[name], source._last_chain_fetch[name] = new, 1000.
            return source._chain_cache[name], {}
        def analyze(**kw):
            (new_seen if kw["options_chain"] is new else old_seen).set()
            return {"regime": {"regime": "TRENDING_UP", "candle_count": 40}}
        source._get_option_chain_cached = read_chain
        source.intel = SimpleNamespace(analyze=analyze)
        settings = SimpleNamespace(intelligence_analysis_ttl_seconds=30.,
            intelligence_background_refresh_seconds=20.)
        monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
        monkeypatch.setattr(scanner_module.time, "time", lambda: 1000.)
        real_sleep = asyncio.sleep
        async def poll(seconds):
            await real_sleep(.002 if seconds >= 1 else 0)
        monkeypatch.setattr(asyncio, "sleep", poll)
        worker = asyncio.create_task(source._background_intel_worker())
        async def wait(event):
            while not event.is_set():
                await real_sleep(.002)
        try:
            await asyncio.wait_for(wait(old_seen), 1.)
            release.set()
            await asyncio.wait_for(wait(new_seen), 1.)
        finally:
            release.set()
            source.is_running = False
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())


def test_failed_index_task_is_retired_and_can_retry(monkeypatch):
    async def run():
        source = _source()
        first = [True]
        original_price = source.candles.get_latest_price
        def price(name):
            if name == "NIFTY" and first[0]:
                first[0] = False
                raise RuntimeError("Transient quote snapshot failure")
            return original_price(name)
        source.candles.get_latest_price = price
        chain = pd.DataFrame({"strike": [23000.]})
        source._get_option_chain_cached = lambda *a, **kw: (chain, {})
        source.intel = SimpleNamespace(analyze=lambda **kw: {
            "regime": {"regime": "TRENDING_UP", "candle_count": 40}})
        real_sleep = asyncio.sleep
        async def poll(seconds):
            await real_sleep(.002 if seconds >= 1 else 0)
        monkeypatch.setattr(asyncio, "sleep", poll)
        worker = asyncio.create_task(source._background_intel_worker())
        async def ready():
            while not all(f"{name}_1min" in source._intel_cache for name in source.active_indices):
                await real_sleep(.002)
        try:
            await asyncio.wait_for(ready(), 1.)
            assert first[0] is False
        finally:
            source.is_running = False
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())
