"""Offline audit probes; synthetic data only, no broker/DB/server access.

Run from the repository: venv/Scripts/python.exe reports/candle_intel_volume_audit_20260911.py
Assertions pin observed defects, not desired future behavior. No product code is changed.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from loguru import logger
from data.candle_builder import CandleBuilder
from intelligence.intelligence_aggregator import IntelligenceAggregator
from intelligence.oi_tracker import OITracker
from intelligence.order_flow import OrderFlowAnalyzer
from intelligence.pcr_engine import PCREngine
from intelligence.volume_profiler import VolumeProfiler

logger.remove()


def frame(volumes, prices=None, freq="min"):
    n = len(volumes)
    prices = prices if prices is not None else [100.0] * n
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices],
            "close": prices,
            "volume": volumes,
            "volume_basis": ["fyers_index"] * n,
            "tick_count": [20] * n,
        },
        index=pd.date_range("2026-09-10 09:15", periods=n, freq=freq),
    )


def pick(value, *keys):
    return {key: value.get(key) for key in keys}


def run():
    output = {}
    builder = CandleBuilder()
    for timestamp, cumulative in [
        ("09:15:00", 1000), ("09:15:50", 1100), ("09:16:00", 1200),
        ("09:16:50", 1300), ("09:17:00", 1400),
    ]:
        builder.update_from_tick("NIFTY", 25000, cumulative,
                                 datetime.fromisoformat("2026-09-10T" + timestamp), "fyers_index")
    rows = builder.get_candles("NIFTY", "1min")
    output["cumulative_boundary_loss"] = {
        "known_cumulative_increase": 400,
        "stored_volume_sum": float(rows.volume.sum()),
        "stored_minutes": rows.volume.astype(float).tolist(),
    }
    assert output["cumulative_boundary_loss"]["stored_volume_sum"] == 200

    builder = CandleBuilder()
    for timestamp, cumulative, basis in [
        ("09:15:00", 1000000, "fyers_index"),
        ("09:15:10", 1100000, "fyers_index"),
        ("09:15:20", 1000, "kite_futures"),
    ]:
        builder.update_from_tick("NIFTY", 25000, cumulative,
                                 datetime.fromisoformat("2026-09-10T" + timestamp), basis)
    latest = builder.get_candles("NIFTY", "1min").iloc[-1]
    output["latent_source_switch_relabel"] = {
        "stored_volume": float(latest.volume), "stored_basis": latest.volume_basis,
        "internal_volume": float(builder._current_candle["NIFTY"]["volume"]),
        "reachability": "latent: current production keeps futures outside index candle volume",
    }
    assert float(latest.volume) == 100000 and latest.volume_basis == "kite_futures"

    data = frame([100] * 26 + [500, 0, 0, 0], [100.0] * 26 + [110, 109, 108, 107])
    result = VolumeProfiler().analyze(data, "NIFTY")
    output["stale_volume_wrong_direction"] = {
        **pick(result, "available", "current_volume", "volume_ratio", "signal"),
        "measured_volume_bar_close_change": 10.0, "volume_lag_bars": 3,
    }
    assert result["available"] and result["signal"] == "BEARISH_VOLUME"

    data = frame([100] * 29 + [500])
    data.index = pd.DatetimeIndex([pd.Timestamp("2026-09-09 15:00"), *data.index[1:]])
    data.iloc[0, data.columns.get_loc("volume_basis")] = "kite_futures"
    result = VolumeProfiler().analyze(data, "NIFTY")
    recent = VolumeProfiler().analyze(data.iloc[-20:], "NIFTY")
    output["entire_frame_unit_veto"] = {
        **pick(result, "available", "reason", "volume_ratio"),
        "same_latest_twenty_alone": pick(recent, "available", "volume_ratio"),
        "different_basis_bar": "previous session; all current-session bars are cash volume",
        "reachability": "legacy/mixed cache conditional; no current futures-in-index injection confirmed",
    }
    assert not result["available"] and recent["available"]

    data = frame([100] * 5 + [0] * 5)
    data["close"] = 100.9
    result = OrderFlowAnalyzer().analyze(data)
    output["zero_volume_last_five_flow"] = pick(result, "available", "signal", "buy_pct", "source")
    assert result["available"] and result["signal"] == "BULLISH"

    data = frame([100000] * 5 + [10] * 5)
    data.loc[data.index[-5:], "volume_basis"] = "kite_futures"
    data.loc[data.index[:5], "close"] = 99.1
    data.loc[data.index[-5:], "close"] = 100.9
    result = OrderFlowAnalyzer().analyze(data)
    output["mixed_units_accepted_by_flow"] = pick(result, "available", "signal", "ratio", "cum_delta", "source")
    assert result["available"] and result["ratio"] == 0.05

    one_minute = frame([100] * 30)
    five_minute = frame([9000] * 30, freq="5min")
    intel = IntelligenceAggregator()
    intel.regime.detect = lambda *args, **kwargs: {"regime": "RANGING"}
    result = intel.analyze("NIFTY", "5min", five_minute, candle_1min_df=one_minute)
    output["timeframe_and_asset_labels"] = {
        "aggregate_timeframe": result["aggregate"]["timeframe"],
        "volume_current": result["volume"]["current_volume"],
        "five_minute_input_volume": 9000,
        "one_minute_input_volume": 100,
        "aggregate_futures_volume": result["aggregate"]["futures_volume"],
        "volume_has_measured_timestamp": "measured_at" in result["volume"],
        "volume_has_timeframe": "timeframe" in result["volume"],
    }
    assert result["volume"]["current_volume"] == 100
    resampled = CandleBuilder._resample_ohlcv(one_minute, "5min")
    output["resample_loses_activity_telemetry"] = {
        "one_minute_tick_count": "tick_count" in one_minute,
        "five_minute_tick_count": "tick_count" in resampled,
        "five_minute_columns": list(resampled.columns),
    }
    assert "tick_count" not in resampled

    chain = pd.DataFrame({
        "strike": [24950, 25000, 25050], "call_oi": [1000] * 3, "put_oi": [1000] * 3,
        "call_oi_change": [500] * 3, "put_oi_change": [0] * 3,
        "call_volume": [1000] * 3, "put_volume": [1000] * 3,
        "oi_change_source": ["snapshot_delta"] * 3,
    })
    oi = OITracker().analyze(chain, spot_price=25000)
    flow = OrderFlowAnalyzer().analyze(one_minute, chain, 25000)
    output["option_oi_flow_double_interpretation"] = {
        "oi_signal": oi["signal"],
        "ce_pe_activity_ratio": flow["options_flow"]["ce_pe_activity_ratio"],
        "flow_confirmation": flow["multi_source_flow"]["confirmation"],
    }
    assert oi["signal"] == "BEARISH" and flow["multi_source_flow"]["confirmation"] == "BULLISH"
    pcr = PCREngine()
    for ratio in [1, 1, 1, 1.5, 1.5, 1.5]:
        chain["put_oi"] = 1000 * ratio
        result = pcr.analyze(chain, "NIFTY", 25000)
    before = result["trend"]
    for _ in range(3):
        result = pcr.analyze(chain, "NIFTY", 25000)
    output["pcr_repeated_snapshot_trend"] = {
        "after_change": before, "after_three_identical_reads": result["trend"],
    }
    assert before == "RISING" and result["trend"] == "STABLE"
    return output


if __name__ == "__main__":
    results = run()
    target = Path(__file__).with_suffix(".json")
    target.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"probes": len(results), "output": str(target), "all_observations_reproduced": True}))
