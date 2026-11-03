"""Combined owners must retain separate refusal evidence for the same source bar."""
from datetime import datetime
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from scanner import Scanner
from tests.test_today_block_counter_2026_10_05 import _scanner


MESSAGE = "Live Gate: blocked NIFTY 5min LONG; data-quality current scan timed out [STALE_SCAN]."


def _candidate(lane, source):
    return SimpleNamespace(instrument="NIFTY", timeframe="5min", direction="LONG",
                           signal_timestamp=datetime(2026, 10, 5, 10, 0),
                           source_lane=lane, source_engine=source)


def _setup(monkeypatch, mode):
    settings = SimpleNamespace(scanner_engine=mode)
    monkeypatch.setattr("config.settings._settings", settings)
    scanner = _scanner()
    tape, saved, archived = [], [], []

    def log(message, kind):
        tape.append(message)
        scanner._persist_rejected_signal_event(message, kind)

    scanner.log_event = log
    monkeypatch.setattr(scanner_module.db, "save_signal_decision", lambda row: saved.append(row))
    scanner._rtracker_record_candidate = lambda candidate, **kw: archived.append((candidate.source_lane, kw))
    return scanner, tape, saved, archived


@pytest.mark.parametrize("mode,lane,source", [
    ("quad_prime_s1", "QP", "utbot"),
    ("ut_smart_s1", "UTS", "ut_smart"),
])
def test_same_bar_live_gate_refusal_remains_separate_for_each_combined_owner(monkeypatch, mode, lane, source):
    scanner, tape, saved, archived = _setup(monkeypatch, mode)
    candidates = [_candidate(lane, source), _candidate("S1", "s1")]
    for _ in range(3):
        for candidate in candidates:
            assert scanner._gate_block(candidate, MESSAGE) is False
            assert candidate.live_gate_rejection_reason == "data-quality current scan timed out [STALE_SCAN]"
            assert candidate.live_gate_rejection_source == "live_entry_gate"
    assert len(tape) == len(saved) == len(archived) == 2
    assert len({row["id"] for row in saved}) == 2
    assert all(row["status"] == "REJECTED" for row in saved)
    assert all((row["instrument"], row["timeframe"], row["direction"]) == ("NIFTY", "5min", "LONG")
               for row in saved)
    native_owner = "quad_prime" if lane == "QP" else "ut_smart"
    assert any(f"[{native_owner}]; " in message for message in tape)
    assert any("[s1]; " in message for message in tape)
    assert {lane for lane, _ in archived} == {lane, "S1"}
    assert sum(scanner._diagnostics["rejects"].values()) == 2
    assert len(scanner._diagnostics["rejects"]) == 2


@pytest.mark.parametrize("mode,lane,source", [
    ("quad_prime_s1", "QP", "utbot"),
    ("ut_smart_s1", "UTS", "ut_smart"),
])
def test_restored_owner_refusals_are_not_counted_again(monkeypatch, mode, lane, source):
    first, _, first_saved, _ = _setup(monkeypatch, mode)
    candidates = [_candidate(lane, source), _candidate("S1", "s1")]
    for candidate in candidates:
        assert first._gate_block(candidate, MESSAGE) is False
    assert len(first_saved) == 2

    restored, _, restored_saved, _ = _setup(monkeypatch, mode)
    monkeypatch.setattr(scanner_module.db, "load_signal_decisions", lambda _day: first_saved)
    restored._rehydrate_live_diagnostics_from_session(first_saved[0]["date"])
    assert sum(restored._diagnostics["rejects"].values()) == 2
    for candidate in candidates:
        assert restored._gate_block(candidate, MESSAGE) is False
    assert {row["id"] for row in restored_saved} == {row["id"] for row in first_saved}
    assert sum(restored._diagnostics["rejects"].values()) == 2


def test_standalone_live_gate_keeps_existing_message_and_signal_key(monkeypatch):
    scanner, tape, saved, archived = _setup(monkeypatch, "quad_prime")
    candidate = _candidate("QP", "utbot")
    for _ in range(3):
        assert scanner._gate_block(candidate, MESSAGE) is False
    assert tape == [MESSAGE.replace("Live Gate: blocked ", "Signal rejected: ")]
    assert len(saved) == len(archived) == 1
    assert list(scanner._activity_signal_decision_keys) == [
        "livegate|NIFTY|5min|LONG|2026-10-05T10:00:00"
    ]
