import pandas as pd

from intelligence.volume_profiler import VolumeProfiler


def _frame(index, volumes):
    closes = [100.0 + (i * 0.1) for i in range(len(index))]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": volumes,
        },
        index=index,
    )


def test_display_volume_ratio_uses_rolling_ratio_when_tod_baseline_missing():
    idx = pd.date_range("2026-07-03 09:15", periods=35, freq="5min")
    df = _frame(idx, [1000] * 34 + [500])

    result = VolumeProfiler().analyze(df, "NIFTY")

    assert result["volume_confidence"] == 50
    assert result["volume_ratio_basis"] == "ROLLING_AVG"
    assert result["relative_volume_ratio"] == 1.0
    assert result["display_volume_ratio"] == result["volume_ratio"]
    assert result["display_volume_ratio"] < 1.0
    assert "market_structure_profile" in result
    assert "aroon" in result
    assert "wavetrend" in result


def test_display_volume_ratio_uses_time_of_day_ratio_when_confident():
    idx = (
        list(pd.date_range("2026-07-01 09:15", periods=35, freq="5min"))
        + list(pd.date_range("2026-07-02 09:15", periods=35, freq="5min"))
        + list(pd.date_range("2026-07-03 09:15", periods=35, freq="5min"))
    )
    df = _frame(pd.DatetimeIndex(idx), [1000] * 104 + [2500])

    result = VolumeProfiler().analyze(df, "NIFTY")

    assert result["volume_confidence"] == 75
    assert result["volume_ratio_basis"] == "TIME_OF_DAY"
    assert result["relative_volume_ratio"] == 2.5
    assert result["display_volume_ratio"] == 2.5
    assert result["effective_volume_ratio"] >= result["display_volume_ratio"]


def test_volume_profiler_exports_market_structure_and_oscillator_context():
    idx = pd.date_range("2026-07-03 09:15", periods=70, freq="5min")
    df = _frame(idx, [1000] * 69 + [4000])
    df.loc[df.index[-1], "close"] = 110.0
    df.loc[df.index[-1], "high"] = 111.0
    df.loc[df.index[-1], "low"] = 108.0

    result = VolumeProfiler().analyze(df, "NIFTY")

    assert result["market_structure_profile"]["poc"] > 0
    assert result["aroon"]["available"] is True
    assert result["wavetrend"]["available"] is True
