"""Chart builds must not run on the scanner's signal-generation loop.

The operator described the dashboard as having "a dedicated ThreadPoolExecutor" that
"stops heavy chart data processing from blocking the FastAPI event loop". Neither half was
true of the code: a repo-wide search found NO ThreadPoolExecutor in dashboard/server.py,
and the single run_in_executor there passed None — asyncio's shared default pool — for the
daily signal report, not charts.

Both chart entry points (`subscribe_chart` over the websocket, and GET /api/chart) went
through _call_on_scanner_loop, which marshals the callable onto scanner._event_loop — the
loop whose `while self.is_running:` body awaits _scan_cycle(). So a chart rebuild ran
inline on the trading loop, delaying signal generation, which is the opposite of the
stated intent.

Selection stays on the scanner loop on purpose: the scan cycle reads active_chart_instrument
and active_chart_tf to decide which chart to keep warm, so setting those from another
thread could have it refresh the wrong one. That is two attribute writes. The build that
follows is the expensive part and now runs on a dedicated two-worker pool, made safe by
the lock inside _build_chart_from_cache.
"""
import ast
import inspect
import textwrap
import threading

import pytest


# ── the executor the operator believed existed ───────────────────────────────

def test_a_dedicated_chart_executor_exists():
    import dashboard.server as server

    pool = getattr(server, "_chart_executor", None)
    assert pool is not None, "dashboard/server.py still has no dedicated chart executor"
    assert pool._max_workers >= 1
    assert "chart" in (pool._thread_name_prefix or "").lower()


def test_it_is_not_the_shared_default_pool():
    """Sharing the default executor puts chart builds behind state saves and joins."""
    import asyncio

    import dashboard.server as server

    loop = asyncio.new_event_loop()
    try:
        assert server._chart_executor is not loop._default_executor
    finally:
        loop.close()


def test_the_chart_pool_is_shut_down_on_exit():
    """Every other executor here is; one that is not would hold the process open."""
    import main

    src = inspect.getsource(main)
    assert "_chart_executor" in src, "the chart pool is never shut down"
    assert "chart_executor.shutdown" in src


# ── the dispatch actually changed ────────────────────────────────────────────

def test_neither_chart_entry_point_marshals_the_build_onto_the_scanner_loop():
    import dashboard.server as server

    src = inspect.getsource(server)
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != "_call_on_scanner_loop":
            continue
        # Does this particular marshalled call build a chart?
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Attribute)
                    and inner.attr == "get_chart_snapshot"):
                offenders.append(node.lineno)
    assert not offenders, (
        f"get_chart_snapshot is still dispatched onto the scanner event loop at "
        f"line(s) {offenders} — a rebuild there delays signal generation"
    )


def test_the_build_goes_through_the_chart_pool():
    import dashboard.server as server

    src = inspect.getsource(server._build_chart_off_loop)
    assert "run_in_executor" in src
    assert "_chart_executor" in src, "the build does not use the dedicated pool"
    assert "get_chart_snapshot" in src


def test_selection_still_happens_on_the_scanner_loop():
    """The scan cycle reads these two attributes to pick which chart to keep warm."""
    import dashboard.server as server

    src = inspect.getsource(server._build_chart_off_loop)
    assert "_call_on_scanner_loop" in src
    assert "active_chart_instrument" in src
    assert "active_chart_tf" in src


# ── the build is safe to run off the loop ────────────────────────────────────

def test_the_chart_build_serialises_itself():
    """Two threads can now reach it: the scan cycle and the chart pool."""
    from scanner import Scanner

    src = inspect.getsource(Scanner._build_chart_from_cache)
    assert "_chart_build_lock" in src, (
        "_build_chart_from_cache is reachable from two threads with no lock; interleaved "
        "builds can publish a payload whose fingerprint belongs to the other one"
    )


def test_concurrent_builds_do_not_interleave():
    """Prove the lock actually serialises, rather than merely existing."""
    from scanner import Scanner

    s = Scanner.__new__(Scanner)
    s.chart_stream_enabled = True
    inside = []
    overlap = []

    def slow(instrument, force=False):
        inside.append(1)
        if len(inside) > 1:
            overlap.append(1)
        import time as _t
        _t.sleep(0.02)
        inside.pop()
        return {}

    s._build_chart_from_cache_locked = slow
    threads = [threading.Thread(target=lambda: s._build_chart_from_cache("NIFTY")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlap == [], "two chart builds ran at the same time"


def test_a_disabled_chart_stream_short_circuits_before_the_lock():
    from scanner import Scanner

    s = Scanner.__new__(Scanner)
    s.chart_stream_enabled = False
    assert s._build_chart_from_cache("NIFTY") == {}
