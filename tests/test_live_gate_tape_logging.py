import collections
from datetime import datetime
from types import SimpleNamespace

from scanner import Scanner


def _candidate():
    return SimpleNamespace(
        instrument="MIDCPNIFTY",
        timeframe="15min",
        direction="SHORT",
        grade="A",
        confidence=0.77,
        signal_timestamp=datetime(2026, 6, 22, 10, 30, 0),
    )


def _scanner():
    s = Scanner.__new__(Scanner)
    s.activity_log = collections.deque(maxlen=50)
    s.is_warmup = False
    s._persist_rejected_signal_event = lambda *a, **k: None
    s._activity_signal_decision_keys = set()
    s._activity_recent_events = {}
    return s


def test_gate_block_writes_reason_to_the_live_tape_and_returns_false():
    s = _scanner()
    result = s._gate_block(_candidate(), "Live Gate: blocked MIDCPNIFTY 15min SHORT; grade A is too weak for RANGING.")

    assert result is False
    assert len(s.activity_log) == 1
    entry = s.activity_log[0]
    assert entry["type"] == "trade"
    # The terse "blocked" prose is normalized to a clear "Signal rejected" line.
    assert "Signal rejected: MIDCPNIFTY 15min SHORT" in entry["msg"]
    assert "too weak for RANGING" in entry["msg"]


def test_gate_block_dedupes_per_signal_not_per_scan():
    s = _scanner()
    c = _candidate()
    # Same signal re-evaluated on multiple scans must only post once to the tape.
    s._gate_block(c, "Live Gate: blocked MIDCPNIFTY 15min SHORT; reason A.")
    s._gate_block(c, "Live Gate: blocked MIDCPNIFTY 15min SHORT; reason A.")
    assert len(s.activity_log) == 1


def test_log_event_dedupes_identical_overlapping_callers(monkeypatch):
    s = _scanner()
    clock = iter([100.0, 100.1, 103.0])
    monkeypatch.setattr("scanner.time.monotonic", lambda: next(clock))

    assert s.log_event("Morning refresh started", "data") is True
    assert s.log_event("Morning refresh started", "data") is False
    assert s.log_event("Morning refresh started", "data") is True
    assert len(s.activity_log) == 2
    assert all(row.get("id") for row in s.activity_log)
