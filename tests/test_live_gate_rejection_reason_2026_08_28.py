"""Live readiness gate keeps its detailed verdict for downstream telemetry."""

from types import SimpleNamespace

import scanner as scanner_module
from scanner import Scanner


def _candidate():
    return SimpleNamespace(
        action="ENTRY",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        signal_timestamp=None,
    )


def test_gate_block_stamps_reason_and_source_without_changing_bool_api():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "SIGNAL"
    scanner.log_event = lambda *args, **kwargs: None
    candidate = _candidate()

    assert Scanner._gate_block(
        scanner,
        candidate,
        "Live Gate: blocked NIFTY 5min LONG; detailed policy reason (reading=1).",
    ) is False
    assert candidate.live_gate_rejection_reason == "detailed policy reason (reading=1)"
    assert candidate.live_gate_rejection_source == "live_entry_gate"


def test_resource_block_stamps_exact_reason(monkeypatch):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = True
    scanner.log_signal_decision_once = lambda *args, **kwargs: None
    candidate = _candidate()
    monkeypatch.setattr(scanner_module, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(
        Scanner,
        "_auto_entry_resource_blockers",
        lambda self: ["CPU pressure", "feed lag"],
    )

    assert scanner._passes_live_trade_ready_gate(candidate) is False
    assert candidate.live_gate_rejection_reason == "CPU pressure; feed lag"
    assert candidate.live_gate_rejection_source == "resource_pressure_gate"


def test_session_standaside_stamps_exact_reason(monkeypatch):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "SIGNAL"
    scanner.log_signal_decision_once = lambda *args, **kwargs: None
    candidate = _candidate()
    monkeypatch.setattr(scanner_module, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(
        Scanner,
        "_session_bias_standaside",
        lambda self, candidate: (True, "session bias is BEARISH — standing aside"),
    )

    assert scanner._passes_live_trade_ready_gate(candidate) is False
    assert candidate.live_gate_rejection_reason == "session bias is BEARISH — standing aside"
    assert candidate.live_gate_rejection_source == "session_bias_gate"
