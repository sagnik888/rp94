"""Unit tests for the structural price-action patch (intelligence/price_action.py)."""
import numpy as np
import pandas as pd

from intelligence.price_action import (
    support_resistance, swing_structure, candle_pattern,
    volume_profile, premium_discount, liquidity_pools, PriceActionAnalyzer,
    aroon_context, market_structure_profile, wavetrend_context,
)


def _osc_trend(direction, n=44, drift=0.6, amp=6.0, period=10.5):
    """Oscillating series with a drift — produces clean fractal swing pivots."""
    t = np.arange(n)
    mid = 100.0 + (drift if direction == "up" else -drift) * t
    osc = amp * np.sin(t * (2 * np.pi / period))
    highs = mid + osc + 1.0
    lows = mid + osc - 1.0
    return highs, lows


def _df_from(highs, lows, vol=1000.0):
    idx = pd.date_range("2026-06-23 09:15", periods=len(highs), freq="5min")
    mid = (highs + lows) / 2.0
    return pd.DataFrame({"open": mid, "high": highs, "low": lows,
                         "close": mid, "volume": np.full(len(highs), vol)}, index=idx)


def test_swing_structure_uptrend_and_downtrend():
    uh, ul = _osc_trend("up")
    assert swing_structure(uh, ul) == "UP"
    dh, dl = _osc_trend("down")
    assert swing_structure(dh, dl) == "DOWN"


def test_support_resistance_finds_oscillation_levels():
    # An oscillation around 100 with no drift -> repeated highs/lows form levels.
    h, l = _osc_trend("up", drift=0.0)
    levels = support_resistance(h, l, tol_pct=0.02)
    assert len(levels) >= 1
    # strongest level should sit within the oscillation band
    top = levels[0][0]
    assert 90.0 <= top <= 110.0


def test_candle_patterns():
    # Bullish engulfing: prior red, current green engulfs it
    assert candle_pattern(o=100, h=106, l=99, c=105, po=104, ph=104.5, pl=100, pc=101)[1] == "BULL"
    # Shooting star: long upper wick, tiny body, ~no lower wick
    assert candle_pattern(o=100, h=112, l=100, c=100.5, po=99, ph=101, pl=98, pc=100)[0] == "SHOOTING_STAR"
    # Doji: tiny body vs range
    assert candle_pattern(o=100, h=105, l=95, c=100.2, po=99, ph=100, pl=98, pc=99.5)[0] == "DOJI"


def test_volume_profile_poc_in_range():
    closes = np.array([100, 101, 100, 102, 100, 101, 100, 103, 100, 101.0])
    vols = np.array([10, 5, 30, 4, 40, 6, 35, 3, 38, 7.0])  # most volume near 100
    vp = volume_profile(closes, vols, bins=10)
    assert vp is not None
    assert 99.0 <= vp["poc"] <= 101.5
    assert vp["val"] <= vp["poc"] <= vp["vah"]


def test_market_structure_profile_detects_upside_acceptance():
    idx = pd.date_range("2026-07-03 09:15", periods=220, freq="5min")
    base = np.full(220, 100.0)
    high = base + 1.0
    low = base - 1.0
    close = base.copy()
    volume = np.full(220, 1000.0)
    high[-1] = 108.0
    low[-1] = 105.0
    close[-1] = 107.0
    volume[-1] = 3000.0
    df = pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )

    profile = market_structure_profile(df, lookback=220, bins=32)

    assert profile is not None
    assert profile["poc"] < profile["close"]
    assert profile["signal"] in {"UPSIDE_ACCEPTANCE", "ACCEPTING_HIGHER"}
    assert profile["state"] == "ABOVE_VALUE"


def test_analyze_long_into_downtrend_is_adverse():
    dh, dl = _osc_trend("down")
    ctx = PriceActionAnalyzer().analyze(_df_from(dh, dl), "BUY")
    assert ctx is not None
    assert ctx["structure"] == "DOWN"
    assert ctx["score"] < 0  # buying into a downtrend scores negative


def test_analyze_short_beats_buy_in_downtrend():
    dh, dl = _osc_trend("down")
    df = _df_from(dh, dl)
    sell = PriceActionAnalyzer().analyze(df, "SELL")
    buy = PriceActionAnalyzer().analyze(df, "BUY")
    assert sell["score"] > buy["score"]  # shorting a downtrend is more favorable than longing it


