import numpy as np
import pandas as pd

from intelligence.regime_detector import RegimeDetector
from scanner import Scanner


def _candles(count: int) -> pd.DataFrame:
    close = np.linspace(100.0, 110.0, count)
    return pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(count, 1000.0),
        }
    )


def test_regime_under_30_candles_is_warming_and_not_gradeable():
    result = RegimeDetector().detect(_candles(29), "NIFTY", "5min")

    assert result["regime"] == "UNKNOWN"
    assert result["readiness"] == "WARMING"
    assert result["ready_for_grading"] is False
    assert result["candle_count"] == 29
    assert Scanner._intel_result_cacheable({"regime": result}) is False


def test_30_to_59_candles_is_partial_and_confidence_is_capped():
    result = RegimeDetector().detect(_candles(30), "NIFTY", "5min")

    assert result["regime"] != "UNKNOWN"
    assert result["readiness"] == "PARTIAL"
    assert result["ready_for_grading"] is True
    assert result["full_confidence"] is False
    assert result["confidence"] <= 60
    assert Scanner._intel_result_cacheable({"regime": result}) is True


def test_60_candles_enables_full_regime_confidence():
    result = RegimeDetector().detect(_candles(60), "NIFTY", "5min")

    assert result["regime"] != "UNKNOWN"
    assert result["readiness"] == "FULL"
    assert result["ready_for_grading"] is True
    assert result["full_confidence"] is True
    assert result["candle_count"] == 60


def test_unknown_result_is_never_cacheable_even_with_enough_candles():
    result = {
        "regime": {
            "regime": "UNKNOWN",
            "ready_for_grading": False,
            "candle_count": 60,
        }
    }

    assert Scanner._intel_result_cacheable(result) is False
