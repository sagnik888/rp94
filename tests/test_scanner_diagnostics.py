from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from engine.scanner_telemetry import ScannerTelemetryMixin
from trading.trade_manager import IST


class DummyTelemetry(ScannerTelemetryMixin):
    pass


def test_live_diagnostics_use_live_source_age_not_five_minute_candle_age():
    telemetry = DummyTelemetry()
    telemetry._diagnostics = {}
    telemetry.active_indices = ["NIFTY", "BANKNIFTY"]
    telemetry.mode = "REAL"
    telemetry.latest_results = {"latency": 12}
    telemetry.scan_count = 7
    telemetry.warm_memory = None

    old_candle_ts = datetime.now(IST).replace(tzinfo=None) - timedelta(minutes=4)
    candle_frame = pd.DataFrame({"close": [23950.0]}, index=[old_candle_ts])
    telemetry.candles = SimpleNamespace(get_candles=lambda _inst, _tf: candle_frame)
    telemetry.data = SimpleNamespace(
        get_source_health=lambda: {
            "angel": True,
            "angel_tick_age_seconds": 1.4,
            "fyers": True,
            "fyers_tick_age_seconds": 0.6,
        }
    )
    telemetry._scanner_freshness_payload = lambda: {"is_stale": False}
    telemetry._get_system_metrics_payload = lambda: {}
    telemetry._system_performance_payload = lambda: {}

    payload = telemetry._get_diagnostics_payload()

    assert payload["data_freshness"] == {"NIFTY": 0.6, "BANKNIFTY": 0.6}
    assert payload["candle_freshness"]["NIFTY"] >= 200


def test_live_mode_suppresses_stale_historical_recalculation_wait_state():
    telemetry = DummyTelemetry()
    telemetry.mode = "REAL"
    telemetry._recalculation_status = {
        "status": "awaiting_scan",
        "reason": "startup-scan-pending",
        "started_at": None,
        "finished_at": None,
    }
    telemetry.recalculation_queue = SimpleNamespace(
        last_status="idle",
        last_reason="",
        superseded_count=0,
    )

    payload = telemetry._recalculation_status_payload()

    assert payload["status"] == "idle"
    assert payload["reason"] == "live-mode"
    assert payload["queue_status"] == "idle"


def test_cpu_metrics_are_normalized_by_logical_cpu_count(monkeypatch):
    telemetry = DummyTelemetry()
    monkeypatch.setattr("engine.scanner_telemetry.os.cpu_count", lambda: 8)

    samples = iter([100.0, 101.0])
    monkeypatch.setattr("engine.scanner_telemetry.time.process_time", lambda: next(samples))
    monkeypatch.setattr("engine.scanner_telemetry.time.time", lambda: 1000.0)

    telemetry._get_system_metrics_payload()
    telemetry._system_metrics_cache = None
    monkeypatch.setattr("engine.scanner_telemetry.time.time", lambda: 1001.0)
    payload = telemetry._get_system_metrics_payload()

    assert payload["process_cpu_single_core_pct"] == 100.0
    assert payload["process_cpu_pct"] == 12.5
    assert payload["logical_cpu_count"] == 8.0


def test_single_busy_core_does_not_trigger_system_cpu_warning(monkeypatch):
    telemetry = DummyTelemetry()
    telemetry.latest_results = {"latency": 300}
    telemetry.scan_count = 1
    telemetry.data = SimpleNamespace(get_source_health=lambda: {})
    telemetry._scanner_freshness_payload = lambda: {"is_stale": False}
    telemetry._recalculation_status_payload = lambda: {"status": "idle"}
    telemetry._get_system_metrics_payload = lambda: {
        "process_cpu_pct": 12.5,
        "process_cpu_single_core_pct": 100.0,
        "ram": {"used_pct": 50.0},
        "disk": {"used_pct": 50.0},
    }

    payload = telemetry._system_performance_payload()

    assert payload["status"] == "OK"
    assert not any(issue["key"] == "process_cpu_pressure" for issue in payload["issues"])