def test_analyze_returns_none_on_short_df():
    h = np.array([101.0] * 5)
    assert PriceActionAnalyzer().analyze(_df_from(h, h - 2), "BUY") is None


def test_premium_discount_zones():
    highs = np.full(30, 110.0)
    lows = np.full(30, 90.0)
    assert premium_discount(highs, lows, 109.0, lookback=30)[0] == "PREMIUM"
    assert premium_discount(highs, lows, 91.0, lookback=30)[0] == "DISCOUNT"
    assert premium_discount(highs, lows, 100.0, lookback=30)[0] == "EQUILIBRIUM"


def test_liquidity_pools_detects_equal_highs():
    # two identical triangular peaks (=105) confirmable by a 5-bar fractal
    highs = np.full(50, 100.0)
    lows = np.full(50, 98.0)
    for center in (12, 32):
        for k in range(-4, 5):
            highs[center + k] = 100.0 + (5 - abs(k))
    eqh, eql = liquidity_pools(highs, lows, close=101.0, strength=5, tol_pct=0.002)
    assert eqh is not None and abs(eqh - 105.0) < 0.5   # equal-highs pool above price
    assert eql is not None and eql <= 101.0             # equal-lows pool below price


def test_analyze_penalises_chasing_premium_over_discount():
    highs = np.full(60, 110.0)
    lows = np.full(60, 90.0)
    mid = (highs + lows) / 2.0
    idx = pd.date_range("2026-06-23 09:15", periods=60, freq="5min")

    def _mk(last_close):
        closes = np.r_[mid[:-1], last_close]
        return pd.DataFrame({"open": mid, "high": highs, "low": lows,
                             "close": closes, "volume": np.full(60, 1000.0)}, index=idx)

    prem = PriceActionAnalyzer().analyze(_mk(109.0), "BUY", use_liquidity=False)
    disc = PriceActionAnalyzer().analyze(_mk(91.0), "BUY", use_liquidity=False)
    assert prem["zone"] == "PREMIUM" and disc["zone"] == "DISCOUNT"
    assert disc["score"] > prem["score"]   # buying the discount beats chasing the top


def test_liquidity_toggle_off_skips_scoring():
    uh, ul = _osc_trend("up")
    df = _df_from(uh, ul)
    on = PriceActionAnalyzer().analyze(df, "BUY", use_liquidity=True, use_premium_discount=True)
    off = PriceActionAnalyzer().analyze(df, "BUY", use_liquidity=False, use_premium_discount=False)
    # off path must not carry premium/discount or liquidity reasons
    assert not any("premium" in r.lower() or "discount" in r.lower() or "liquidity" in r.lower()
                   for r in off["reasons"])
    assert on is not None and off is not None


def test_aroon_context_marks_late_uptrend_caution():
    highs = np.linspace(100.0, 140.0, 60)
    lows = highs - 2.0

    ctx = aroon_context(highs, lows, period=25)

    assert ctx["available"] is True
    assert ctx["trend"] == "UP"
    assert ctx["freshness"] == "LATE"
    assert ctx["late_trend_caution"] is True


def test_wavetrend_context_is_dashboard_only_telemetry():
    highs = np.linspace(100.0, 130.0, 60)
    lows = highs - 2.0
    closes = highs - 0.5

    ctx = wavetrend_context(highs, lows, closes)

    assert ctx["available"] is True
    assert ctx["dashboard_only"] is True
    assert ctx["exhaustion"] in {"OVERBOUGHT", "OVERSOLD", "NEUTRAL"}


def test_price_action_exposes_profile_target_and_trailing_context():
    idx = pd.date_range("2026-07-03 09:15", periods=80, freq="5min")
    close = np.r_[np.full(79, 100.0), 107.0]
    high = close + 1.0
    low = close - 1.0
    volume = np.r_[np.full(79, 1000.0), 4000.0]
    df = pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )

    ctx = PriceActionAnalyzer().analyze(df, "BUY")

    assert ctx is not None
    assert ctx["market_structure_profile"]
    assert ctx["target_context"]
    assert ctx["trailing_context"]
    assert "profile" in " ".join(ctx["reasons"]).lower()
