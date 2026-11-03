"""Refuse an entry the whole option book already agrees with.

2,348 live signals, 2026-07-20..2026-09-10, scored by the index move in the signal's
direction over the next 30 minutes. With zero of the three OI/PCR/flow pillars against
the trade (usable evidence, the same votes _intel_adverse_votes counts), 45.8% moved the
trade's way against 54.1% for the rest; executable option P&L was -Rs.301/trade (95% CI
-704..-72). Crowded agreement is late agreement. Absent evidence must never block, and
the gate is one setting away from off.
"""
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from scanner import Scanner

BULLISH_BOOK = {  # PCR >= 1.0 reads contrarian-bullish; OI bullish; flow ratio >= 1.15
    "pcr": {"primary_pcr": 1.12, "signal": "BULLISH"},
    "oi": {"signal": "BULLISH"},
    "order_flow": {"ratio": 1.3, "signal": "BULLISH"},
}
MIXED_BOOK = {
    "pcr": {"primary_pcr": 1.12, "signal": "BULLISH"},
    "oi": {"signal": "BEARISH"},
    "order_flow": {"ratio": 1.0, "signal": "NEUTRAL"},
}
NO_BOOK = {"pcr": {"primary_pcr": 0.0}, "oi": {"signal": "NEUTRAL"}, "order_flow": {}}


def _scanner(monkeypatch, intel, enabled=True):
    s = Scanner.__new__(Scanner)
    s.mode = "REAL"
    s.auto_mode = True
    s._cached_timeframe_intel = lambda instrument, timeframe, allow_fallback=True: intel
    monkeypatch.setattr(
        scanner_module, "get_settings",
        lambda: SimpleNamespace(entry_block_unanimous_book=enabled, event_blackout_enabled=False),
    )
    return s


def _candidate(direction="LONG"):
    return SimpleNamespace(
        instrument="NIFTY", timeframe="5min", direction=direction, action="ENTRY",
        confidence=0.8, grade="A",
    )


def test_a_long_the_whole_book_agrees_with_is_refused(monkeypatch):
    s = _scanner(monkeypatch, BULLISH_BOOK)
    reason = Scanner._unanimous_book_blocker(s, _candidate("LONG"))
    assert "UNANIMOUS_BOOK" in reason and "0 of 3" in reason


def test_a_short_against_the_same_book_is_not(monkeypatch):
    s = _scanner(monkeypatch, BULLISH_BOOK)
    assert Scanner._unanimous_book_blocker(s, _candidate("SHORT")) == ""


def test_one_dissenting_pillar_is_enough_to_pass(monkeypatch):
    s = _scanner(monkeypatch, MIXED_BOOK)
    assert Scanner._unanimous_book_blocker(s, _candidate("LONG")) == ""


def test_an_absent_book_never_blocks(monkeypatch):
    s = _scanner(monkeypatch, NO_BOOK)
    assert Scanner._unanimous_book_blocker(s, _candidate("LONG")) == ""


def test_the_setting_turns_it_off(monkeypatch):
    s = _scanner(monkeypatch, BULLISH_BOOK, enabled=False)
    assert Scanner._unanimous_book_blocker(s, _candidate("LONG")) == ""


def test_it_is_off_by_default():
    """Anti-trend: it would have refused the 2026-09-11 rally LONGs."""
    from config.settings import Settings

    assert Settings.model_fields["entry_block_unanimous_book"].default is False


def test_the_live_gate_refuses_and_names_the_gate(monkeypatch):
    s = _scanner(monkeypatch, BULLISH_BOOK)
    logged = []
    s.log_signal_decision_once = lambda key, message, category="trade": logged.append(message)
    monkeypatch.setattr(Scanner, "_auto_entry_resource_blockers", lambda self: [])
    monkeypatch.setattr(Scanner, "_session_bias_standaside", lambda self, c: (False, ""))
    cand = _candidate("LONG")
    assert s._passes_live_trade_ready_gate(cand) is False
    assert cand.live_gate_rejection_source == "unanimous_book_gate"
    assert logged and "Unanimous option book" in logged[0]


def test_the_breadth_context_no_longer_dies_on_a_name_error(monkeypatch):
    """scanner.py built SimpleNamespace peers without importing it: every live call
    raised NameError and breadth silently read NEUTRAL/0."""
    assert scanner_module.SimpleNamespace is SimpleNamespace
