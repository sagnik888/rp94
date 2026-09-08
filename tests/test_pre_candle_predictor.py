from datetime import datetime, timedelta

import pandas as pd

from engine.pre_candle_predictor import (
    EARLY_REVERSAL_WARNING,
    LIKELY_15M_FLIP,
    LIKELY_5M_FLIP,
    build_pre_candle_prediction,
)


def _df(start: datetime, rows):
    return pd.DataFrame(
        rows,
        index=pd.date_range(start, periods=len(rows), freq="min"),
    )


def _tf_df(start: datetime, minutes: int, prior_count: int, current):
    rows = []
    price = 100.0
    for idx in range(prior_count):
        open_px = price
        close_px = price + (1 if idx % 2 == 0 else -1)
        rows.append(
            {
                "open": open_px,
                "high": max(open_px, close_px) + 4,
                "low": min(open_px, close_px) - 4,
                "close": close_px,
                "volume": 1000,
            }
        )
        price += 0.5
    rows.append(current)
    index = pd.date_range(start, periods=prior_count + 1, freq=f"{minutes}min")
    return pd.DataFrame(rows, index=index)


def _intel(score=0.8):
    return {
        "aggregate": {"score": score},
        "order_flow": {"signal": "BULLISH"},
        "pcr": {"signal": "BULLISH"},
        "oi": {"signal": "BULLISH"},
    }


def test_pre_candle_predictor_flags_likely_5m_flip_from_1m_thrust():
    start = datetime(2026, 7, 1, 9, 15)
    current_start = start + timedelta(minutes=20)
    candle_5m = _tf_df(
        start,
        5,
        4,
        {"open": 100, "high": 125, "low": 99, "close": 124, "volume": 3200},
    )
    candle_1m = _df(
        current_start,
        [
            {"open": 100, "high": 108, "low": 99, "close": 107, "volume": 900},
            {"open": 107, "high": 116, "low": 106, "close": 115, "volume": 1200},
            {"open": 115, "high": 125, "low": 114, "close": 124, "volume": 1500},
        ],
    )
    history_1m = _df(
        current_start - timedelta(minutes=20),
        [{"open": 99, "high": 101, "low": 98, "close": 100, "volume": 300} for _ in range(20)],
    )
    candle_1m = pd.concat([history_1m, candle_1m])

    pred = build_pre_candle_prediction(
        instrument="NIFTY",
        timeframe="5min",
        candle_tf=candle_5m,
        candle_1min=candle_1m,
        mtf_states={"1min": {"position_label": "LONG"}, "5min": {"adx_value": 31}},
        intel_result=_intel(0.82),
        core_states={
            "NIFTY": {"position_label": "LONG"},
            "BANKNIFTY": {"position_label": "LONG"},
            "SENSEX": {"position_label": "SHORT"},
        },
    )

    assert pred["state"] == LIKELY_5M_FLIP
    assert pred["direction"] == "LONG"
    assert pred["confidence"] >= 0.62
    assert pred["read_only"] is True


def test_pre_candle_predictor_uses_partial_5m_inside_15m():
    start = datetime(2026, 7, 1, 9, 15)
    current_start = start + timedelta(minutes=60)
    candle_15m = _tf_df(
        start,
        15,
        4,
        {"open": 200, "high": 238, "low": 198, "close": 236, "volume": 9000},
    )
    candle_5m = _tf_df(
        current_start,
        5,
        1,
        {"open": 210, "high": 238, "low": 209, "close": 236, "volume": 4500},
    )
    history_1m = _df(
        current_start - timedelta(minutes=20),
        [{"open": 200, "high": 202, "low": 199, "close": 201, "volume": 250} for _ in range(20)],
    )
    current_1m = _df(
        current_start,
        [{"open": 210 + i, "high": 216 + i, "low": 209 + i, "close": 215 + i, "volume": 1100} for i in range(8)],
    )

    pred = build_pre_candle_prediction(
        instrument="BANKNIFTY",
        timeframe="15min",
        candle_tf=candle_15m,
        candle_1min=pd.concat([history_1m, current_1m]),
        candle_5min=candle_5m,
        mtf_states={"1min": {"position_label": "LONG"}, "15min": {"adx_value": 29}},
        intel_result=_intel(0.9),
        core_states={
            "NIFTY": {"position_label": "LONG"},
            "BANKNIFTY": {"position_label": "LONG"},
            "SENSEX": {"position_label": "LONG"},
        },
    )

    assert pred["state"] == LIKELY_15M_FLIP
    assert pred["direction"] == "LONG"
    assert "partial 5m agrees" in pred["reason"]


