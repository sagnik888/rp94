"""Buffer maturity must use the same complete grade and evidence as direct grading."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from engine.signal_manager import SignalManager
from engine.signal_processor import TradeCandidate, carry_grading_telemetry
from scanner import Scanner


@pytest.fixture(autouse=True)
def deterministic_grading(monkeypatch):
    settings = get_settings()
    for key, value in {
        "stabilization_regrade_enabled": True,
        "ut_confidence_calibration_enabled": False,
        "ut_confidence_logistic": True,
        "ut_confidence_logistic_k": 5.0,
        "ut_dynamic_confidence": False,
        "scanner_engine": "utbot",
    }.items():
        monkeypatch.setattr(settings, key, value)


def _signal(adx=45.0):
    return SimpleNamespace(
        instrument="NIFTY", timeframe="5min", signal_type="BUY",
        timestamp=datetime(2026, 9, 23, 10), price=110.0,
        trailing_stop=109.0, stop_distance=1.0, atr_value=1.0,
        adx_value=adx, position_state=1, bar_index=40,
        raw_candle={"open": 100.0, "close": 110.0, "high": 111.0, "low": 99.0},
        trend_agreement=0, source_engine="utbot", source_lane="",
    )


def _candidate(signal, intel=0.6, regime="TRENDING_UP", confluence=0.6):
    grade, confidence, reasons = SignalManager()._grade_signal(
        signal, confluence, intel, regime, vix_value=15.0,
    )
    candidate = SimpleNamespace(
        instrument=signal.instrument, timeframe=signal.timeframe, direction="LONG",
        source_signal_type="BUY", source_engine="utbot", source_lane="",
        signal_timestamp=signal.timestamp, grade=grade, confidence=confidence,
        reasons=reasons + ["Execution: fixed contract selected"], intelligence_score=intel,
        market_regime=regime, adx_value=signal.adx_value, price=100.0,
        market_intelligence_context={"aggregate": {"score_norm": intel}},
    )
    carry_grading_telemetry(candidate, signal)
    return candidate


def _scanner(score, regime, signal=None):
    scanner = Scanner.__new__(Scanner)
    scanner.signals = SignalManager()
    scanner._diag_inc = lambda *a, **kw: None
    scanner._get_trade_regime = lambda *a, **kw: "TRENDING_UP"
    scanner._fresh_exact_trade_intel = lambda *a, **kw: {
        "timeframe": "5min", "aggregate": {"score_norm": score},
        "regime": {"regime": regime}, "session_context": {},
    }
    scanner._cached_results = {"NIFTY_5min": {"signals": [signal]}} if signal else {}
    return scanner


@pytest.mark.parametrize("old_intel,new_intel", [(0.9, -0.4), (-0.4, 0.9)])
def test_exhaustion_cap_is_reapplied_to_current_evidence(old_intel, new_intel):
    signal = _signal()
    candidate = _candidate(signal, intel=old_intel, regime="RANGING")
    scanner = _scanner(new_intel, "RANGING")
    expected = SignalManager()._grade_signal(deepcopy(signal), 0.6, new_intel, "RANGING", vix_value=15)

    telemetry = scanner._regrade_matured_candidate(candidate)

    assert telemetry is not None
    assert candidate.grade == expected[0]
    assert candidate.confidence == pytest.approx(expected[1])
    assert candidate.grade_pillars["confidence_final"] == round(expected[1], 4)
    assert not scanner.signals.active_signals and not scanner.signals.signal_history


def test_maturity_replaces_admission_evidence_and_keeps_birth_snapshot():
    signal = _signal(adx=40)
    candidate = _candidate(signal)
    birth_confidence = candidate.confidence
    scanner = _scanner(-0.6, "RANGING")

    scanner._regrade_matured_candidate(candidate)

    assert candidate.intelligence_score == candidate.graded_intel_score == -0.6
    assert candidate.market_regime == candidate.graded_regime == "RANGING"
    assert candidate.graded_timeframe == "5min"
    assert not any("Institutional Volume/OI Confirmed" in reason for reason in candidate.reasons)
    assert any("CONTRADICTS" in reason for reason in candidate.reasons)
    assert "Execution: fixed contract selected" in candidate.reasons
    assert candidate.market_intelligence_context["aggregate"]["score_norm"] == -0.6
    assert candidate.grade_birth["confidence"] == birth_confidence
    assert candidate.grade_birth["intelligence_score"] == 0.6


def test_forming_setup_is_refreshed_from_exact_cached_signal():
    signal = _signal(adx=30)
    candidate = _candidate(signal)
    fresh = deepcopy(signal)
    fresh.raw_candle = {"open": 100.0, "close": 101.0, "high": 111.0, "low": 99.0}
    fresh.adx_value = 25.0
    scanner = _scanner(0.6, "TRENDING_UP", fresh)
    expected = SignalManager()._grade_signal(deepcopy(fresh), 0.6, 0.6, "TRENDING_UP", vix_value=15)

    scanner._regrade_matured_candidate(candidate)

    assert candidate.confidence == pytest.approx(expected[1])
    assert candidate.adx_value == 25.0
    assert any("Supply-Trap" in r for r in candidate.reasons)
    assert candidate.signal_timestamp == signal.timestamp
    assert candidate.price == 100.0, "raw index evidence must not overwrite contract geometry"


@pytest.mark.parametrize("changed", ["timestamp", "signal_type", "source_engine"])
def test_maturity_does_not_substitute_another_raw_signal(changed):
    signal = _signal(adx=30)
    candidate = _candidate(signal)
    unrelated = deepcopy(signal)
    setattr(unrelated, changed, {"timestamp": signal.timestamp + timedelta(minutes=5),
                               "signal_type": "SELL", "source_engine": "ema"}[changed])
    unrelated.raw_candle = {"open": 100.0, "close": 101.0, "high": 111.0, "low": 99.0}
    scanner = _scanner(0.6, "TRENDING_UP", unrelated)
    expected = candidate.confidence

    scanner._regrade_matured_candidate(candidate)

    assert candidate.confidence == pytest.approx(expected)
    assert candidate.signal_timestamp == signal.timestamp


def test_equal_candle_snapshot_refresh_keeps_identity_and_timer():
    signal = _signal(adx=30)
    original = _candidate(signal)
    fresh = deepcopy(signal)
    fresh.raw_candle["close"] = 101.0
    replacement = _candidate(fresh)
    scanner = _scanner(0.6, "TRENDING_UP")
    started = datetime(2026, 9, 23, 10, 0, 3)
    scanner._pending_live_signals = {"key": {
        "candidate": original, "timestamp": started, "sig_timestamp": signal.timestamp,
        "stable_scans": 2, "announced_seconds": 30.0,
    }}

    assert scanner._refresh_pending_live_signal("key", replacement, started + timedelta(seconds=20))
    pending = scanner._pending_live_signals["key"]
    assert pending["candidate"] is replacement
    assert pending["timestamp"] == started and pending["sig_timestamp"] == signal.timestamp
    assert pending["stable_scans"] == 2 and pending["announced_seconds"] == 30.0
    assert replacement.grade_birth["confidence"] == original.confidence


def test_grading_inputs_are_detached_from_mutating_raw_engine_signal():
    signal = _signal()
    candidate = _candidate(signal)
    signal.raw_candle["high"] = 999.0
    assert candidate.grading_inputs["signal"]["raw_candle"]["high"] == 111.0


def test_missing_full_inputs_does_not_recombine_frozen_adjustments():
    candidate = SimpleNamespace(grade="A", confidence=0.82, grade_pillars={"tech": 0.2, "adjustments": -0.1})
    assert _scanner(-0.6, "RANGING")._regrade_matured_candidate(candidate) is None
    assert candidate.confidence == 0.82


def test_nonlinear_calibration_is_recomputed(monkeypatch):
    monkeypatch.setattr("intelligence.confidence_calibration.calibrate", lambda value, *a, **kw: value ** 2)
    signal = _signal(adx=30)
    candidate = _candidate(signal)
    expected = SignalManager()._grade_signal(deepcopy(signal), 0.6, -0.4, "RANGING", vix_value=15)

    _scanner(-0.4, "RANGING")._regrade_matured_candidate(candidate)

    assert candidate.confidence == pytest.approx(expected[1])


def test_fresh_range_day_damp_matches_direct_grading(monkeypatch):
    monkeypatch.setattr(get_settings(), "range_day_regime_pillar_damp_enabled", True)
    monkeypatch.setattr(get_settings(), "range_day_regime_pillar_bonus_cap", 0.10)
    signal = _signal(adx=30)
    candidate = _candidate(signal)
    birth_confidence = candidate.confidence
    scanner = _scanner(0.6, "TRENDING_UP")
    scanner._fresh_exact_trade_intel = lambda *a, **kw: {
        "timeframe": "5min", "aggregate": {"score_norm": 0.6},
        "regime": {"regime": "TRENDING_UP"},
        "session_context": {
            "day_type": "TREND_DAY", "confidence": 0.2,
            "effective_day_type": "RANGE_DAY", "effective_confidence": 0.9,
        },
    }
    expected = SignalManager()._grade_signal(
        deepcopy(signal), 0.6, 0.6, "TRENDING_UP", vix_value=15,
        day_type="RANGE_DAY", day_type_confidence=0.9,
    )

    telemetry = scanner._regrade_matured_candidate(candidate)

    assert telemetry is not None
    assert candidate.grade == expected[0]
    assert candidate.confidence == pytest.approx(expected[1])
    assert candidate.confidence < birth_confidence
    assert candidate.grade_pillars["regime"] == pytest.approx(0.10)
    assert any("Range-day damp" in reason for reason in candidate.reasons)
    assert candidate.market_intelligence_context["session_context"]["effective_day_type"] == "RANGE_DAY"


def test_maturity_recomputes_confluence_from_current_cached_states():
    from engine.multi_timeframe import MultiTimeframeEngine

    signal = _signal(adx=30)
    candidate = _candidate(signal)
    scanner = _scanner(0.6, "TRENDING_UP")
    scanner.mtf = MultiTimeframeEngine.__new__(MultiTimeframeEngine)
    scanner.mtf.tf_weights = {"1min": 0.2, "5min": 0.3, "15min": 0.5}
    scanner._cached_results = {
        f"NIFTY_{tf}": {"state": {"position": -1, "is_trending": True}}
        for tf in scanner.mtf.tf_weights
    }
    expected = SignalManager()._grade_signal(deepcopy(signal), -1.0, 0.6, "TRENDING_UP", vix_value=15)

    scanner._regrade_matured_candidate(candidate)

    assert candidate.confidence == pytest.approx(expected[1])
    assert candidate.grading_inputs["confluence_score"] == -1.0


@pytest.mark.parametrize("field,value", [("timeframe", "15min"), ("timestamp", "2026-09-23T10:05:00"),
                                          ("instrument", "BANKNIFTY"), ("signal_type", "SELL")])
def test_mismatched_grading_snapshot_is_not_applied(field, value):
    candidate = _candidate(_signal())
    previous = candidate.confidence
    candidate.grading_inputs["signal"][field] = value
    assert _scanner(-0.6, "RANGING")._regrade_matured_candidate(candidate) is None
    assert candidate.confidence == previous


def test_warm_candidate_roundtrip_keeps_complete_regrade_inputs():
    signal = _signal(adx=30)
    original = _candidate(signal)
    candidate = TradeCandidate(
        instrument="NIFTY", timeframe="5min", direction="LONG", price=100.0,
        stop=95.0, target=110.0, lots=1, lot_size=65, grade=original.grade,
        confidence=original.confidence, inst_type="OPT", option_type="CE",
        atm_strike=25000, multiplier=1.0, trading_symbol="TESTCE", symbol_token="123",
    )
    candidate.__dict__.update(vars(original))
    scanner = _scanner(-0.6, "RANGING")
    scanner._manual_signal_exit_state = lambda c: {}
    payload = json.loads(json.dumps(scanner._serialize_trade_candidate(candidate)))
    restored = scanner._candidate_from_payload(payload)

    assert restored is not None
    assert restored.grading_inputs == original.grading_inputs
    assert scanner._regrade_matured_candidate(restored) is not None
    expected = SignalManager()._grade_signal(deepcopy(signal), 0.6, -0.6, "RANGING", vix_value=15)
    assert restored.confidence == pytest.approx(expected[1])
    assert restored.grade_birth["confidence"] == original.confidence
