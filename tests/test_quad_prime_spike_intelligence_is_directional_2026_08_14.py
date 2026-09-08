"""Regression coverage for the retired Quad Prime exceptional-spike bypass.

Raw provenance may choose scalp/runner handling after admission, but it may not own a
second quality policy. Signed intelligence therefore cannot admit a weak signal (or
silently veto a canonical verdict); the shared evidence-first verdict is the sole
quality decision.
"""
import inspect
from types import SimpleNamespace

from config.settings import Settings
from scanner import Scanner


def _scanner():
    scanner = Scanner.__new__(Scanner)
    scanner._quad_union_sources = lambda candidate: {"raw"}
    scanner._candidate_15m_trend_agrees = lambda candidate: True
    return scanner


def _candidate(intelligence_score):
    return SimpleNamespace(
        intelligence_score=intelligence_score,
        timeframe="5min",
        adx_value=30.0,
        grade="A",
        market_wide_reversal_entry=False,
        one_way_cascade_entry=False,
    )


def _quality(allowed):
    return SimpleNamespace(
        allowed=allowed,
        reason="canonical setup decision",
        reason_code="CANONICAL_TEST",
        route="canonical_test",
    )


def test_opposing_intelligence_cannot_rescue_a_canonical_rejection():
    allowed, scalp, reason = _scanner()._quad_prime_raw_entry_decision(
        _candidate(-1.0),
        regime="TRENDING_UP",
        confidence=0.95,
        risky_regime=False,
        momentum_override=True,
        quality=_quality(False),
    )

    assert allowed is False
    assert scalp is True
    assert "CANONICAL_TEST" in reason


def test_intelligence_sign_is_not_a_second_post_verdict_quality_gate():
    bullish = _scanner()._quad_prime_raw_entry_decision(
        _candidate(1.0),
        regime="TRENDING_UP",
        confidence=0.80,
        risky_regime=False,
        momentum_override=False,
        quality=_quality(True),
    )
    bearish = _scanner()._quad_prime_raw_entry_decision(
        _candidate(-1.0),
        regime="TRENDING_UP",
        confidence=0.80,
        risky_regime=False,
        momentum_override=False,
        quality=_quality(True),
    )

    assert bullish == bearish
    assert bullish[0] is True


def test_the_obsolete_exception_and_its_dead_setting_stay_removed():
    source = inspect.getsource(Scanner._quad_prime_raw_entry_decision)

    assert "intelligence_score" not in source
    assert "intel_aligned" not in source
    assert "exceptional_spike" not in source
    assert "quad_prime_raw_spike_min_intel_score" not in source
    assert "quad_prime_raw_spike_min_intel_score" not in Settings.model_fields
