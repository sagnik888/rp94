"""Replay/recovery/EOD must build the same explicit canonical quality evidence."""

from inspect import getsource
from types import SimpleNamespace

from engine.signal_processor import SignalProcessor
from tests.source_views import signal_pipeline_source


def _settings():
    return SimpleNamespace(
        quality_policy_bplus_min_confidence=0.55,
        live_5min_quality_min_confidence=0.70,
        quality_policy_bplus_min_adx=22.0,
        quality_policy_risky_bplus_min_adx=25.0,
        quality_policy_min_volume_ratio=1.20,
        momentum_override_threshold=25.0,
        ut_regime_adaptation=True,
    )


def _processor():
    processor = SignalProcessor.__new__(SignalProcessor)
    processor.scanner = SimpleNamespace(mode="HISTORICAL")
    return processor


def _signal(**overrides):
    values = {
        "adx_value": 25.0,
        "volume_ratio": 0.0,
        # An opposing high-magnitude score must not become an impulse leg.
        "intelligence_score": -0.95,
        "impulse_confirmed": False,
        "is_explosive_bypass": False,
        "power_move": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _verdict(processor, signal, **overrides):
    values = {
        "tf": "5min",
        "grade": "B+",
        "confidence": 0.60,
        "regime": "TRENDING_UP",
        "inst_type": "FUT",
        "settings": _settings(),
        "trend_15m_agrees": True,
        "source_count": 1,
    }
    values.update(overrides)
    return processor._quality_verdict_for_signal(signal, **values)


def test_high_adx_or_absolute_intel_does_not_manufacture_an_impulse_leg():
    verdict = _verdict(_processor(), _signal(adx_value=35.0, intelligence_score=-0.99))

    assert verdict.allowed is False
    assert verdict.checks["adx"] == 35.0
    assert verdict.checks["impulse_confirmed"] is False
    assert verdict.checks["confirmation_count"] == 1  # 15m trend only


def test_explicit_structural_impulse_and_power_flags_are_preserved():
    processor = _processor()

    impulse = _verdict(processor, _signal(impulse_confirmed=True))
    power = _verdict(processor, _signal(power_move=True))
    measured = _verdict(
        processor,
        _signal(),
        structural_impulse=True,
        structural_power=True,
    )

    assert impulse.allowed and impulse.checks["impulse_confirmed"] is True
    assert power.allowed and power.checks["power_move"] is True
    assert measured.allowed
    assert measured.checks["impulse_confirmed"] is True
    assert measured.checks["power_move"] is True


def test_shared_callsite_passes_measured_adx_instead_of_defaulting_eod_to_zero():
    verdict = _verdict(
        _processor(),
        _signal(adx_value=22.0),
        grade="A",
        confidence=0.70,
    )

    assert verdict.allowed is True
    assert verdict.checks["adx"] == 22.0
    assert verdict.checks["confirmation_count"] == 1


def test_historical_recovery_and_eod_use_the_shared_evidence_callsite():
    source = signal_pipeline_source()

    assert "hist_quality_verdict = self._quality_verdict_for_signal(" in source
    assert "rec_quality_verdict = self._quality_verdict_for_signal(" in source
    assert "eod_quality_verdict = self._quality_verdict_for_signal(" in source
    assert "final_native_verdict = self._quality_verdict_for_signal(" in source
    assert source.count("self._quality_verdict_for_signal(") == 4
