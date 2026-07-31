"""The seam between grading and the decision tape (live audit, 2026-07-31).

Two commits added grading telemetry — the per-timeframe grading context and the pillar
breakdown — and both were verified at their ends: `_grade_signal` demonstrably attaches
the data to the signal, and rtracker demonstrably reads it. Neither test crossed the
SEAM, and there was no hop: TradeCandidate is constructed fresh field-by-field from the
signal, so the attributes never arrived. Live at market open every `grading` block in the
tape was `{"intel_score": 0.0, "pillars": {}, "regime": "", "timeframe": ""}`.

These tests assert the whole chain end to end, so the two ends cannot pass while the
middle is missing.
"""
from datetime import datetime

import pytest

from engine.signal_manager import SignalManager
from engine.signal_processor import (
    GRADING_TELEMETRY_ATTRS, TradeCandidate, carry_grading_telemetry,
)
from engine.ut_bot_core import UTBotSignal


def _signal(**over):
    kwargs = dict(
        timestamp=datetime(2026, 7, 31, 9, 20), signal_type="BUY", price=100.0,
        trailing_stop=96.0, stop_distance=4.0, atr_value=2.0, adx_value=22.0,
        position_state=1, bar_index=50, instrument="NIFTY", timeframe="5min",
    )
    kwargs.update(over)
    return UTBotSignal(**kwargs)


def _candidate(**over):
    kwargs = dict(
        instrument="NIFTY", direction="LONG", price=100.0, stop=96.0, target=110.0,
        lots=1, lot_size=75, grade="A", confidence=0.8, timeframe="5min",
        inst_type="OPT", option_type="CE", atm_strike=24000.0, multiplier=1.0,
        trading_symbol="NIFTY31JUL2624000CE", symbol_token="12345", rr=2.0, score=0.8,
        reasons=[], signal_timestamp=datetime(2026, 7, 31, 9, 20),
    )
    kwargs.update(over)
    return TradeCandidate(**kwargs)


def _graded_signal():
    sig = _signal()
    SignalManager._grade_signal(
        SignalManager.__new__(SignalManager), sig, 0.6, 0.5, regime="TRENDING_UP"
    )
    # what signal_processor sets alongside grading
    sig.graded_timeframe = "15min"
    sig.graded_intel_score = 0.42
    sig.graded_regime = "TRENDING_UP"
    return sig


def test_grading_attributes_survive_the_hop_to_the_candidate():
    sig, cand = _graded_signal(), _candidate()
    for name in GRADING_TELEMETRY_ATTRS:
        assert getattr(cand, name, None) is None, f"{name} should start absent"
    carry_grading_telemetry(cand, sig)
    for name in GRADING_TELEMETRY_ATTRS:
        assert getattr(cand, name, None) == getattr(sig, name), f"{name} did not survive"


def test_tape_records_a_populated_grading_block_end_to_end():
    """The regression that shipped: every field empty in the live tape."""
    import intelligence.rtracker as rt
    cand = _candidate()
    carry_grading_telemetry(cand, _graded_signal())

    from intelligence.rtracker import RegimeTracker
    tracker = RegimeTracker.__new__(RegimeTracker)
    payload = RegimeTracker._event_from_candidate(
        tracker, cand, decision="REJECTED", reason="audit seam test",
        source="session_candidate", extra=None,
    )

    grading = payload.get("grading") or {}
    assert grading.get("timeframe") == "15min"
    assert grading.get("intel_score") == pytest.approx(0.42)
    assert grading.get("regime") == "TRENDING_UP"
    pillars = grading.get("pillars") or {}
    assert pillars, "pillar breakdown is empty — the seam is broken again"
    assert {"tech", "intel", "regime", "confidence_final"} <= set(pillars)


def test_a_candidate_that_never_reached_grading_stays_empty_not_garbage():
    cand = _candidate()
    carry_grading_telemetry(cand, _signal())   # ungraded signal
    for name in GRADING_TELEMETRY_ATTRS:
        assert getattr(cand, name, None) is None


def test_both_construction_sites_carry_the_telemetry():
    """Structural: a new TradeCandidate built from a signal must not skip the hop."""
    import inspect
    from engine import signal_processor as sp
    body = "\n".join(
        l.split("#", 1)[0] for l in inspect.getsource(sp.SignalProcessor.process_best_signal).splitlines()
    )
    assert body.count("carry_grading_telemetry(") >= 2, (
        "a TradeCandidate construction site is missing the grading-telemetry hop"
    )
