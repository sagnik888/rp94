"""Policy migration must preserve the evidence saved with an accepted entry."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from engine.quality_verdict import QualityInputs, evaluate_quality
from scanner import Scanner


def _saved_candidate(monkeypatch, **evidence):
    settings = get_settings()
    monkeypatch.setattr(settings, "quality_policy_risky_bplus_count_multi_source", False)
    values = dict(
        timeframe="15min", inst_type="OPT", confidence=0.65, grade="B+",
        regime="VOLATILE", adx_value=28.0, trend_15m_agrees=True, source_count=2,
        volume_ratio=3.0, volume_available=False, volume_basis="UNAVAILABLE",
    )
    values.update(evidence)
    quality = evaluate_quality(QualityInputs(**values), settings).to_dict()
    # The prior policy admitted this row; only its recorded evidence is trustworthy.
    quality.update(allowed=True, policy_version="quality_verdict.v8-old", configuration_hash="old")
    return SimpleNamespace(
        instrument="NIFTY", direction="LONG", action="ENTRY", accepted_by_gate=True,
        timeframe=values["timeframe"], inst_type=values["inst_type"],
        confidence=values["confidence"], grade=values["grade"],
        market_regime=values["regime"], quality_verdict=deepcopy(quality),
    )


@pytest.mark.parametrize("available,basis", [
    (False, "UNAVAILABLE"), (True, "TICK_ACTIVITY"), (True, "PROXY"),
])
def test_policy_restore_does_not_turn_unmeasured_volume_into_confirmation(monkeypatch, available, basis):
    candidate = _saved_candidate(monkeypatch, volume_available=available, volume_basis=basis)

    assert Scanner.__new__(Scanner)._restored_entry_quality_is_current(candidate) is False
    checks = candidate.quality_verdict["checks"]
    assert checks["volume_available"] is available
    assert checks["volume_basis"] == basis
    assert checks["volume_confirmed"] is False


def test_policy_restore_keeps_measured_volume_and_calibration_provenance(monkeypatch):
    candidate = _saved_candidate(
        monkeypatch, volume_available=True, volume_basis="EXCHANGE_VOLUME",
        confidence_calibrated=True, adx_available=False,
    )

    assert Scanner.__new__(Scanner)._restored_entry_quality_is_current(candidate) is True
    checks = candidate.quality_verdict["checks"]
    assert checks["volume_available"] is True
    assert checks["volume_basis"] == "EXCHANGE_VOLUME"
    assert checks["volume_confirmed"] is True
    assert checks["confidence_calibrated"] is True
    assert checks["adx_measured"] is False


def test_policy_restore_missing_volume_provenance_cannot_prove_an_independent_leg(monkeypatch):
    candidate = _saved_candidate(monkeypatch, volume_available=True, volume_basis="EXCHANGE_VOLUME")
    checks = candidate.quality_verdict["checks"]
    del checks["volume_available"]
    del checks["volume_basis"]

    assert Scanner.__new__(Scanner)._restored_entry_quality_is_current(candidate) is False

