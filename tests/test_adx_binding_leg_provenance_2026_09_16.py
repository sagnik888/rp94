"""A refused verdict must name the leg that bound, and say whether ADX was measured.

``adx_value`` reaches the quality policy as 0.0 from three separate upstream NaN
guards, so for 326 of 583 archived risky refusals the recorded ADX of 0.0 meant "no
reading", not "no trend" -- while the refusal text still read "needs ADX >= 25".  That
made every ADX-shaped refusal unattributable.  These tests pin the attribution and,
just as importantly, pin that the attribution changed no admission: the routes still
compare the numeric ``adx`` alone, and ``adx_available`` never moves a decision.
"""
from itertools import product

import pytest

from config.settings import Settings
from engine.quality_verdict import QualityInputs, evaluate_quality


ADX_LEGS = {"adx_unavailable", "adx_below_threshold"}


@pytest.fixture(scope="module")
def settings():
    # A fresh Settings() so a leaked singleton mutation from another module cannot
    # move the thresholds this test reasons about.
    return Settings()


def _inputs(**overrides):
    base = dict(
        timeframe="5min",
        inst_type="OPT",
        confidence=0.60,
        regime="RANGING",
        adx_value=0.0,
        trend_15m_agrees=False,
        impulse_confirmed=False,
        risky_regime=True,
        is_choppy=True,
        grade="B+",
        source_count=1,
        volume_confirmed=False,
        volume_ratio=0.0,
        lane="LIVE",
    )
    base.update(overrides)
    return QualityInputs(**base)


def test_missing_adx_is_named_unavailable_not_weak_trend(settings):
    verdict = evaluate_quality(_inputs(adx_value=0.0), settings)
    checks = verdict.checks

    assert verdict.allowed is False
    assert checks["adx_basis"] == "UNAVAILABLE"
    assert checks["adx_measured"] is False
    assert "adx_unavailable" in checks["binding_legs"]
    assert "adx_below_threshold" not in checks["binding_legs"]


def test_measured_weak_adx_is_named_below_threshold(settings):
    verdict = evaluate_quality(_inputs(adx_value=11.0), settings)
    checks = verdict.checks

    assert verdict.allowed is False
    assert checks["adx_basis"] == "MEASURED"
    assert checks["adx_measured"] is True
    assert "adx_below_threshold" in checks["binding_legs"]
    assert "adx_unavailable" not in checks["binding_legs"]


def test_every_failing_leg_is_listed_not_only_the_first(settings):
    """The archived rows that fail three legs must not read as an ADX-only refusal."""
    verdict = evaluate_quality(
        _inputs(adx_value=0.0, trend_15m_agrees=False, source_count=1), settings
    )

    assert verdict.reason_code == "BPLUS_RISKY_SETUP_INSUFFICIENT"
    assert set(verdict.checks["binding_legs"]) == {
        "adx_unavailable", "trend_15m", "confirmations"
    }


def test_a_leg_that_passed_is_never_listed_as_binding(settings):
    verdict = evaluate_quality(
        _inputs(adx_value=40.0, trend_15m_agrees=True, source_count=1), settings
    )

    assert verdict.allowed is False
    assert verdict.checks["binding_legs"] == ["confirmations"]


def test_below_bplus_refusal_binds_on_the_confidence_floor(settings):
    verdict = evaluate_quality(_inputs(confidence=0.40, grade=""), settings)

    assert verdict.reason_code == "BELOW_BPLUS_ELIGIBILITY"
    assert verdict.checks["binding_legs"] == ["confidence_floor"]


def test_an_admitted_verdict_reports_no_binding_leg(settings):
    verdict = evaluate_quality(
        _inputs(
            adx_value=40.0,
            trend_15m_agrees=True,
            impulse_confirmed=True,
            source_count=2,
        ),
        settings,
    )

    assert verdict.allowed is True
    assert verdict.checks["binding_legs"] == []


def test_adx_availability_is_attribution_only_and_never_admits_or_refuses(settings):
    """The flag may relabel the leg; it may never change who gets in.

    Admission still reads the numeric ADX, so an unavailable-but-numeric ADX above the
    threshold is admitted exactly as before, and the flag cannot rescue a 0.0.
    """
    grid = product(
        (0.0, 11.0, 26.0, 40.0),   # adx
        (False, True),             # trend_15m_agrees
        (1, 2),                    # source_count
        ("RANGING", "TRENDING_UP", "UNKNOWN"),
        (0.60, 0.75, 0.90),        # confidence
    )
    for adx, trend, sources, regime, confidence in grid:
        risky = regime in {"RANGING", "UNKNOWN"}
        common = dict(
            adx_value=adx,
            trend_15m_agrees=trend,
            source_count=sources,
            regime=regime,
            risky_regime=risky,
            is_choppy=regime == "RANGING",
            confidence=confidence,
            grade="",
        )
        available = evaluate_quality(_inputs(adx_available=True, **common), settings)
        missing = evaluate_quality(_inputs(adx_available=False, **common), settings)

        assert available.allowed == missing.allowed, (adx, trend, sources, regime)
        assert available.route == missing.route
        assert available.reason_code == missing.reason_code


def test_recorded_adx_value_is_never_rewritten_by_the_provenance_flag(settings):
    """Downstream replay reads ``checks['adx']``; it must stay the number that decided."""
    verdict = evaluate_quality(_inputs(adx_value=31.5, adx_available=False), settings)

    assert verdict.checks["adx"] == pytest.approx(31.5)
    assert verdict.checks["adx_basis"] == "UNAVAILABLE"


def test_named_legs_stay_within_the_documented_vocabulary(settings):
    """A typo in a leg name would silently break every attribution query."""
    allowed_names = ADX_LEGS | {"grade_consistency", "confidence_floor", "trend_15m", "confirmations"}
    seen = set()
    for adx, trend, sources, regime, confidence, volume in product(
        (0.0, 11.0, 26.0), (False, True), (1, 2),
        ("RANGING", "TRENDING_UP", "UNKNOWN"), (0.40, 0.60, 0.80), (False, True),
    ):
        risky = regime in {"RANGING", "UNKNOWN"}
        verdict = evaluate_quality(
            _inputs(
                adx_value=adx, trend_15m_agrees=trend, source_count=sources,
                regime=regime, risky_regime=risky, is_choppy=regime == "RANGING",
                confidence=confidence, grade="", volume_confirmed=volume,
                volume_ratio=1.5 if volume else 0.0,
            ),
            settings,
        )
        legs = verdict.checks["binding_legs"]
        assert isinstance(legs, list)
        assert bool(legs) is not bool(verdict.allowed)
        seen.update(legs)

    assert seen <= allowed_names
    assert ADX_LEGS <= seen
