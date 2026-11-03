"""The confirmed_market_shift route ships OFF.

The route let any market-shift detector hit (market-wide vote, LTP shock, cascade, late
reclaim) at confidence >= 0.60 skip the regime/ADX/confirmation fence. Every source agreed it
loses:
    real money      12 trades, 3 winners, -Rs.2,447
    shadow ledger   signals accepted through it: +101 / -277 / -574 per signal at 15/30/60 min,
                    negative in both halves at 30-60 min; the normal high_confidence_confirmed
                    route over the same 11 sessions: +1,299 / +906 / +440
With the route off, the same candidate meets the normal fence instead of bypassing it.
"""
import pytest

from config.settings import Settings
from engine.quality_verdict import QualityInputs, evaluate_quality, policy_configuration


def _settings(route_on):
    s = Settings()
    object.__setattr__(s, "quality_policy_market_shift_route_enabled", route_on)
    return s


def _shift(**overrides):
    """The shape the route admitted live: B+ in RANGING, low ADX, no 15m/impulse legs."""
    values = dict(
        timeframe="5min", inst_type="OPT", confidence=0.65, grade="B+", regime="RANGING",
        adx_value=16.0, trend_15m_agrees=False, direction="LONG",
        market_shift_confirmed=True, market_shift_core_votes=2, market_shift_total_votes=3,
        volume_available=True, volume_basis="TRADED", volume_ratio=1.40,
    )
    values.update(overrides)
    return QualityInputs(**values)


def test_the_route_ships_off():
    assert Settings().quality_policy_market_shift_route_enabled is False
    assert policy_configuration(Settings())["market_shift_route_enabled"] is False


def test_with_the_route_off_a_market_shift_meets_the_normal_fence():
    verdict = evaluate_quality(_shift(), _settings(False))

    assert not verdict.allowed
    assert verdict.route != "confirmed_market_shift"
    assert verdict.reason_code == "BPLUS_RISKY_SETUP_INSUFFICIENT"


def test_the_same_candidate_was_admitted_with_the_route_on():
    """Proves the test above is refusing because of the switch, not something else."""
    verdict = evaluate_quality(_shift(), _settings(True))

    assert verdict.allowed and verdict.route == "confirmed_market_shift"


def test_a_setup_that_clears_the_fence_is_still_admitted_with_the_route_off():
    strong = _shift(
        confidence=0.80, grade="A", regime="TRENDING_UP", adx_value=30.0,
        trend_15m_agrees=True, impulse_confirmed=True,
    )

    verdict = evaluate_quality(strong, _settings(False))

    assert verdict.allowed
    assert verdict.route != "confirmed_market_shift"


def test_full_core_market_shift_is_not_governed_by_this_switch():
    full_core = _shift(
        confidence=0.3645, grade="C", adx_value=18.01,
        market_shift_core_votes=3, market_shift_total_votes=4,
    )

    verdict = evaluate_quality(full_core, _settings(False))

    assert verdict.allowed and verdict.reason_code == "FULL_CORE_MARKET_SHIFT"


@pytest.mark.parametrize("regime", ["RANGING", "VOLATILE", "UNKNOWN", "TRENDING_UP"])
def test_route_off_never_admits_what_route_on_refuses(regime):
    """Switching a bypass off can only refuse more; it must never admit something new."""
    for adx in (10.0, 20.0, 30.0):
        for conf in (0.56, 0.62, 0.70, 0.85):
            grade = "A" if conf >= 0.70 else "B+"
            inputs = _shift(confidence=conf, grade=grade, regime=regime, adx_value=adx)
            off = evaluate_quality(inputs, _settings(False)).allowed
            on = evaluate_quality(inputs, _settings(True)).allowed
            assert not (off and not on), f"{regime} adx={adx} conf={conf}: off admitted, on refused"


def test_env_carries_the_switch_explicitly():
    """.env is the real settings store; the value is set there, not left to a default."""
    from pathlib import Path

    env = Path(__file__).resolve().parents[1].joinpath(".env").read_text(encoding="utf-8")
    assert "QUALITY_POLICY_MARKET_SHIFT_ROUTE_ENABLED='False'" in env
