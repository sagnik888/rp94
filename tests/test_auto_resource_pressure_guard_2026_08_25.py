"""Critical RAM/commit pressure blocks new Auto exposure, not exit management."""

import time
from types import SimpleNamespace

from engine.scanner_telemetry import ScannerTelemetryMixin


class _Telemetry(ScannerTelemetryMixin):
    pass


def _settings(**overrides):
    values = {
        "auto_resource_guard_enabled": True,
        "auto_resource_ram_block_pct": 94.0,
        "auto_resource_commit_block_pct": 92.0,
        "auto_resource_commit_min_available_mb": 1024.0,
        "auto_resource_clear_hysteresis_pct": 4.0,
        "auto_resource_clear_consecutive_samples": 2,
        "auto_resource_sample_max_age_seconds": 5.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _metrics(ram=50.0, commit=50.0, available=8192.0, **meta):
    return {
        "ram": {"total_mb": 16384.0, "used_pct": ram},
        "commit": {
            "total_mb": 32768.0,
            "used_pct": commit,
            "available_mb": available,
        },
        "_valid": meta.get("valid", True),
        "_sampled_at": meta.get("sampled_at", time.time()),
    }


def test_commit_pressure_latches_until_it_clears_with_hysteresis(monkeypatch):
    import engine.scanner_telemetry as module

    monkeypatch.setattr(module, "get_settings", lambda: _settings())
    telemetry = _Telemetry()
    samples = iter(
        [
            _metrics(commit=93.0, available=900.0),
            _metrics(commit=90.0, available=1400.0),
            _metrics(commit=80.0, available=3000.0),
            _metrics(commit=80.0, available=3000.0),
        ]
    )
    telemetry._fresh_resource_memory_payload = lambda: next(samples)

    assert "Windows commit" in telemetry._auto_execution_resource_blockers()[0]
    assert telemetry._auto_execution_resource_blockers(), "pressure must not flap clear"
    assert telemetry._auto_execution_resource_blockers(), "one healthy sample is insufficient"
    assert telemetry._auto_execution_resource_blockers() == []


def test_unknown_metrics_fail_closed_for_new_exposure(monkeypatch):
    import engine.scanner_telemetry as module

    monkeypatch.setattr(module, "get_settings", lambda: _settings())
    telemetry = _Telemetry()
    telemetry._fresh_resource_memory_payload = lambda: {}

    assert "unavailable or stale" in telemetry._auto_execution_resource_blockers()[0]


def test_runtime_guard_inhibits_entries_without_changing_auto_exit_semantics():
    import main

    scanner = SimpleNamespace(
        mode="REAL",
        auto_mode=True,
        is_running=True,
        _auto_execution_resource_blockers=lambda: ["critical commit pressure"],
    )

    pressured, message = main._apply_runtime_resource_guard(scanner)

    assert pressured is True
    assert scanner.auto_mode is True
    assert scanner.is_running is True, "exit/reconciliation tasks must remain alive"
    assert scanner._auto_resource_entry_inhibited is True
    assert "entries inhibited" in message


def test_resource_guard_is_inactive_when_auto_is_already_off():
    import main

    scanner = SimpleNamespace(
        mode="REAL",
        auto_mode=False,
        is_running=True,
        _auto_execution_resource_blockers=lambda: ["critical RAM pressure"],
    )

    pressured, message = main._apply_runtime_resource_guard(scanner)

    assert pressured is False
    assert scanner.auto_mode is False
    assert scanner._auto_resource_entry_inhibited is False
    assert message == ""


def test_live_auto_entry_gate_blocks_pressure_between_watchdog_polls():
    from scanner import Scanner

    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = True
    scanner._auto_execution_resource_blockers = lambda: ["critical commit pressure"]
    scanner.log_signal_decision_once = lambda *args, **kwargs: None
    candidate = SimpleNamespace(
        action="ENTRY", instrument="NIFTY", timeframe="5min", direction="LONG"
    )

    assert scanner._passes_live_trade_ready_gate(candidate) is False


def test_failed_sample_never_clears_an_existing_pressure_latch(monkeypatch):
    import engine.scanner_telemetry as module

    monkeypatch.setattr(module, "get_settings", lambda: _settings())
    telemetry = _Telemetry()
    samples = iter([_metrics(commit=95.0), {}, _metrics(commit=50.0), _metrics(commit=50.0)])
    telemetry._fresh_resource_memory_payload = lambda: next(samples)

    assert telemetry._auto_execution_resource_blockers()
    assert "unavailable or stale" in telemetry._auto_execution_resource_blockers()[0]
    assert telemetry._auto_execution_resource_blockers()
    assert telemetry._auto_execution_resource_blockers() == []


def test_stale_sample_is_not_accepted_as_healthy(monkeypatch):
    import engine.scanner_telemetry as module

    monkeypatch.setattr(module, "get_settings", lambda: _settings())
    telemetry = _Telemetry()
    telemetry._fresh_resource_memory_payload = lambda: _metrics(
        sampled_at=time.time() - 30.0
    )

    assert "unavailable or stale" in telemetry._auto_execution_resource_blockers()[0]


def test_commit_pressure_alert_remains_numeric_and_publishable():
    telemetry = _Telemetry()
    telemetry.mode = "REAL"
    telemetry._diagnostics = {"latency": {"last_ms": 0}}
    telemetry.latest_results = {}
    telemetry._historical_rebuild_pending = False
    telemetry._scanner_freshness_payload = lambda: {"is_stale": False}
    telemetry._recalculation_status_payload = lambda: {"status": "idle"}
    telemetry._get_system_metrics_payload = lambda: _metrics(
        commit=93.0, available=700.0
    )
    telemetry.data = SimpleNamespace(get_source_health=lambda: {})
    events = []
    telemetry.log_event = lambda message, category: events.append((message, category))

    payload = telemetry._system_performance_payload()
    issue = next(row for row in payload["issues"] if row["key"] == "commit_pressure")
    assert isinstance(issue["value"], float)
    telemetry._publish_system_performance_alerts()
    assert any("Windows commit pressure" in message for message, _ in events)


def test_resource_pressure_does_not_park_a_strategy_exit_in_manual_pending():
    import main
    from scanner import Scanner

    scanner = Scanner.__new__(Scanner)
    scanner.auto_mode = True
    scanner.mode = "REAL"
    scanner.is_running = True
    scanner._auto_execution_resource_blockers = lambda: ["critical commit pressure"]
    scanner._reset_daily_counters = lambda: None
    scanner._spread_aware_exit_decision = lambda *args: (False, "")
    trade = SimpleNamespace(
        instrument="NIFTY",
        inst_type="FUT",
        execution_status="OPEN",
        is_ghost=False,
        pending_exit=False,
    )
    scanner.trades = SimpleNamespace(open_trades={"T1": trade})
    closed = []
    scanner._close_and_record_internal = lambda *args, **kwargs: closed.append(args[0])

    assert main._apply_runtime_resource_guard(scanner)[0] is True
    scanner._close_and_record("T1", 101.0, "TARGET_HIT")

    assert scanner.auto_mode is True
    assert closed == ["T1"]
    assert trade.pending_exit is False
