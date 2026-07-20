import inspect

import pandas as pd

from intelligence.intelligence_aggregator import IntelligenceAggregator
from intelligence.outcome_calibration import OutcomeCalibrationStore
from intelligence.session_context import SessionContextAnalyzer
from engine.signal_processor import SignalProcessor
from scanner import Scanner


def _ohlcv(index, opens, highs, lows, closes, volume=1000):
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [volume for _ in closes],
        },
        index=index,
    )


def test_session_context_identifies_trend_day_up():
    prior_idx = pd.date_range("2026-07-01 14:00", periods=12, freq="5min")
    current_idx = pd.date_range("2026-07-02 09:15", periods=14, freq="5min")
    prior = _ohlcv(
        prior_idx,
        [100 + i * 0.1 for i in range(12)],
        [101 + i * 0.1 for i in range(12)],
        [99 + i * 0.1 for i in range(12)],
        [100.5 + i * 0.1 for i in range(12)],
    )
    closes = [106.0, 106.5, 107.0, 107.4, 107.8, 108.0, 108.6, 109.0, 109.4, 109.8, 110.2, 110.6, 111.0, 111.4]
    current = _ohlcv(
        current_idx,
        [105.8] + closes[:-1],
        [value + 0.35 for value in closes],
        [value - 0.25 for value in closes],
        closes,
    )

    result = SessionContextAnalyzer().analyze(
        pd.concat([prior, current]),
        instrument="NIFTY",
        spot_price=111.4,
        previous_close=101.6,
        vix_value=12.6,
        vix_open=13.0,
    )

    assert result["day_type"] == "TREND_DAY_UP"
    assert result["bias"] == "BULLISH"
    assert result["opening_range_status"] == "BREAK_UP"
    assert result["close_above_vwap_pct"] >= 68
    assert result["vix_change_pct"] < 0


def test_session_context_identifies_range_day_from_vwap_crosses():
    prior_idx = pd.date_range("2026-07-01 14:00", periods=12, freq="5min")
    current_idx = pd.date_range("2026-07-02 09:15", periods=14, freq="5min")
    prior = _ohlcv(
        prior_idx,
        [100 for _ in range(12)],
        [101 for _ in range(12)],
        [99 for _ in range(12)],
        [100 for _ in range(12)],
    )
    closes = [100.2, 99.8, 100.3, 99.7, 100.4, 99.9, 100.1, 99.8, 100.2, 99.9, 100.1, 99.8, 100.0, 99.9]
    current = _ohlcv(
        current_idx,
        closes,
        [value + 0.4 for value in closes],
        [value - 0.4 for value in closes],
        closes,
    )

    result = SessionContextAnalyzer().analyze(
        pd.concat([prior, current]),
        instrument="BANKNIFTY",
        spot_price=99.9,
        previous_close=100.0,
    )

    assert result["day_type"] == "RANGE_DAY"
    assert result["bias"] == "NEUTRAL"
    assert result["vwap_cross_count"] >= 3


def test_top_down_context_is_exposed_and_adjusts_intelligence_score():
    aggregator = IntelligenceAggregator()
    aggregator.volume = type(
        "_Volume",
        (),
        {"analyze": lambda *_args, **_kwargs: {"signal": "NEUTRAL", "buy_sell_ratio": 1.0, "current_volume": 1000}},
    )()
    aggregator.regime = type(
        "_Regime",
        (),
        {"detect": lambda *_args, **_kwargs: {"regime": "TRENDING_UP", "confidence": 70, "ready_for_grading": True}},
    )()
    aggregator.order_flow = type(
        "_Flow",
        (),
        {"analyze": lambda *_args, **_kwargs: {"signal": "NEUTRAL", "ratio": 1.0}},
    )()

    candles = _ohlcv(
        pd.date_range("2026-07-02 09:15", periods=60, freq="5min"),
        [100 + i * 0.1 for i in range(60)],
        [101 + i * 0.1 for i in range(60)],
        [99 + i * 0.1 for i in range(60)],
        [100.5 + i * 0.1 for i in range(60)],
    )
    result = aggregator.analyze(
        instrument="NIFTY",
        timeframe="5min",
        candle_df=candles,
        options_chain=None,
        spot_price=106.4,
        session_context={
            "day_type": "TREND_DAY_UP",
            "bias": "BULLISH",
            "confidence": 0.8,
            "session_delta": {"bias": "BULLISH", "delta_ratio": 0.3},
            "vix_change_pct": -3.0,
        },
        anchor_regime={"regime": "TRENDING_UP", "confidence": 75, "ready_for_grading": True},
        breadth_context={"bias": "BULLISH", "aligned_count": 3},
    )

    aggregate = result["aggregate"]
    assert result["top_down_context"]["has_context"] is True
    assert aggregate["top_down_adjustment"] > 0
    assert aggregate["day_type"] == "TREND_DAY_UP"
    assert aggregate["hourly_anchor"] == "TRENDING_UP"
    assert aggregate["breadth_bias"] == "BULLISH"
    assert aggregate["score"] > 0


def test_live_intel_cache_paths_do_not_hardcode_zero_change_or_weekly_dte():
    morning = inspect.getsource(Scanner._morning_market_refresh)
    worker = inspect.getsource(Scanner._background_intel_worker)

    assert "price_change_pct=0" not in morning
    assert "price_change_pct=0" not in worker
    assert "days_to_expiry=7" not in morning
    assert "days_to_expiry=7" not in worker
    assert "_live_analysis_window" in worker
    assert "_days_to_expiry_for" in worker
    assert "_build_intelligence_contexts" in worker


def test_signal_processor_has_session_trend_day_scalp_guard():
    source = inspect.getsource(SignalProcessor.process_best_signal)

    assert "session_context_trend_day_guard_enabled" in source
    assert "REJECTED - Session Trend Guard" in source
    assert "counter-trend scalp" in source
    assert "fast_thrust_override" in source
    assert "_reversal_relief_context" in source


def test_outcome_calibration_rows_include_context_features(tmp_path):
    trade = type(
        "_Trade",
        (),
        {
            "id": "T1",
            "instrument": "NIFTY",
            "timeframe": "5min",
            "inst_type": "OPT",
            "direction": "LONG",
            "grade": "A",
            "confidence": 0.82,
            "pnl": 125.0,
            "peak_pnl": 200.0,
            "max_drawdown": -30.0,
            "exit_reason": "TARGET",
        },
    )()
    intel = {
        "aggregate": {"score": 18.5, "top_down_adjustment": 0.21},
        "volume": {"signal": "BULLISH_VOLUME", "volume_ratio": 1.8},
        "pcr": {"pcr_oi": 0.92},
        "oi": {"signal": "BULLISH"},
        "order_flow": {"ratio": 1.4},
        "greeks": {"iv_percentile": 64.0},
        "regime": {"regime": "TRENDING_UP"},
        "session_context": {
            "day_type": "TREND_DAY_UP",
            "bias": "BULLISH",
            "vix_change_pct": -2.5,
            "session_delta": {"delta_ratio": 0.25},
        },
        "anchor_regime": {"regime": "TRENDING_UP"},
        "breadth_context": {"bias": "BULLISH"},
    }

    row = OutcomeCalibrationStore(str(tmp_path / "calibration.csv")).build_row(trade, intel)

    assert row["aggregate_score"] == 18.5
    assert row["volume_signal"] == "BULLISH_VOLUME"
    assert row["day_type"] == "TREND_DAY_UP"
    assert row["hourly_anchor"] == "TRENDING_UP"
    assert row["breadth_bias"] == "BULLISH"
    assert row["session_delta_ratio"] == 0.25
