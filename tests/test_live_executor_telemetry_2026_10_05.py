"""Live capacity and queue diagnostics must describe the dedicated analysis pool."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from engine.scanner_telemetry import ScannerTelemetryMixin


class _Telemetry(ScannerTelemetryMixin):
    active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]


def test_live_capacity_uses_the_real_analysis_pool_and_observes_its_queue(monkeypatch):
    host = _Telemetry()
    host._estimated_default_executor_max_workers = lambda: 20
    host._default_executor_max_workers = lambda: 20
    host._default_executor_queue_depth = lambda: 0
    release, both_started = threading.Event(), threading.Event()
    start_lock = threading.Lock()
    started = []
    def work():
        with start_lock:
            started.append(True)
            if len(started) == 2:
                both_started.set()
        release.wait(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        host._scanner_live_executor = pool
        futures = [pool.submit(work) for _ in range(3)]
        try:
            assert both_started.wait(1), "test workers did not start"
            payload = host._worker_resources_payload()
            assert payload["live_processing_capacity_ratio"] == .5, (
                "live capacity still describes the default pool rather than ScannerLive")
            assert payload["live_executor_max_workers"] == 2
            assert payload["live_executor_queued_tasks"] == 1
            assert payload["default_executor_max_workers"] == 20
            assert payload["default_executor_queued_tasks"] == 0
        finally:
            release.set()
            for future in futures:
                future.result(2)


def test_bare_hosts_keep_estimated_capacity_and_report_unavailable_live_pool(monkeypatch):
    host = _Telemetry()
    host._estimated_default_executor_max_workers = lambda: 12
    host._default_executor_max_workers = lambda: None
    host._default_executor_queue_depth = lambda: None
    payload = host._worker_resources_payload()
    assert payload["live_processing_capacity_ratio"] == 3.
    assert payload["live_executor_max_workers"] is None
    assert payload["live_executor_queued_tasks"] is None
    assert payload["estimated_default_executor_max_workers"] == 12
    assert payload["default_executor_max_workers"] is None


def test_unreadable_live_queue_does_not_hide_known_live_capacity(monkeypatch):
    def unavailable():
        raise RuntimeError("queue introspection unavailable")
    host = _Telemetry()
    host._scanner_live_executor = SimpleNamespace(_max_workers=8,
        _work_queue=SimpleNamespace(qsize=unavailable))
    host._estimated_default_executor_max_workers = lambda: 20
    payload = host._worker_resources_payload()
    assert payload["live_processing_capacity_ratio"] == 2.
    assert payload["live_executor_max_workers"] == 8
    assert payload["live_executor_queued_tasks"] is None
