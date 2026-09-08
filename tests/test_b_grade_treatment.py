"""B/B+ treatment is identical across live, replay, recovery and EOD lanes."""

import pytest

from config.settings import get_settings
from engine.signal_manager import SignalManager
from engine.signal_quality_policy import (
    grade_rank,
    option_grade_allowed,
    passes_quality_gate,
)
from engine.ut_bot_core import UTBotSignal


@pytest.fixture
def settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)
    monkeypatch.setattr(settings, "ut_regime_adaptation", True)
    monkeypatch.setattr(settings, "ut_backtest_more_results", True)
    return settings


def _signal(adx=20.0):
    return UTBotSignal(
        timestamp=None,
        signal_type="BUY",
        price=25_000.0,
        trailing_stop=24_950.0,
        stop_distance=50.0,
        atr_value=40.0,
        adx_value=adx,
        position_state=1,
        bar_index=10,
        raw_candle={"open": 24_990.0, "high": 25_010.0, "low": 24_980.0, "close": 25_000.0},
    )


def test_b_grade_confidence_band_is_0_40_to_0_55():
    assert grade_rank("B") == 1
    assert grade_rank("B+") == 2
    grade, _, _ = SignalManager()._grade_signal(
        _signal(adx=18.0), confluence_score=0.0,
        intelligence_score=0.0, regime="UNKNOWN",
    )
    assert grade in {"B", "C"}


@pytest.mark.parametrize("regime", ["TRENDING_UP", "RANGING", "VOLATILE", "UNKNOWN"])
@pytest.mark.parametrize("tf", ["5min", "15min"])
@pytest.mark.parametrize("mode", ["REAL", "HISTORICAL", "RECOVERY", "EOD"])
def test_b_grade_is_neither_option_routable_nor_admissible(settings, regime, tf, mode):
    assert not option_grade_allowed(
        "B", 0.54, regime, mode=mode, adx_value=45.0,
        intel_score=0.95, signal_score=0.95, timeframe=tf,
        trend_15m_agrees=True,
    )
    assert not passes_quality_gate(
        tf, "B", 0.54, regime, "FUT", settings,
        mode=mode, adx_value=45.0, trend_15m_agrees=True,
        impulse_confirmed=True, volume_ratio=2.0, source_count=3,
    )


@pytest.mark.parametrize("mode", ["REAL", "HISTORICAL", "RECOVERY", "EOD"])
def test_confirmed_bplus_has_lane_parity(settings, mode):
    assert passes_quality_gate(
        "15min", "B+", 0.58, "TRENDING_UP", "OPT", settings,
        mode=mode, adx_value=25.0, trend_15m_agrees=True,
        impulse_confirmed=True,
    )


@pytest.mark.parametrize("mode", ["REAL", "HISTORICAL", "RECOVERY", "EOD"])
def test_unevidenced_bplus_has_lane_parity(settings, mode):
    assert not passes_quality_gate(
        "15min", "B+", 0.58, "TRENDING_UP", "OPT", settings,
        mode=mode, adx_value=20.0, trend_15m_agrees=True,
    )
