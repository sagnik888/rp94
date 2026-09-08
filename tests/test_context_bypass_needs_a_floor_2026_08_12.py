"""Market-wide/cascade context may not bypass canonical setup quality.

The 2026-08-12 defect admitted two C-grade 12--13% signals because a context flag was
treated as a quality licence. A later patch replaced that with a separate B/50% helper,
which still duplicated policy and encoded one profitable observation as an admission
rule. Quality now has one owner: context can affect routing/exposure only after the
canonical evidence-first verdict passes.
"""
import inspect
from types import SimpleNamespace

import pytest

from config.settings import Settings
from engine.signal_quality_policy import confidence_to_grade
from scanner import Scanner


def _scanner():
    scanner = Scanner.__new__(Scanner)
    scanner._quad_union_sources = lambda candidate: set(candidate.source_engines)
    scanner._candidate_15m_trend_agrees = lambda candidate: bool(
        getattr(candidate, "trend_15m_agrees", False)
    )
    return scanner


def _candidate(confidence, grade=None, peers=("BANKNIFTY",), **evidence):
    candidate = SimpleNamespace(
        instrument="NIFTY",
        direction="LONG",
        inst_type="OPT",
        timeframe="15min",
        grade=grade or confidence_to_grade(confidence),
        confidence=confidence,
        adx_value=0.0,
        trend_15m_agrees=False,
        impulse_confirmed=False,
        is_explosive_bypass=False,
        intelligence_score=0.0,
        volume_spike_entry=False,
        volume_ratio=0.0,
        power_move=False,
        market_wide_reversal_entry=False,
        one_way_cascade_entry=False,
        source_engine="utbot",
        source_engines=["utbot"],
        market_wide_reversal_context={
            "core_indices": list(peers),
            "indices": list(peers),
        },
    )
    for key, value in evidence.items():
        setattr(candidate, key, value)
    return candidate


def _decide(candidate, regime="TRENDING_DOWN"):
    return _scanner()._quad_prime_raw_entry_decision(
        candidate,
        regime=regime,
        confidence=float(candidate.confidence),
        risky_regime=regime in {"RANGING", "VOLATILE"},
        momentum_override=False,
    )


@pytest.mark.parametrize("flag", ["market_wide_reversal_entry", "one_way_cascade_entry"])
@pytest.mark.parametrize("confidence", [0.12, 0.13, 0.49, 0.54])
def test_context_cannot_admit_any_signal_below_bplus(flag, confidence):
    allowed, _scalp, reason = _decide(_candidate(confidence, **{flag: True}))

    assert allowed is False
    assert "BELOW_BPLUS_ELIGIBILITY" in reason


@pytest.mark.parametrize("flag", ["market_wide_reversal_entry", "one_way_cascade_entry"])
def test_context_cannot_rescue_weak_bplus_without_setup_evidence(flag):
    allowed, _scalp, reason = _decide(_candidate(0.60, **{flag: True}))

    assert allowed is False
    assert "BPLUS_SETUP_INSUFFICIENT" in reason


@pytest.mark.parametrize("flag", ["market_wide_reversal_entry", "one_way_cascade_entry"])
def test_context_routes_only_after_a_confirmed_bplus_setup_passes(flag):
    candidate = _candidate(
        0.60,
        adx_value=30.0,
        trend_15m_agrees=True,
        volume_spike_entry=True,
        volume_ratio=1.5,
        **{flag: True},
    )
    allowed, _scalp, reason = _decide(candidate)

    assert allowed is True, reason
    assert candidate.quality_verdict["route"] == "bplus_setup_confirmed"


def test_market_wide_peer_names_do_not_create_an_admission_leg():
    for peers in ((), ("NIFTY",), ("SENSEX",), ("MIDCPNIFTY", "SENSEX")):
        allowed, _scalp, reason = _decide(
            _candidate(0.54, peers=peers, market_wide_reversal_entry=True)
        )
        assert allowed is False, (peers, reason)


def test_the_observed_b_54_winner_remains_shadow_evidence_not_policy():
    candidate = _candidate(
        0.54,
        peers=("MIDCPNIFTY", "NIFTY", "SENSEX"),
        market_wide_reversal_entry=True,
        adx_value=30.0,
        trend_15m_agrees=True,
        volume_spike_entry=True,
        volume_ratio=2.0,
    )

    allowed, _scalp, reason = _decide(candidate)

    assert allowed is False
    assert "BELOW_BPLUS_ELIGIBILITY" in reason


def test_risky_regime_still_requires_trend_plus_independent_evidence():
    weak = _candidate(0.60, adx_value=25.0, trend_15m_agrees=True)
    strong = _candidate(
        0.60,
        adx_value=25.0,
        trend_15m_agrees=True,
        impulse_confirmed=True,
    )

    assert _decide(weak, regime="RANGING")[0] is False
    assert _decide(strong, regime="RANGING")[0] is True


def test_a_confirmed_high_confidence_signal_is_unaffected():
    candidate = _candidate(0.75, adx_value=30.0, trend_15m_agrees=True)

    allowed, scalp, reason = _decide(candidate)

    assert allowed is True, reason
    assert scalp is False


def test_the_duplicate_context_quality_controls_stay_removed():
    scanner_source = inspect.getsource(Scanner)

    assert "_market_wide_candidate_quality" not in scanner_source
    assert "market_wide_reversal_min_grade" not in Settings.model_fields
    assert "market_wide_reversal_min_confidence" not in Settings.model_fields
    assert "quad_prime_raw_min_confidence" not in Settings.model_fields