def test_pre_candle_predictor_warns_when_active_trade_faces_opposite_thrust():
    start = datetime(2026, 7, 1, 11, 0)
    candle_5m = _tf_df(
        start,
        5,
        4,
        {"open": 100, "high": 124, "low": 99, "close": 123, "volume": 3500},
    )
    history_1m = _df(
        start,
        [{"open": 99, "high": 101, "low": 98, "close": 100, "volume": 250} for _ in range(20)],
    )
    current_1m = _df(
        candle_5m.index[-1],
        [{"open": 100 + i, "high": 108 + i, "low": 99 + i, "close": 107 + i, "volume": 1200} for i in range(3)],
    )

    pred = build_pre_candle_prediction(
        instrument="SENSEX",
        timeframe="5min",
        candle_tf=candle_5m,
        candle_1min=pd.concat([history_1m, current_1m]),
        mtf_states={"1min": {"position_label": "LONG"}, "5min": {"adx_value": 30}},
        intel_result=_intel(0.75),
        core_states={"NIFTY": {"position_label": "LONG"}, "BANKNIFTY": {"position_label": "LONG"}},
        trade_context={"direction": "SHORT", "pnl": 300, "peak_pnl": 1200},
        option_context={"available": True, "acceleration_points_per_min": -8.0},
    )

    assert pred["state"] == EARLY_REVERSAL_WARNING
    assert pred["direction"] == "LONG"


def test_pre_candle_predictor_uses_tick_price_proxy_when_index_volume_unavailable():
    start = datetime(2026, 7, 1, 12, 0)
    current_start = start + timedelta(minutes=20)
    candle_5m = _tf_df(
        start,
        5,
        4,
        {"open": 100, "high": 125, "low": 99, "close": 124, "volume": 0},
    )
    history_1m = _df(
        current_start - timedelta(minutes=20),
        [
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 0, "tick_count": 2}
            for _ in range(20)
        ],
    )
    current_1m = _df(
        current_start,
        [
            {"open": 100, "high": 109, "low": 99, "close": 108, "volume": 0, "tick_count": 10},
            {"open": 108, "high": 118, "low": 107, "close": 117, "volume": 0, "tick_count": 11},
            {"open": 117, "high": 126, "low": 116, "close": 125, "volume": 0, "tick_count": 12},
        ],
    )

    pred = build_pre_candle_prediction(
        instrument="NIFTY",
        timeframe="5min",
        candle_tf=candle_5m,
        candle_1min=pd.concat([history_1m, current_1m]),
        mtf_states={"1min": {"position_label": "LONG"}, "5min": {"adx_value": 28}},
        intel_result={
            "aggregate": {"score": 0.7},
            "order_flow": {"signal": "BULLISH", "options_flow": {"ce_pe_activity_ratio": 1.3}},
            "pcr": {"signal": "BULLISH"},
            "oi": {"signal": "BULLISH"},
        },
        core_states={
            "NIFTY": {"position_label": "LONG"},
            "BANKNIFTY": {"position_label": "LONG"},
        },
        volume_context={
            "volume_available": False,
            "volume_reliable": False,
            "volume_source": "fyers_ws",
            "volume_key": "",
            "tick_count_current_minute": 12,
        },
    )

    one_min = pred["evidence"]["one_min"]
    assert one_min["volume_unavailable"] is True
    assert one_min["tick_count"] == 12
    assert "live 1m cash volume unavailable" in pred["reason"]
    assert "tick activity 12/min" in pred["reason"]
    assert "1m price-action burst proxy" in pred["reason"]
    assert pred["confidence"] >= 0.50
