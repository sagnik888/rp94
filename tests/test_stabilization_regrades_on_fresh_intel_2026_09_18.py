"""A buffered signal is re-scored on the evidence that exists when it matures.

Grading ran once, at the arrow's birth. The live gate then read that birth-time
confidence (`confidence=float(getattr(candidate, "confidence", 0.0))`), so the 20-60
seconds a signal spends in the stabilization buffer — the window whose entire purpose is
to collect more evidence — could not move its grade. Regime was re-read live for other
checks while the pillar built on it stayed frozen.

Maturity runs the complete grader from captured raw inputs and refreshed own-timeframe
evidence. Nonlinear calibration and caps cannot be replayed as a frozen adjustment.
"""
import inspect
import math
from datetime import datetime
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from engine.signal_manager import (
    SignalManager,
    confidence_from_total_score,
    intelligence_pillar_score,
    regime_pillar_score,
)
from scanner import Scanner
from engine.execution_service import ExecutionService


# ── the shared pillar functions ───────────────────────────────────────────────


def test_the_intelligence_pillar_is_direction_aligned():
    buy, _ = intelligence_pillar_score(0.6, "BUY")
    sell, _ = intelligence_pillar_score(0.6, "SELL")

    assert buy == pytest.approx(0.18)
    assert sell == pytest.approx(-0.18)


def test_the_intelligence_pillar_keeps_its_asymmetric_clamp():
    """A full-scale reading (legacy -100..100 units) hits +0.30 but floors at -0.20."""
    assert intelligence_pillar_score(100.0, "BUY")[0] == pytest.approx(0.30)
    assert intelligence_pillar_score(100.0, "SELL")[0] == pytest.approx(-0.20)


@pytest.mark.parametrize(
    "regime, signal_type, expected",
    [
        ("TRENDING_UP", "BUY", 0.30),
        ("TRENDING_UP", "SELL", -0.25),
        ("BREAKOUT_DOWN", "SELL", 0.30),
        ("BREAKOUT_DOWN", "BUY", -0.25),
        ("RANGING", "BUY", -0.10),
        ("VOLATILE", "SELL", -0.10),
        ("UNKNOWN", "BUY", -0.30),
    ],
)
def test_the_regime_pillar_matches_the_grading_table(regime, signal_type, expected):
    assert regime_pillar_score(regime, signal_type)[0] == pytest.approx(expected)


def test_a_confirmed_range_day_caps_the_trend_bonus():
    capped, reasons = regime_pillar_score(
        "TRENDING_UP", "BUY", day_type="RANGE_DAY", day_type_confidence=0.9
    )

    assert capped == pytest.approx(0.10)
    assert any("Range-day damp" in reason for reason in reasons)


def test_confidence_matches_the_logistic_used_at_birth():
    settings = get_settings()
    k = float(getattr(settings, "ut_confidence_logistic_k", 5.0) or 5.0)

    for total in (-0.4, -0.1, 0.0, 0.25, 0.6):
        assert confidence_from_total_score(total) == pytest.approx(1.0 / (1.0 + math.exp(-k * total)))


# ── the maturity re-grade ─────────────────────────────────────────────────────


