import inspect

import numpy as np
import pandas as pd

from config.settings import get_settings
from engine.ut_bot_core import UTBotEngine
from intelligence.intelligence_aggregator import IntelligenceAggregator
from intelligence.regime_detector import RegimeDetector
from scanner import Scanner


def _trend_frame(direction: str = "UP", count: int = 60) -> pd.DataFrame:
    step = 0.04 if direction == "UP" else -0.04
    close = 100.0 + np.arange(count) * step
    return pd.DataFrame(
        {
            "open": close - (0.01 if direction == "UP" else -0.01),
            "high": close + 0.03,
            "low": close - 0.03,
            "close": close,
            "volume": np.full(count, 1000.0),
        },
        index=pd.date_range("2026-07-03 09:15", periods=count, freq="5min"),
    )


def _patch_low_adx(monkeypatch, value: float = 12.0) -> None:
    def fake_adx(df: pd.DataFrame, period: int = 14):
        series = pd.Series(float(value), index=df.index)
        return series, series, series

    def fake_atr(df: pd.DataFrame, period: int = 14):
        return pd.Series(0.08, index=df.index)

    monkeypatch.setattr(UTBotEngine, "calculate_adx", staticmethod(fake_adx))
    monkeypatch.setattr(UTBotEngine, "calculate_atr", staticmethod(fake_atr))


def _er_settings(monkeypatch, enabled: bool = True):
    settings = get_settings()
    monkeypatch.setattr(settings, "regime_early_trend_er_promotion_enabled", enabled)
    monkeypatch.setattr(settings, "regime_early_trend_er_period", 10)
    monkeypatch.setattr(settings, "regime_early_trend_er_threshold", 0.60)
    monkeypatch.setattr(settings, "regime_early_trend_er_adx_ceiling", 20.0)
    monkeypatch.setattr(settings, "regime_early_trend_er_range_day_min_confidence", 0.60)
    monkeypatch.setattr(settings, "regime_smoothing_enabled", True)
    return settings


def test_kaufman_er_promotes_low_adx_clean_opening_drive(monkeypatch):
    _patch_low_adx(monkeypatch, value=12.0)
    _er_settings(monkeypatch, enabled=True)

    result = RegimeDetector().detect(
        _trend_frame("UP"),
        "NIFTY",
        "5min",
        session_context={"day_type": "UNDECIDED", "confidence": 0.30},
    )

    assert result["adx"] == 12.0
    assert result["kaufman_er"] >= 0.99
    assert result["regime"] == "TRENDING_UP"
    assert result["er_promotion"] is True
    assert "Kaufman ER promotion" in result["er_promotion_reason"]


def test_kaufman_er_promotion_supports_bearish_opening_drive(monkeypatch):
    _patch_low_adx(monkeypatch, value=11.0)
    _er_settings(monkeypatch, enabled=True)

    result = RegimeDetector().detect(
        _trend_frame("DOWN"),
        "BANKNIFTY",
        "5min",
        session_context={"day_type": "UNDECIDED", "confidence": 0.35},
    )

    assert result["regime"] == "TRENDING_DOWN"
    assert result["er_promotion"] is True
    assert "SMA stack DOWN" in result["er_promotion_reason"]


def test_confirmed_range_day_blocks_er_phantom_trend(monkeypatch):
    _patch_low_adx(monkeypatch, value=12.0)
    _er_settings(monkeypatch, enabled=True)

    result = RegimeDetector().detect(
        _trend_frame("UP"),
        "SENSEX",
        "5min",
        session_context={"day_type": "RANGE_DAY", "confidence": 0.62},
    )

    assert result["regime"] != "TRENDING_UP"
    assert result["er_promotion"] is False
    assert result["er_promotion_blocked"] == "confirmed_range_day"


def test_er_promotion_toggle_leaves_low_adx_clean_move_ranging(monkeypatch):
    _patch_low_adx(monkeypatch, value=12.0)
    _er_settings(monkeypatch, enabled=False)

    result = RegimeDetector().detect(
        _trend_frame("UP"),
        "MIDCPNIFTY",
        "5min",
        session_context={"day_type": "UNDECIDED", "confidence": 0.20},
    )

    assert result["regime"] in {"RANGING", "VOLATILE"}
    assert result["er_promotion"] is False


def test_intelligence_aggregator_passes_day_type_context_to_regime_detector(monkeypatch):
    captured = {}
    ctx = {"day_type": "RANGE_DAY", "confidence": 0.80}
    frame = _trend_frame("UP")
    aggregator = IntelligenceAggregator()

    def fake_detect(df, instrument="", timeframe="", *, session_context=None):
        captured["session_context"] = session_context
        return {
            "regime": "RANGING",
            "confidence": 60,
            "adx": 12.0,
            "ready_for_grading": True,
            "candle_count": len(df),
        }

    monkeypatch.setattr(aggregator.regime, "detect", fake_detect)

    aggregator.analyze(
        instrument="NIFTY",
        timeframe="5min",
        candle_df=frame,
        candle_1min_df=None,
        options_chain=None,
        spot_price=float(frame["close"].iloc[-1]),
        session_context=ctx,
    )

    assert captured["session_context"] is ctx


def test_er_settings_force_regime_refresh_and_clear_intel_caches():
    source = inspect.getsource(Scanner.configure)

    assert "regime_early_trend_er_promotion_enabled" in source
    assert "regime_early_trend_er_threshold" in source
    assert "advanced_refresh_changed" in source
    assert "_intel_cache" in source
    assert "_timeframe_regimes" in source
