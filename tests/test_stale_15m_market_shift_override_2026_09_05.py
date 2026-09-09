"""Strict fast-market evidence may supersede stale 15-minute direction."""
from __future__ import annotations

import pytest

from config.settings import Settings
from engine.quality_verdict import QualityInputs, evaluate_quality
from scanner import Scanner


@pytest.mark.parametrize("regime", ["UNKNOWN", "RANGING", "VOLATILE"])
@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_confirmed_market_shift_is_bidirectional_and_not_vetoed_by_stale_15m(regime, direction):
    verdict = evaluate_quality(
        QualityInputs(
            timeframe="5min", inst_type="OPT", confidence=0.65, regime=regime,
            adx_value=28.0, trend_15m_agrees=False, direction=direction,
            market_shift_confirmed=True,
        ),
        Settings(),
    )
    assert verdict.allowed is True
    assert verdict.reason_code == "CONFIRMED_MARKET_SHIFT"


@pytest.mark.parametrize("regime", ["UNKNOWN", "RANGING"])
def test_same_counter_15m_signal_without_strict_market_shift_stays_blocked(regime):
    verdict = evaluate_quality(
        QualityInputs(
            timeframe="5min", inst_type="OPT", confidence=0.65, regime=regime,
            adx_value=28.0, trend_15m_agrees=False, direction="SHORT",
            market_shift_confirmed=False,
        ),
        Settings(),
    )
    assert verdict.allowed is False


def test_market_shift_never_bypasses_canonical_bplus_floor():
    verdict = evaluate_quality(
        QualityInputs(
            timeframe="1min", inst_type="OPT", confidence=0.54, regime="UNKNOWN",
            trend_15m_agrees=False, direction="SHORT", market_shift_confirmed=True,
        ),
        Settings(),
    )
    assert verdict.allowed is False
    assert verdict.reason_code == "BELOW_BPLUS_ELIGIBILITY"


def test_full_core_five_minute_market_shift_admits_the_observed_banknifty_setup():
    verdict = evaluate_quality(
        QualityInputs(
            timeframe="5min", inst_type="OPT", confidence=0.3645, regime="RANGING",
            adx_value=18.01, trend_15m_agrees=False, direction="LONG",
            market_shift_confirmed=True, market_shift_core_votes=3,
            market_shift_total_votes=4,
        ),
        Settings(),
    )
    assert verdict.allowed is True
    assert verdict.reason_code == "FULL_CORE_MARKET_SHIFT"


def test_partial_core_market_shift_cannot_bypass_canonical_bplus_floor():
    verdict = evaluate_quality(
        QualityInputs(
            timeframe="5min", inst_type="OPT", confidence=0.50, regime="RANGING",
            adx_value=30.0, direction="LONG", market_shift_confirmed=True,
            market_shift_core_votes=2, market_shift_total_votes=3,
        ),
        Settings(),
    )
    assert verdict.allowed is False
    assert verdict.reason_code == "BELOW_BPLUS_ELIGIBILITY"


def test_scanner_passes_only_verified_full_core_votes_to_quality_policy():
    candidate = type("Candidate", (), {
        "instrument": "BANKNIFTY", "timeframe": "5min", "inst_type": "OPT",
        "direction": "LONG", "confidence": 0.3645, "grade": "C",
        "adx_value": 18.01, "trend_15m_agrees": False,
        "impulse_confirmed": False, "is_explosive_bypass": False,
        "source_engines": [], "volume_ratio": 0.56, "volume_spike_entry": False,
        "power_move": False, "market_wide_reversal_entry": True,
        "market_wide_reversal_context": {
            "enabled": True, "core_votes": 3, "total_votes": 4,
        },
    })()
    scanner = Scanner.__new__(Scanner)
    scanner._candidate_15m_trend_agrees = lambda value: value.trend_15m_agrees
    scanner._get_trade_regime = lambda *args, **kwargs: "RANGING"

    verdict = scanner._quality_verdict_for_candidate(candidate, regime="RANGING")

    assert verdict.allowed is True
    assert verdict.reason_code == "FULL_CORE_MARKET_SHIFT"
    assert verdict.checks["market_shift_core_votes"] == 3
    assert verdict.checks["market_shift_total_votes"] == 4


def test_full_sensitivity_market_shock_window_covers_requested_afternoon():
    fields = Settings.model_fields
    assert fields["market_ltp_shock_session_start"].default == "13:00"
    assert fields["market_ltp_shock_session_end"].default == "15:15"


def test_unvalidated_context_flag_does_not_become_quality_evidence():
    candidate = type("Candidate", (), {
        "instrument": "NIFTY", "timeframe": "5min", "inst_type": "OPT",
        "direction": "SHORT", "confidence": 0.65, "grade": "B+",
        "adx_value": 28.0, "trend_15m_agrees": False,
        "impulse_confirmed": False, "is_explosive_bypass": False,
        "source_engines": [], "volume_ratio": 0.0, "volume_spike_entry": False,
        "power_move": False, "market_wide_reversal_entry": True,
        "market_wide_reversal_context": {"enabled": False},
    })()
    scanner = Scanner.__new__(Scanner)
    scanner._candidate_15m_trend_agrees = lambda value: value.trend_15m_agrees
    scanner._get_trade_regime = lambda *args, **kwargs: "UNKNOWN"
    verdict = scanner._quality_verdict_for_candidate(candidate, regime="UNKNOWN")
    assert verdict.allowed is False
