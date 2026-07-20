"""Standalone tests for the (not-yet-integrated) MACD histogram momentum filter."""
import numpy as np
import pandas as pd

from engine.macd_histogram import (
    BEAR_ACCEL, BEAR_FADE, BULL_ACCEL, BULL_FADE,
    CAUTION, CONFIRM, VETO,
    classify_state, compute_macd, evaluate,
)


def test_classify_state_matches_chrismoody_four_colors():
    assert classify_state(0.5, 0.3) == BULL_ACCEL   # >0 rising  (aqua)
    assert classify_state(0.5, 0.7) == BULL_FADE     # >0 falling (blue)
    assert classify_state(-0.5, -0.3) == BEAR_ACCEL  # <=0 falling (red)
    assert classify_state(-0.5, -0.7) == BEAR_FADE   # <=0 rising  (maroon)


def test_compute_macd_shapes_and_signal_ma_modes():
    close = pd.Series(np.linspace(100, 120, 80))
    ema_df = compute_macd(close, signal_ma="ema")
    sma_df = compute_macd(close, signal_ma="sma")
    assert list(ema_df.columns) == ["macd", "signal", "hist"]
    # hist == macd - signal by construction
    assert abs((ema_df["macd"] - ema_df["signal"] - ema_df["hist"]).dropna().abs().max()) < 1e-9
    # EMA vs SMA signal lines differ
    assert not np.allclose(ema_df["signal"].dropna().tail(10), sma_df["signal"].dropna().tail(10))


def _accelerating_up():
    return pd.Series(100 + np.cumsum(np.linspace(0.5, 4.0, 80)))


def _accelerating_down():
    return pd.Series(300 - np.cumsum(np.linspace(0.5, 4.0, 80)))


def test_uptrend_confirms_buy_and_vetoes_sell():
    close = _accelerating_up()
    buy = evaluate(close, "BUY")
    sell = evaluate(close, "SELL")
    assert buy.state == BULL_ACCEL and buy.verdict == CONFIRM
    assert sell.verdict == VETO          # accelerating up momentum kills a SHORT


def test_downtrend_confirms_sell_and_vetoes_buy():
    close = _accelerating_down()
    sell = evaluate(close, "SELL")
    buy = evaluate(close, "BUY")
    assert sell.state == BEAR_ACCEL and sell.verdict == CONFIRM
    assert buy.verdict == VETO


def test_short_into_fading_down_momentum_is_not_confirmed():
    # A decelerating downtrend: MACD-hist (momentum-of-momentum) turns up first, so
    # a fresh SELL here must NOT be CONFIRMED — the filter refuses to short into
    # reversing/fading momentum (the exhaustion entries that got stopped today).
    close = pd.Series(300 - np.cumsum(np.linspace(4.0, 0.2, 60)))
    res = evaluate(close, "SELL")
    assert res.verdict in (CAUTION, VETO)
    assert res.verdict != CONFIRM


def test_chop_series_flags_caution():
    close = pd.Series(100 + 5 * np.sin(np.arange(80) * 2.0))
    res = evaluate(close, "BUY")
    assert res.is_chop is True
    assert res.verdict == CAUTION
