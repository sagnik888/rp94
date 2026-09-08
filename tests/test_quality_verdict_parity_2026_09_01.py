"""The quality policy has one evaluator, one threshold surface and durable evidence.

This replaces the old parity test against a known-divergent live-only implementation.
It sweeps the actual evaluator and the legacy/replay wrapper, so historical, recovery
and EOD callers cannot silently use a different quality rule.
"""
from itertools import product

import pytest

from config.settings import get_settings
from engine.quality_verdict import POLICY_VERSION, QualityInputs, evaluate_quality
from engine.signal_quality_policy import confidence_to_grade, passes_quality_gate


REGIMES = ("TRENDING_UP", "TRENDING_DOWN", "BREAKOUT", "VOLATILE", "RANGING", "UNKNOWN")
TIMEFRAMES = ("5min", "15min")
INSTRUMENTS = ("OPT", "FUT")
ADX_VALUES = (10.0, 22.0, 25.0, 35.0)
BOOLS = (False, True)


def _verdict(confidence, regime, timeframe, inst_type, adx, trend, impulse, volume, sources=1):
    return evaluate_quality(
        QualityInputs(
            timeframe=timeframe,
            inst_type=inst_type,
            confidence=confidence,
            regime=regime,
            adx_value=adx,
            trend_15m_agrees=trend,
            impulse_confirmed=impulse,
            risky_regime=regime in {"VOLATILE", "RANGING", "UNKNOWN"},
            is_choppy=regime in {"VOLATILE", "RANGING"},
            grade=confidence_to_grade(confidence),
            source_count=sources,
            volume_confirmed=volume,
            volume_ratio=1.3 if volume else 0.0,
            lane="LIVE",
        ),
        get_settings(),
    )


def test_replay_wrapper_uses_the_same_policy_for_every_supported_lane():
    """No live/historical/recovery divergence is permitted in the quality decision."""
    settings = get_settings()
    mismatches = []
    for regime, timeframe, inst_type, adx, trend, impulse, volume in product(
        REGIMES, TIMEFRAMES, INSTRUMENTS, ADX_VALUES, BOOLS, BOOLS, BOOLS
    ):
        for step in range(35, 100, 5):
            confidence = step / 100.0
            verdict = _verdict(
                confidence, regime, timeframe, inst_type, adx, trend, impulse, volume
            )
            wrapped = passes_quality_gate(
                timeframe,
                confidence_to_grade(confidence),
                confidence,
                regime,
                inst_type,
                settings,
                mode="HISTORICAL",
                adx_value=adx,
                trend_15m_agrees=trend,
                # The wrapper's legacy intel signal is its impulse equivalent.
                intel_score=0.8 if impulse else 0.0,
                volume_ratio=1.3 if volume else 0.0,
                fifteen_min_momentum_override=impulse,
            )
            if bool(verdict) != bool(wrapped):
                mismatches.append(
                    f"{regime}/{timeframe}/{inst_type} conf={confidence:.0%} adx={adx} "
                    f"trend={trend} impulse={impulse} volume={volume}: "
                    f"evaluator={verdict.allowed} wrapper={wrapped}"
                )
    assert not mismatches, "; ".join(mismatches[:5])


def test_replay_preserves_multi_source_evidence_instead_of_hardcoding_solo():
    settings = get_settings()
    live = _verdict(0.60, "TRENDING_UP", "5min", "OPT", 22.0,
                    False, False, True, sources=2)
    replay = passes_quality_gate(
        "5min", "B+", 0.60, "TRENDING_UP", "OPT", settings,
        mode="HISTORICAL", adx_value=22.0, trend_15m_agrees=False,
        volume_ratio=1.3, source_count=2,
    )
    assert live.allowed and replay


def test_power_move_is_confirmation_evidence_not_a_quality_bypass():
    settings = get_settings()
    assert not passes_quality_gate(
        "5min", "C", 0.20, "TRENDING_UP", "OPT", settings,
        mode="HISTORICAL", adx_value=35.0, trend_15m_agrees=True,
        power_move=True,
    )


def test_weak_solo_bplus_is_rejected_in_every_noisy_regime():
    for regime in ("VOLATILE", "RANGING", "UNKNOWN"):
        verdict = _verdict(0.55, regime, "5min", "OPT", 10.0, False, False, False)
        assert not verdict.allowed
        assert verdict.reason_code in {
            "BPLUS_RISKY_SETUP_INSUFFICIENT", "UNKNOWN_REGIME_SETUP_INSUFFICIENT",
        }


def test_good_bplus_setup_is_allowed_without_using_tracker_outcomes():
    verdict = _verdict(0.60, "VOLATILE", "5min", "OPT", 25.0, True, True, False)
    assert verdict.allowed
    assert verdict.route == "bplus_risky_confirmed"
    assert verdict.configuration_hash
    assert verdict.to_dict()["policy_version"] == POLICY_VERSION


@pytest.mark.parametrize("regime", REGIMES)
@pytest.mark.parametrize("adx", ADX_VALUES)
def test_admission_is_monotonic_in_confidence(regime, adx):
    for trend, impulse, volume in product(BOOLS, BOOLS, BOOLS):
        ladder = [
            _verdict(c / 100.0, regime, "5min", "OPT", adx, trend, impulse, volume).allowed
            for c in range(35, 100, 5)
        ]
        assert all(not before or after for before, after in zip(ladder, ladder[1:])), (
            regime, adx, trend, impulse, volume, ladder
        )


def test_single_named_standalone_floor_is_consistent_in_reason_and_checks(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_5min_quality_min_confidence", 0.70, raising=False)
    verdict = _verdict(0.64, "TRENDING_UP", "5min", "OPT", 10.0, False, False, False)
    assert not verdict.allowed
    assert verdict.reason_code == "BPLUS_SETUP_INSUFFICIENT"
    config = verdict.checks["configuration"]
    assert config["standalone_5min_confidence"] == pytest.approx(0.70)
    assert "ADX >= 22" in verdict.reason


def test_old_ut_5min_knob_cannot_override_the_quality_floor(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_5min_quality_min_confidence", 0.70, raising=False)
    monkeypatch.setattr(settings, "ut_5min_option_min_confidence", 0.99, raising=False)
    assert not _verdict(0.70, "TRENDING_UP", "5min", "OPT", 0.0, False, False, False).allowed
    assert _verdict(0.70, "TRENDING_UP", "5min", "OPT", 22.0, True, False, False).allowed


@pytest.mark.parametrize(
    "grade,confidence,regime",
    (("C", 0.95, "CHOPPY"), ("B", 0.60, "CHOPPY"), ("A", 0.68, "TRENDING_UP")),
)
def test_malformed_grade_confidence_guardrails_fail_closed(grade, confidence, regime):
    verdict = evaluate_quality(
        QualityInputs(
            timeframe="5min", inst_type="OPT", confidence=confidence,
            regime=regime, adx_value=35.0, trend_15m_agrees=True,
            impulse_confirmed=True, volume_confirmed=True, source_count=2,
            grade=grade,
        ),
        get_settings(),
    )
    assert not verdict.allowed
    assert verdict.reason_code == "GRADE_CONFIDENCE_MISMATCH"