def _candidate(**overrides):
    raw = SimpleNamespace(
        instrument="BANKNIFTY", timeframe="5min", signal_type="SELL",
        timestamp=datetime(2026, 9, 18, 10), adx_value=20.0,
        stop_distance=0.5, atr_value=1.0, raw_candle={}, trend_agreement=0,
    )
    SignalManager()._grade_signal(raw, 0.0, 0.0, "UNKNOWN", vix_value=15.0)
    values = dict(
        instrument="BANKNIFTY", timeframe="5min", direction="SHORT",
        signal_timestamp=raw.timestamp, grading_inputs=raw.grading_inputs,
        grade="C", confidence=0.10,
        grade_pillars={"tech": 0.05, "intel": -0.12, "regime": -0.25,
                       "vix_penalty": 0.0, "adjustments": 0.0, "total_score": -0.32},
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _scanner(intel, regime="TRENDING_DOWN"):
    scanner = Scanner.__new__(Scanner)
    scanner._diag_inc = lambda *args, **kwargs: None
    scanner._setting_enabled = lambda value, default: bool(default if value is None else value)
    if isinstance(intel, dict):
        intel = {**intel, "regime": {"regime": regime}}
    scanner._fresh_exact_trade_intel = lambda candidate, **kwargs: intel
    scanner._get_trade_regime = lambda candidate, default="UNKNOWN": regime
    return scanner


def _intel(score, day_type=""):
    return {
        "timeframe": "5min",
        "aggregate": {"score": score, "signal": "SELL" if score < 0 else "BUY"},
        "session_context": {"day_type": day_type, "confidence": 0.9 if day_type else 0.0},
    }


def test_a_signal_the_tape_now_agrees_with_is_upgraded():
    """The regime turned with the signal and intelligence turned negative (SHORT-aligned)
    while it waited: both pillars flip from penalty to support."""
    candidate = _candidate()
    scanner = _scanner(_intel(-60.0), regime="TRENDING_DOWN")

    telemetry = scanner._regrade_matured_candidate(candidate)

    assert telemetry is not None
    assert candidate.confidence > 0.10
    assert candidate.grade != "C"
    assert telemetry["pillars"]["regime"] == pytest.approx(0.30)
    assert telemetry["pillars"]["tech"] == pytest.approx(0.05), "the signal bar is unchanged"
    assert candidate.grade_regrade["previous_grade"] == "C"


def test_a_signal_the_tape_turned_against_is_downgraded():
    candidate = _candidate(grade="A", confidence=0.72,
                           grade_pillars={"tech": 0.20, "intel": 0.10, "regime": 0.30,
                                          "vix_penalty": 0.0, "adjustments": 0.0})
    scanner = _scanner(_intel(70.0), regime="TRENDING_UP")  # bullish tape, SHORT signal

    telemetry = scanner._regrade_matured_candidate(candidate)

    assert telemetry is not None
    assert candidate.confidence < 0.72
    assert telemetry["pillars"]["regime"] == pytest.approx(-0.25)


def test_the_regrade_reproduces_the_birth_time_formula():
    candidate = _candidate()
    scanner = _scanner(_intel(-40.0), regime="RANGING")

    telemetry = scanner._regrade_matured_candidate(candidate)

    intel_part, _ = intelligence_pillar_score(-40.0, "SELL")
    regime_part, _ = regime_pillar_score("RANGING", "SELL")
    expected_total = 0.05 + intel_part + regime_part
    assert telemetry["pillars"]["total_score"] == pytest.approx(round(expected_total, 4))
    assert candidate.confidence == pytest.approx(confidence_from_total_score(expected_total), abs=1e-6)


def test_post_grading_adjustments_are_recomputed(monkeypatch):
    """Quad-prime's actual agreement inputs are replayed, not an old numeric delta."""
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime")
    candidate = _candidate(confidence=0.19,
                           grade_pillars={"tech": 0.05, "intel": -0.12, "regime": -0.25,
                                          "vix_penalty": 0.0, "adjustments": 0.09})
    candidate.grading_inputs["signal"]["trend_agreement"] = 3
    scanner = _scanner(_intel(-40.0), regime="RANGING")

    scanner._regrade_matured_candidate(candidate)

    intel_part, _ = intelligence_pillar_score(-40.0, "SELL")
    expected = confidence_from_total_score(0.05 + intel_part - 0.10) + 0.09
    assert candidate.confidence == pytest.approx(expected, abs=1e-6)


def test_without_fresh_intelligence_the_birth_grade_stands():
    candidate = _candidate()
    scanner = _scanner(None)

    assert scanner._regrade_matured_candidate(candidate) is None
    assert (candidate.grade, candidate.confidence) == ("C", 0.10)


def test_a_candidate_without_full_grading_inputs_is_left_alone():
    candidate = _candidate(grading_inputs=None)
    scanner = _scanner(_intel(-60.0))

    assert scanner._regrade_matured_candidate(candidate) is None
    assert candidate.confidence == 0.10


def test_the_regrade_can_be_switched_off(monkeypatch):
    candidate = _candidate()
    scanner = _scanner(_intel(-60.0))
    monkeypatch.setattr(get_settings(), "stabilization_regrade_enabled", False)

    assert scanner._regrade_matured_candidate(candidate) is None
    assert (candidate.grade, candidate.confidence) == ("C", 0.10)


def test_the_maturity_path_calls_the_regrade():
    from pathlib import Path

    source = inspect.getsource(ExecutionService.coordinate_and_execute)
    matured = source.index("scanner._diag_inc(\"stabilization\", \"matured\")")
    assert "_regrade_matured_candidate" in source[matured:matured + 700], (
        "the re-grade must run on the matured candidate, before the live gate reads it"
    )
