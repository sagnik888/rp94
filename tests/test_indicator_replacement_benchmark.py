from __future__ import annotations

import numpy as np
import pandas as pd

from config.settings import get_settings
from research.indicator_replacement_benchmark import (
    BASELINE_STRATEGY,
    STRATEGIES,
    aroon_positions,
    non_atr_majority_positions,
    run_benchmark,
    trades_from_positions,
)


def _trend_frame(rows: int = 120) -> pd.DataFrame:
    idx = pd.date_range("2026-07-01 09:15", periods=rows, freq="5min")
    base = np.linspace(100.0, 150.0, rows)
    wave = np.sin(np.linspace(0, 6, rows)) * 0.6
    close = base + wave
    open_ = close - 0.2
    high = close + 0.7
    low = close - 0.7
    volume = np.linspace(1000, 1400, rows)
    return pd.DataFrame(
        {"timestamp": idx, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    ).set_index("timestamp")


def _swing_frame(rows: int = 120) -> pd.DataFrame:
    idx = pd.date_range("2026-07-01 09:15", periods=rows, freq="5min")
    close = 100.0 + np.sin(np.linspace(0, 14, rows)) * 4.0
    open_ = close + np.cos(np.linspace(0, 14, rows)) * 0.2
    high = np.maximum(open_, close) + 0.8
    low = np.minimum(open_, close) - 0.8
    volume = np.full(rows, 1000.0)
    return pd.DataFrame(
        {"timestamp": idx, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    ).set_index("timestamp")


def test_aroon_detects_fresh_trend_direction():
    positions = aroon_positions(_trend_frame(), period=25)
    assert positions[-1] == 1
    assert np.count_nonzero(positions) > 0


def test_non_atr_majority_can_generate_research_positions():
    positions = non_atr_majority_positions(_swing_frame())
    assert len(positions) == 120
    assert set(np.unique(positions)).issubset({-1, 0, 1})
    assert np.count_nonzero(np.diff(positions)) > 0


def test_trade_replay_is_intraday_and_point_based():
    df = _trend_frame(80)
    positions = np.zeros(len(df), dtype=int)
    positions[10:40] = 1
    positions[40:70] = -1
    trades = trades_from_positions(
        df,
        positions,
        strategy="unit",
        instrument="NIFTY",
        timeframe="5min",
        friction_bps_per_side=0.0,
    )
    assert len(trades) >= 2
    assert trades[0].direction == "LONG"
    assert trades[0].points > 0


def test_run_benchmark_uses_supplied_csvs_without_changing_settings(tmp_path):
    before = get_settings().scanner_engine
    data_dir = tmp_path / "candles"
    data_dir.mkdir()
    frame = _trend_frame(150).reset_index()
    for instrument in ("NIFTY", "BANKNIFTY"):
        for timeframe in ("5min", "15min"):
            frame.to_csv(data_dir / f"{instrument}_{timeframe}.csv", index=False)
    result = run_benchmark(
        data_dir,
        instruments=("NIFTY", "BANKNIFTY"),
        timeframes=("5min", "15min"),
        strategies={name: STRATEGIES[name] for name in (BASELINE_STRATEGY, "ema_9_21", "aroon_25")},
    )
    assert get_settings().scanner_engine == before
    assert result["baseline"] == BASELINE_STRATEGY
    assert len(result["files"]) == 4
    assert {row["strategy"] for row in result["aggregate"]} == {BASELINE_STRATEGY, "ema_9_21", "aroon_25"}
    assert all("improvement_vs_baseline_points" in row for row in result["aggregate"])