def test_historical_idle_stale_scan_is_not_critical():
    telemetry = DummyTelemetry()
    telemetry.mode = "HISTORICAL"
    telemetry.latest_results = {"latency": 300}
    telemetry.scan_count = 4
    telemetry._historical_rebuild_pending = False
    telemetry.data = SimpleNamespace(get_source_health=lambda: {})
    telemetry._scanner_freshness_payload = lambda: {"is_stale": True, "age_seconds": 75.0}
    telemetry._recalculation_status_payload = lambda: {"status": "idle"}
    telemetry._get_system_metrics_payload = lambda: {
        "process_cpu_pct": 5.0,
        "process_cpu_single_core_pct": 20.0,
        "ram": {"used_pct": 50.0},
        "disk": {"used_pct": 50.0},
    }

    payload = telemetry._system_performance_payload()

    assert payload["status"] == "OK"
    assert not any(issue["key"] == "scanner_stale" for issue in payload["issues"])


def test_active_historical_scan_progress_prevents_false_stale_freshness():
    telemetry = DummyTelemetry()
    telemetry.mode = "HISTORICAL"
    telemetry.last_scan_time = datetime.now(IST) - timedelta(minutes=10)
    telemetry._active_scan_progress_at = datetime.now(IST) - timedelta(seconds=1)
    telemetry.is_calculating = True
    telemetry._calculation_lock = True
    telemetry.dashboard_cache = None

    payload = telemetry._scanner_freshness_payload()

    assert payload["age_seconds"] >= 500
    assert payload["progress_age_seconds"] < 5
    assert payload["stale_basis"] == "scan_progress"
    assert payload["is_stale"] is False


def test_option_history_quality_surfaces_fallback_rate():
    telemetry = DummyTelemetry()
    telemetry.mode = "HISTORICAL"
    telemetry._diagnostics = {
        "option_history": {
            "hits": 10,
            "archived_hits": 90,
            "misses": 5,
            "synthetic": 0,
            "fallback_to_fut": 5,
        }
    }
    telemetry.reject_counts = {}
    telemetry.active_indices = []
    telemetry.latest_results = {}
    telemetry.scan_count = 1
    telemetry.warm_memory = None
    telemetry.data = SimpleNamespace(get_source_health=lambda: {})
    telemetry.candles = SimpleNamespace(get_candles=lambda *_args: pd.DataFrame())
    telemetry._recalculation_status_payload = lambda: {"status": "idle"}
    telemetry._scanner_freshness_payload = lambda: {"is_stale": False}
    telemetry._worker_resources_payload = lambda: {}
    telemetry._get_system_metrics_payload = lambda: {}

    payload = telemetry._get_diagnostics_payload()

    assert payload["option_history_quality"]["status"] == "WATCH"
    assert payload["option_history_quality"]["real_option_hits"] == 100
    assert payload["option_history_quality"]["fallback_rate_pct"] == 4.76


def test_worker_resources_publish_capacity_and_queue_context(monkeypatch):
    telemetry = DummyTelemetry()
    telemetry.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    telemetry._live_processing_futures = {"NIFTY": object()}
    telemetry._historical_rebuild_pending = True
    telemetry.recalculation_worker = SimpleNamespace(timeout_seconds=120.0)
    telemetry.trades = SimpleNamespace(
        broker=SimpleNamespace(
            _executor=SimpleNamespace(
                _max_workers=5,
                _work_queue=SimpleNamespace(qsize=lambda: 2),
            )
        )
    )
    monkeypatch.setattr("engine.scanner_telemetry.os.cpu_count", lambda: 8)
    monkeypatch.setattr("engine.scanner_telemetry.threading.active_count", lambda: 17)

    payload = telemetry._worker_resources_payload()

    assert payload["logical_cpu_count"] == 8
    assert payload["active_threads"] == 17
    assert payload["active_indices"] == 4
    assert payload["live_inflight"] == 1
    assert payload["estimated_default_executor_max_workers"] == 12
    assert payload["live_processing_capacity_ratio"] == 3.0
    assert payload["recalculation_worker_timeout_seconds"] == 120.0
    assert payload["broker_executor"] == {
        "available": True,
        "max_workers": 5,
        "queued_tasks": 2,
    }
