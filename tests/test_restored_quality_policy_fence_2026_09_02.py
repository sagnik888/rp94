from copy import deepcopy
from types import SimpleNamespace

from config.settings import get_settings
from engine.quality_verdict import POLICY_VERSION, QualityInputs, evaluate_quality
from scanner import Scanner


def _accepted_candidate(quality):
    return SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        inst_type="FUT",
        grade="B+",
        confidence=0.60,
        market_regime="RANGING",
        quality_verdict=deepcopy(quality),
        accepted_by_gate=True,
        action="ENTRY",
    )


def _current_allowed_quality():
    return evaluate_quality(
        QualityInputs(
            timeframe="5min",
            inst_type="FUT",
            confidence=0.60,
            regime="RANGING",
            adx_value=25.0,
            trend_15m_agrees=True,
            grade="B+",
            volume_ratio=1.30,
        ),
        get_settings(),
    ).to_dict()


def test_matching_current_allowed_verdict_restores_without_regrading():
    scanner = Scanner.__new__(Scanner)
    quality = _current_allowed_quality()
    candidate = _accepted_candidate(quality)

    assert quality["allowed"] is True
    assert scanner._restored_entry_quality_is_current(candidate) is True
    assert candidate.quality_verdict == quality


def test_stale_policy_is_replayed_from_its_persisted_evidence():
    scanner = Scanner.__new__(Scanner)
    stale = _current_allowed_quality()
    stale["policy_version"] = "quality_verdict.v0-stale"
    stale["configuration_hash"] = "stale"
    candidate = _accepted_candidate(stale)

    assert scanner._restored_entry_quality_is_current(candidate) is True
    assert candidate.quality_verdict["policy_version"] == POLICY_VERSION
    assert candidate.quality_verdict["configuration_hash"] != "stale"
    assert candidate.quality_verdict["checks"]["lane"] == "RESTORE"


def test_stale_allowed_bit_cannot_override_weak_persisted_evidence():
    scanner = Scanner.__new__(Scanner)
    stale = _current_allowed_quality()
    stale.update(
        {
            "allowed": True,
            "policy_version": "quality_verdict.v0-stale",
            "configuration_hash": "stale",
        }
    )
    stale["checks"]["adx"] = 10.0
    candidate = _accepted_candidate(stale)

    assert scanner._restored_entry_quality_is_current(candidate) is False
    assert candidate.quality_verdict["allowed"] is False
    assert candidate.quality_verdict["reason_code"] == "BPLUS_RISKY_SETUP_INSUFFICIENT"


def test_legacy_accepted_row_without_evidence_fails_closed():
    scanner = Scanner.__new__(Scanner)

    assert scanner._restored_entry_quality_is_current(_accepted_candidate({})) is False
