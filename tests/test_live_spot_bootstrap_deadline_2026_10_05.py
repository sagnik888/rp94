"""Missing index marks must share the live analysis deadline and single flight."""

import ast
import asyncio
import inspect
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from scanner import Scanner
from config.runtime_groups import ExternalCallSettings
from engine.market_data_gateway import LiveMarketDataGateway


def _coordinator(monkeypatch, host, deadline=.05):
    """Execute the production nested coordinator rather than a copied implementation."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(Scanner._scan_cycle)))
    coordinator = next(node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "process_task")
    namespace = dict(vars(scanner_module), self=host, cycle_is_warmup=False,
        ScannerRuntimeSettings=SimpleNamespace(from_flat=lambda _: SimpleNamespace(
            live_scan=SimpleNamespace(index_deadline_seconds=deadline),
            external_calls=SimpleNamespace(deadline_seconds=deadline))))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[coordinator], type_ignores=[])),
                 "<production process_task>", "exec"), namespace)
    return namespace["process_task"]


def _host(lookup, prices=None):
    host = Scanner.__new__(Scanner)
    host.mode, host.simulation_id = "REAL", 1
    host._live_processing_futures, host._live_processing_started = {}, {}
    host._diagnostics = {}
    host._live_entry_data_blocks = {}
    host._live_inflight_skip_counts, host._live_inflight_last_alert = {}, {}
    host._last_signal_identity, host._last_signal_time = {}, {}
    host._candidate_process_cache = {}
    host.calls = []
    marks = dict(prices or {"NIFTY": 0., "BANKNIFTY": 100.})
    host.candles = SimpleNamespace(get_latest_price=lambda name: marks[name],
        update_latest_price=lambda name, price: marks.__setitem__(name, price))
    host.data = SimpleNamespace(get_ltp=lookup)
    host._process_instrument = lambda name, cfg: (
        host.calls.append(name) or {"processed": name}, [])
    host._diag_inc = host._record_live_processing_timeout = lambda *a, **kw: None
    host._record_live_inflight_skip = host._clear_live_inflight_skip = lambda *a, **kw: None
    host._build_live_fallback_result = lambda name, cfg, reason: (
        {"fallback_active": True, "fallback_reason": reason}, [])
    return host


@pytest.mark.asyncio
async def test_a_hung_missing_price_lookup_cannot_hold_the_scan_past_its_budget(monkeypatch):
    release = threading.Event()
    started = threading.Event()
    def lookup(*args):
        started.set()
        release.wait(2)
        return 101.
    host = _host(lookup)
    coordinator = _coordinator(monkeypatch, host)
    with ThreadPoolExecutor(max_workers=2) as pool:
        host._scanner_live_executor = pool
        missing = asyncio.create_task(coordinator("NIFTY", {}))
        healthy = asyncio.create_task(coordinator("BANKNIFTY", {}))
        try:
            for _ in range(25):
                await asyncio.sleep(.01)
                if missing.done():
                    break
            assert started.is_set()
            assert healthy.done(), "one missing index blocked a healthy index"
            assert missing.done(), "missing-price lookup sits outside the live index deadline"
            _, result = missing.result()
            assert result[0]["fallback_active"] and result[1] == []
            assert "NIFTY" in host._live_entry_data_blocks
            assert host.calls == ["BANKNIFTY"]
        finally:
            release.set()
            await asyncio.gather(missing, healthy)
            await asyncio.sleep(.02)


@pytest.mark.asyncio
async def test_a_timed_out_lookup_is_single_flight_and_never_starts_delayed_analysis(monkeypatch):
    release = threading.Event()
    calls = []
    def lookup(*args):
        calls.append(args)
        release.wait(2)
        return 101.
    host = _host(lookup)
    coordinator = _coordinator(monkeypatch, host, .02)
    with ThreadPoolExecutor(max_workers=2) as pool:
        host._scanner_live_executor = pool
        first = asyncio.create_task(coordinator("NIFTY", {}))
        try:
            await asyncio.sleep(.08)
            assert first.done(), "a hung lookup is not bounded"
            assert "NIFTY" in host._live_processing_futures
            _, second = await coordinator("NIFTY", {})
            assert second[0]["fallback_active"]
            assert len(calls) == 1, "retry overlaps the same outstanding lookup"
        finally:
            release.set()
            await first
            await asyncio.sleep(.03)
        assert host.calls == [], "expired lookup launched analysis after its deadline"
        assert host.candles.get_latest_price("NIFTY") == 0., "expired lookup published a late price"
        assert "NIFTY" not in host._live_processing_futures
        assert "NIFTY" in host._live_entry_data_blocks
        _, current = await coordinator("NIFTY", {})
        assert current[0]["processed"] == "NIFTY"
        assert "NIFTY" not in host._live_entry_data_blocks


@pytest.mark.asyncio
async def test_lookup_and_analysis_share_one_budget(monkeypatch):
    def lookup(*args):
        time.sleep(.04)
        return 101.
    host = _host(lookup)
    def process(name, cfg):
        host.calls.append(name)
        time.sleep(.04)
        return {"processed": name}, []
    host._process_instrument = process
    coordinator = _coordinator(monkeypatch, host, .06)
    with ThreadPoolExecutor(max_workers=1) as pool:
        host._scanner_live_executor = pool
        _, result = await coordinator("NIFTY", {})
        assert result[0].get("fallback_active"), "lookup gets a second budget before analysis"
        assert result[1] == []
        await asyncio.sleep(.04)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_healthy_price_paths_keep_normal_analysis_and_clear_the_block(monkeypatch, missing):
    calls = []
    host = _host(lambda *args: calls.append(args) or 101.,
                 {"NIFTY": 0. if missing else 100.})
    host._live_entry_data_blocks["NIFTY"] = "previous timeout"
    coordinator = _coordinator(monkeypatch, host)
    with ThreadPoolExecutor(max_workers=1) as pool:
        host._scanner_live_executor = pool
        _, result = await coordinator("NIFTY", {})
        assert result == ({"processed": "NIFTY"}, [])
        assert len(calls) == int(missing)
        assert host.calls == ["NIFTY"]
        assert "NIFTY" not in host._live_entry_data_blocks


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [0., None, float("nan")])
async def test_an_invalid_bootstrap_price_cannot_clear_stale_entry_protection(monkeypatch, price):
    host = _host(lambda *args: price)
    coordinator = _coordinator(monkeypatch, host)
    with ThreadPoolExecutor(max_workers=1) as pool:
        host._scanner_live_executor = pool
        _, result = await coordinator("NIFTY", {})
        assert result[0].get("fallback_active")
        assert result[1] == []
        assert "NIFTY" in host._live_entry_data_blocks
        assert host.calls == []


@pytest.mark.asyncio
async def test_real_gateway_keeps_the_hung_lookup_lease_until_completion(monkeypatch):
    release = threading.Event()
    lookups = []
    def lookup(*args):
        lookups.append(args)
        release.wait(2)
        return 101.
    host = _host(lookup)
    coordinator = _coordinator(monkeypatch, host, .15)
    with ThreadPoolExecutor(max_workers=2) as analysis_pool, ThreadPoolExecutor(max_workers=2) as external_pool:
        host._scanner_live_executor = analysis_pool
        host._market_data_gateway = LiveMarketDataGateway(external_pool,
            ExternalCallSettings(deadline_seconds=.15, failure_threshold=5,
                                 recovery_seconds=.1, max_inflight_per_route=1))
        try:
            _, first = await coordinator("NIFTY", {})
            assert first[0]["fallback_active"]
            route = host._market_data_gateway.snapshot()["index_ltp:NIFTY"]
            assert route["inflight"] == route["timed_out_inflight"] == 1
            _, second = await coordinator("NIFTY", {})
            assert second[0]["fallback_active"]
            assert len(lookups) == 1, "timed-out SDK work lost its external bulkhead lease"
            assert host.calls == []
            _, healthy = await coordinator("BANKNIFTY", {})
            assert healthy[0]["processed"] == "BANKNIFTY"
        finally:
            release.set()
            for _ in range(30):
                if not host._market_data_gateway.snapshot()["index_ltp:NIFTY"]["inflight"]:
                    break
                await asyncio.sleep(.01)
        _, recovered = await coordinator("NIFTY", {})
        assert recovered[0]["processed"] == "NIFTY"
        assert len(lookups) == 2
        assert "NIFTY" not in host._live_entry_data_blocks


@pytest.mark.asyncio
async def test_a_lookup_exception_returns_fallback_and_keeps_entries_blocked(monkeypatch):
    def lookup(*args):
        raise RuntimeError("quote transport unavailable")
    host = _host(lookup)
    coordinator = _coordinator(monkeypatch, host)
    with ThreadPoolExecutor(max_workers=1) as pool:
        host._scanner_live_executor = pool
        _, result = await coordinator("NIFTY", {})
        assert result[0]["fallback_active"]
        assert "quote transport unavailable" in host._live_entry_data_blocks["NIFTY"]
        assert host.calls == []


@pytest.mark.asyncio
async def test_historical_missing_price_remains_deterministic_and_untracked(monkeypatch):
    def lookup(*args):
        time.sleep(.08)
        return 101.
    host = _host(lookup)
    host.mode = "HISTORICAL"
    host._market_data_gateway = SimpleNamespace(call=lambda *a, **kw: pytest.fail(
        "historical lookup unexpectedly used a live external circuit"))
    coordinator = _coordinator(monkeypatch, host, .02)
    with ThreadPoolExecutor(max_workers=1) as pool:
        host._scanner_live_executor = pool
        _, result = await coordinator("NIFTY", {})
        assert result == ({"processed": "NIFTY"}, [])
        assert host._live_processing_futures == {}
        assert host._live_entry_data_blocks == {}
