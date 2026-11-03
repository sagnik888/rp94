"""A delayed analysis must not consume an entry it never delivered."""

import ast
import asyncio
import inspect
import textwrap
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from engine.scanner_modes import scope_suffix
from engine.signal_processor import SignalProcessor
from scanner import Scanner


OWNERS = [
    ("quad_prime_s1", "multi", "QP"),
    ("quad_prime_s1", "s1", "S1"),
    ("ut_smart_s1", "ut_smart", "UTS"),
    ("ut_smart_s1", "s1", "S1"),
    ("quad_prime", "multi", "QP"),
    ("s1", "s1", "S1"),
    ("ut_smart", "ut_smart", "UTS"),
]


def _host(monkeypatch, mode, source, lane):
    settings = SimpleNamespace(scanner_engine=mode, s1_lane_enabled=True, uts_lane_enabled=True)
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
    host = Scanner.__new__(Scanner)
    host.mode, host.simulation_id = "REAL", 1
    host._analysis_event_generation = 0
    host._last_signal_identity, host._last_signal_time = {}, {}
    host._candidate_process_cache = {"NIFTY": {"candidates": ["undelivered"]}}
    host._live_processing_futures, host._live_processing_started = {}, {}
    host._diagnostics = {}
    fresh = []
    host.mtf = SimpleNamespace(require_fresh_result=lambda *args: fresh.append(args))
    stamp = datetime(2026, 10, 5, 11, 30)
    candidate = SimpleNamespace(
        instrument="NIFTY", timeframe="5min", direction="LONG", source_engine=source,
        source_lane=lane, signal_timestamp=stamp, source_signal_type="BUY", action="ENTRY",
        accepted_by_gate=False,
    )
    key = "NIFTY_5min" + scope_suffix(candidate, settings)
    identity = f"{stamp.isoformat()}|BUY|{source}"
    opposite = f"{stamp.isoformat()}|SELL|{source}"
    foreign = f"{stamp.isoformat()}|BUY|foreign"
    for consumed in (identity, opposite, foreign):
        SignalProcessor._remember_live_signal_identity(host, key, stamp, consumed)
    return host, candidate, key, identity, opposite, foreign, fresh


def _future(candidate):
    future = Future()
    future.set_result(({}, [candidate]))
    return future


@pytest.mark.parametrize("mode,source,lane", OWNERS)
def test_timeout_releases_only_exact_native_entry(monkeypatch, mode, source, lane):
    host, candidate, key, identity, opposite, foreign, fresh = _host(monkeypatch, mode, source, lane)

    host._release_undelivered_live_analysis(_future(candidate), 1)

    remaining = SignalProcessor._live_signal_identities(host._last_signal_identity.get(key))
    assert identity not in remaining
    assert remaining == {opposite, foreign}
    assert host._last_signal_time[key] == candidate.signal_timestamp
    assert "NIFTY" not in host._candidate_process_cache
    assert fresh == [("NIFTY", "5min")]
    assert host._analysis_event_generation == 1


@pytest.mark.parametrize("protection", ["accepted", "exit", "no_entry", "session", "historical", "reset", "identity_changed", "newer_signal", "engine_changed"])
def test_timeout_cleanup_preserves_delivered_or_changed_state(monkeypatch, protection):
    host, candidate, key, identity, opposite, foreign, fresh = _host(monkeypatch, "quad_prime_s1", "s1", "S1")
    if protection == "accepted":
        candidate.accepted_by_gate = True
    elif protection == "exit":
        candidate.action = "EXIT"
    elif protection == "no_entry":
        candidate.action = "NO_ENTRY"
    elif protection == "session":
        host.simulation_id = 2
    elif protection == "historical":
        host.mode = "HISTORICAL"
    elif protection == "reset":
        host._last_signal_identity.clear()
        host._last_signal_time.clear()
    elif protection == "identity_changed":
        host._last_signal_identity[key] = {opposite, foreign}
    elif protection == "newer_signal":
        later = candidate.signal_timestamp + timedelta(minutes=5)
        SignalProcessor._remember_live_signal_identity(host, key, later, f"{later.isoformat()}|BUY|s1")
    elif protection == "engine_changed":
        scanner_module.get_settings().scanner_engine = "utbot"
    before = {k: set(v) for k, v in host._last_signal_identity.items()}
    before_times = dict(host._last_signal_time)

    host._release_undelivered_live_analysis(_future(candidate), 1)

    assert host._last_signal_identity == before
    assert host._last_signal_time == before_times
    assert host._candidate_process_cache["NIFTY"]
    assert not fresh
    assert not host._analysis_event_generation


def test_duplicate_timeout_completion_recomputes_once(monkeypatch):
    host, candidate, key, identity, _, _, fresh = _host(monkeypatch, "ut_smart_s1", "ut_smart", "UTS")
    future = _future(candidate)
    host._release_undelivered_live_analysis(future, 1)
    host._release_undelivered_live_analysis(future, 1)
    assert identity not in host._last_signal_identity[key]
    assert fresh == [("NIFTY", "5min")]
    assert host._analysis_event_generation == 1


def test_failed_timeout_completion_cannot_release_identity(monkeypatch):
    host, candidate, key, identity, _, _, fresh = _host(monkeypatch, "ut_smart_s1", "s1", "S1")
    future = Future()
    future.set_exception(RuntimeError("analysis failed"))
    host._release_undelivered_live_analysis(future, 1)
    assert identity in host._last_signal_identity[key]
    assert not fresh
    assert not host._analysis_event_generation


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,source,lane", OWNERS[:4])
async def test_real_late_worker_is_discarded_then_releases_native_identity(monkeypatch, mode, source, lane):
    host, candidate, key, identity, opposite, foreign, fresh = _host(monkeypatch, mode, source, lane)
    host._last_signal_identity[key] = {opposite, foreign}
    host.candles = SimpleNamespace(get_latest_price=lambda _: 100.0)
    host._diag_inc = host._record_live_processing_timeout = lambda *a, **kw: None
    host._set_live_entry_data_block = host._clear_live_entry_data_block = lambda *a, **kw: None
    host._clear_live_inflight_skip = lambda *a: None
    host._build_live_fallback_result = lambda *a: ({"fallback": True}, [])

    def calculate(*args):
        time.sleep(0.04)
        SignalProcessor._remember_live_signal_identity(host, key, candidate.signal_timestamp, identity)
        return {}, [candidate]

    host._process_instrument = calculate
    tree = ast.parse(textwrap.dedent(inspect.getsource(Scanner._scan_cycle)))
    coordinator = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "process_task")
    namespace = dict(vars(scanner_module), self=host, cycle_is_warmup=False,
        ScannerRuntimeSettings=SimpleNamespace(from_flat=lambda _: SimpleNamespace(
            live_scan=SimpleNamespace(index_deadline_seconds=0.005))))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[coordinator], type_ignores=[])),
                 "<production process_task>", "exec"), namespace)
    with ThreadPoolExecutor(max_workers=1) as executor:
        host._scanner_live_executor = executor
        _, result = await namespace["process_task"]("NIFTY", {})
        assert result == ({"fallback": True}, [])
        await asyncio.sleep(0.08)
    assert identity not in host._last_signal_identity[key]
    assert host._last_signal_identity[key] == {opposite, foreign}
    assert fresh == [("NIFTY", "5min")]
    assert host._analysis_event_generation == 1
