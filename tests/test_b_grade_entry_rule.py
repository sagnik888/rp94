"""Canonical B+/evidence admission regressions.

The retired dedicated B-band exception admitted 50--54% signals in trends. Quality
policy v4 has one rule in every lane: B+ (55%) is eligibility, not unconditional
admission; measured ADX plus contemporaneous trend/impulse/volume/source evidence is
still required. Tracker outcomes never lower this floor.
"""

from types import SimpleNamespace
import time

import pytest

from config.settings import get_settings
from engine.signal_quality_policy import option_grade_allowed, passes_quality_gate
from scanner import Scanner


@pytest.fixture
def settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)
    monkeypatch.setattr(settings, "ut_regime_adaptation", True)
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")
    monkeypatch.setattr(settings, "signal_grade_preference", "auto")
    return settings


@pytest.mark.parametrize("tf", ["5min", "15min"])
@pytest.mark.parametrize("inst", ["FUT", "OPT"])
@pytest.mark.parametrize("mode", ["REAL", "HISTORICAL", "RECOVERY", "EOD"])
def test_b_band_never_crosses_the_canonical_floor(settings, tf, inst, mode):
    assert not passes_quality_gate(
        tf,
        "B",
        0.54,
        "TRENDING_UP",
        inst,
        settings,
        mode=mode,
        adx_value=40.0,
        trend_15m_agrees=True,
        impulse_confirmed=True,
        volume_ratio=2.0,
        source_count=3,
        power_move=True,
    )


@pytest.mark.parametrize("tf", ["5min", "15min"])
@pytest.mark.parametrize("inst", ["FUT", "OPT"])
def test_bplus_trend_setup_passes_with_adx_and_two_confirmations(settings, tf, inst):
    assert passes_quality_gate(
        tf,
        "B+",
        0.58,
        "TRENDING_UP",
        inst,
        settings,
        mode="REAL",
        adx_value=22.0,
        trend_15m_agrees=True,
        impulse_confirmed=True,
    )


def test_bplus_is_eligibility_not_an_unconditional_bypass(settings):
    assert not passes_quality_gate(
        "15min", "B+", 0.58, "TRENDING_UP", "FUT", settings,
        mode="REAL", adx_value=22.0, trend_15m_agrees=True,
    )
    assert not passes_quality_gate(
        "15min", "B+", 0.58, "TRENDING_UP", "FUT", settings,
        mode="REAL", adx_value=21.99, trend_15m_agrees=True,
        impulse_confirmed=True,
    )


@pytest.mark.parametrize("regime", ["RANGING", "VOLATILE"])
def test_risky_bplus_requires_trend_and_an_independent_leg(settings, regime):
    common = dict(
        tf="15min", grade="B+", confidence=0.58, regime=regime,
        inst_type="OPT", settings=settings, mode="REAL", adx_value=25.0,
        trend_15m_agrees=True,
    )
    assert not passes_quality_gate(**common)
    assert passes_quality_gate(**common, volume_ratio=1.20)


def test_unknown_needs_a_adx_trend_and_three_confirmation_legs(settings):
    common = dict(
        tf="15min", grade="A", confidence=0.72, regime="UNKNOWN",
        inst_type="FUT", settings=settings, mode="REAL", adx_value=25.0,
        trend_15m_agrees=True,
    )
    assert not passes_quality_gate(**common)
    assert passes_quality_gate(**common, impulse_confirmed=True, volume_ratio=1.20)


def test_option_routing_helper_cannot_resurrect_b_band(settings):
    assert not option_grade_allowed(
        "B", 0.54, "TRENDING_UP", mode="REAL", adx_value=40.0,
        trend_15m_agrees=True,
    )
    assert option_grade_allowed(
        "B+", 0.58, "TRENDING_UP", mode="REAL", adx_value=28.0,
        timeframe="15min", trend_15m_agrees=True,
    )


_INST_METHODS = [
    "_passes_live_trade_ready_gate",
    "_passes_candidate_grade_preference",
    "_passes_timeframe_entry_policy",
    "_market_location_entry_gate",
    "_candidate_15m_trend_agrees",
    "_gate_block",
    "_late_entry_scalp_active",
    "_set_scalp_lock",
    "_minutes_to_force_exit",
    "_get_trade_regime",
    "_trade_regime_is_own_timeframe",
]
_STATIC_METHODS = [
    "_normalize_signal_grade_preference",
    "_setting_enabled",
    "_is_quad_prime_raw_candidate",
    "_timeframe_intel_key",
]


def _stub(regime, tf_regimes=None):
    scanner = SimpleNamespace(
        mode="REAL", latest_regimes={"NIFTY": regime},
        mtf=SimpleNamespace(engines={
            "NIFTY_15min": SimpleNamespace(
                get_state=lambda _key: SimpleNamespace(position=1)
            ),
        }),
        _timeframe_regimes=dict(tf_regimes or {}),
        _intel_cache_time={key: time.time() for key in {'NIFTY_5min', *(tf_regimes or {})}},
    )
    for name in _INST_METHODS:
        setattr(scanner, name, getattr(Scanner, name).__get__(scanner, Scanner))
    for name in _STATIC_METHODS:
        setattr(scanner, name, getattr(Scanner, name))
    scanner.log_signal_decision_once = lambda *args, **kwargs: None
    return scanner


def _candidate(*, confidence=0.58, grade="B+", timeframe="15min", impulse=True):
    return SimpleNamespace(
        grade=grade, confidence=confidence, timeframe=timeframe, inst_type="FUT",
        instrument="NIFTY", direction="LONG", adx_value=28.0,
        impulse_confirmed=impulse, is_explosive_bypass=False,
        trend_15m_agrees=True, reasons=[],
    )


def test_live_gate_uses_the_same_bplus_rule(settings):
    scanner = _stub("TRENDING_UP", {"NIFTY_15min": "TRENDING_UP"})
    assert scanner._passes_live_trade_ready_gate(_candidate()) is True
    rejected = _candidate(confidence=0.54, grade="B")
    assert scanner._passes_live_trade_ready_gate(rejected) is False
    assert rejected.quality_verdict["reason_code"] == "BELOW_BPLUS_ELIGIBILITY"


def test_15min_candidate_cannot_borrow_the_5min_trend_for_admission(settings):
    scanner = _stub("TRENDING_UP")
    candidate = _candidate(timeframe="15min")
    assert scanner._passes_live_trade_ready_gate(candidate) is False
    assert candidate.quality_verdict["reason_code"] == "UNKNOWN_REGIME_SETUP_INSUFFICIENT"


def test_explicit_operator_a_preference_can_only_make_the_book_stricter(settings):
    settings.signal_grade_preference = "A"
    allowed, reason = _stub("TRENDING_UP")._passes_candidate_grade_preference(
        _candidate()
    )
    assert allowed is False
    assert "explicit operator grade preference" in reason
