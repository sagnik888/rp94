"""Risky-regime B+ admission needs evidence the candidate was not built from.

2026-10-07 live: NIFTY 15m SHORT (09:30) and SENSEX 15m LONG (10:30), both VOLATILE,
both admitted as bplus_risky_confirmed on ADX + 15m trend + multi_source, both lost.
A quad-union raw entry exists because two engines agreed, so multi_source was always
true and the "independent" leg never constrained anything. Live ledger 2026-09-15..10-07:
the six risky B+ admissions resting only on trend + multi-source lost Rs.2,751 with one
winner; the three carrying impulse or volume made Rs.99.
"""
from types import SimpleNamespace

import pytest

from engine.quality_verdict import POLICY_VERSION, QualityInputs, evaluate_quality


def _settings(**overrides):
    base = dict(
        quality_policy_bplus_min_confidence=0.55, live_5min_quality_min_confidence=0.70,
        quality_policy_bplus_min_adx=22.0, quality_policy_risky_bplus_min_adx=25.0,
        quality_policy_min_volume_ratio=1.20, quality_policy_market_shift_route_enabled=False,
        stale_regime_reversal_enabled=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _today(**overrides):
    """The evidence the two 2026-10-07 losers carried (SENSEX row)."""
    base = dict(
        timeframe="15min", inst_type="OPT", confidence=0.6652, grade="B+", regime="VOLATILE",
        adx_value=28.8, trend_15m_agrees=True, impulse_confirmed=False, source_count=2,
        volume_available=False, volume_basis="UNAVAILABLE", volume_ratio=1.0,
        market_shift_confirmed=True, market_shift_core_votes=3, market_shift_total_votes=4,
    )
    base.update(overrides)
    return QualityInputs(**base)


def test_union_agreement_alone_no_longer_admits_a_risky_bplus():
    verdict = evaluate_quality(_today(), _settings())
    assert not verdict.allowed
    assert verdict.reason_code == "BPLUS_RISKY_SETUP_INSUFFICIENT"
    assert "confirmations" in verdict.checks["binding_legs"]
    assert verdict.checks["independent_leg"] is False


@pytest.mark.parametrize("extra", [
    dict(impulse_confirmed=True),
    dict(power_move=True),
    dict(volume_available=True, volume_basis="EXCHANGE_VOLUME", volume_ratio=1.25),
])
def test_control_an_independent_leg_still_admits(extra):
    verdict = evaluate_quality(_today(**extra), _settings())
    assert verdict.allowed and verdict.route == "bplus_risky_confirmed"
    assert verdict.checks["independent_leg"] is True


def test_proxy_volume_is_not_an_independent_leg():
    verdict = evaluate_quality(
        _today(volume_available=True, volume_basis="TICK_ACTIVITY", volume_ratio=3.0), _settings()
    )
    assert not verdict.allowed


def test_operator_can_restore_the_old_rule():
    verdict = evaluate_quality(_today(), _settings(quality_policy_risky_bplus_count_multi_source=True))
    assert verdict.allowed and verdict.route == "bplus_risky_confirmed"


def test_trending_regime_admission_is_unchanged():
    # The profitable non-risky route still accepts multi-source as a confirmation leg.
    verdict = evaluate_quality(_today(regime="TRENDING_UP", confidence=0.72, grade="A"), _settings())
    assert verdict.allowed and verdict.route == "high_confidence_confirmed"


def test_policy_change_is_versioned_and_hashed():
    assert POLICY_VERSION == "quality_verdict.v9-independent-risky-evidence"
    on = evaluate_quality(_today(impulse_confirmed=True), _settings())
    off = evaluate_quality(
        _today(impulse_confirmed=True), _settings(quality_policy_risky_bplus_count_multi_source=True)
    )
    assert on.configuration_hash != off.configuration_hash


def test_setting_is_declared_with_the_fix_on():
    from config.settings import Settings

    assert Settings().quality_policy_risky_bplus_count_multi_source is False
