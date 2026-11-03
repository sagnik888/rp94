"""Live warmup must respect an index's existing tracked native analysis."""
import ast
import asyncio
import inspect
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from scanner import Scanner


def _host():
    host = Scanner.__new__(Scanner)
    host.mode, host.simulation_id = "REAL", 1
    host._live_processing_futures, host._live_processing_started = {}, {}
    host._diagnostics = {}
    host.candles = SimpleNamespace(get_latest_price=lambda _: 100.0)
    host._diag_inc = host._record_live_processing_timeout = lambda *a, **kw: None
    host._record_live_inflight_skip = host._clear_live_inflight_skip = lambda *a: None
    host._set_live_entry_data_block = host._clear_live_entry_data_block = lambda *a: None
    host._build_live_fallback_result = lambda *a: ({"fallback": True}, [])
    return host


def _coordinator(host, warmup, deadlines=None):
    tree = ast.parse(textwrap.dedent(inspect.getsource(Scanner._scan_cycle)))
    coordinator = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "process_task")
    async_tools = asyncio
    if deadlines is not None:
        async def short_wait(awaitable, timeout):
            deadlines.append(timeout)
            return await asyncio.wait_for(awaitable, timeout=0.005)
        async_tools = SimpleNamespace(**{name: getattr(asyncio, name) for name in ("get_event_loop", "shield", "TimeoutError")}, wait_for=short_wait)
    namespace = dict(vars(scanner_module), self=host, cycle_is_warmup=warmup, asyncio=async_tools,
        ScannerRuntimeSettings=SimpleNamespace(from_flat=lambda _: SimpleNamespace(
            live_scan=SimpleNamespace(index_deadline_seconds=0.1))))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[coordinator], type_ignores=[])),
                 "<production process_task>", "exec"), namespace)
    return namespace["process_task"]


@pytest.mark.asyncio
async def test_warmup_skips_an_existing_pending_live_worker():
    host = _host()
    pending = asyncio.get_running_loop().create_future()
    host._live_processing_futures["NIFTY"] = pending
    calls = []
    host._process_instrument = lambda *a: (calls.append(a) or {}, [])
    with ThreadPoolExecutor(max_workers=1) as executor:
        host._scanner_live_executor = executor
        _, result = await _coordinator(host, True)("NIFTY", {})
    assert result == ({"fallback": True}, [])
    assert not calls
    assert host._live_processing_futures["NIFTY"] is pending


@pytest.mark.asyncio
@pytest.mark.parametrize("second_warmup", [False, True])
async def test_warmup_worker_is_tracked_and_never_overlaps_next_live_pass(second_warmup):
    host = _host()
    started, release = threading.Event(), threading.Event()
    calls = []
    def analyze(*args):
        calls.append(args)
        started.set()
        assert release.wait(timeout=2.0)
        return {}, []
    host._process_instrument = analyze
    with ThreadPoolExecutor(max_workers=2) as executor:
        host._scanner_live_executor = executor
        first = asyncio.create_task(_coordinator(host, True)("NIFTY", {}))
        second = None
        try:
            assert await asyncio.to_thread(started.wait, 1.0)
            second = asyncio.create_task(_coordinator(host, second_warmup)("NIFTY", {}))
            await asyncio.sleep(0.03)
            assert len(calls) == 1
            assert "NIFTY" in host._live_processing_futures
            assert second.done()
            assert second.result()[1] == ({"fallback": True}, [])
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second is not None else []))
    assert not host._live_processing_futures
    assert not host._live_processing_started


@pytest.mark.asyncio
async def test_warmup_preserves_sixty_second_budget_and_late_completion_cleanup():
    host = _host()
    release = threading.Event()
    deadlines, recovered = [], []
    host._release_undelivered_live_analysis = lambda future, simulation: recovered.append((future, simulation))
    def analyze(*args):
        assert release.wait(timeout=2.0)
        return {}, []
    host._process_instrument = analyze
    with ThreadPoolExecutor(max_workers=1) as executor:
        host._scanner_live_executor = executor
        try:
            _, result = await _coordinator(host, True, deadlines)("NIFTY", {})
            assert result == ({"fallback": True}, [])
            assert deadlines == [60.0]
            assert "NIFTY" in host._live_processing_futures
        finally:
            release.set()
            await asyncio.sleep(0.03)
    assert len(recovered) == 1
    assert recovered[0][1] == 1
    assert not host._live_processing_futures


@pytest.mark.asyncio
async def test_historical_replay_still_awaits_complete_results_without_live_tracking():
    host = _host()
    host.mode = "HISTORICAL"
    host._process_instrument = lambda *a: ({"complete": True}, [])
    with ThreadPoolExecutor(max_workers=1) as executor:
        host._scanner_live_executor = executor
        _, result = await _coordinator(host, True)("NIFTY", {})
    assert result == ({"complete": True}, [])
    assert not host._live_processing_futures
