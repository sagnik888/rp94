"""
Trend-continuation entry + regime soft-direction (2026-06-24 missed-move fixes).

Covers the three changes made after the system missed a NIFTY +80 / BANKNIFTY +300
grind that it rejected as VOLATILE / B+ @ 69% / ADX 24-26:

  [1] regime_detector: soft directional resolution (MA structure) so a grind whose
      strict price>sma9>sma21 stack breaks on a pullback is TRENDING_UP, not VOLATILE.
  [2] five_min_true_impulse_allowed: a higher-conviction continuation tier that admits
      a 15m-aligned trending signal at a LOWER ADX when confidence is strong.
  [3] live_5min_choppy_confidence_floor: relax the VOLATILE 5min floor 0.70 -> 0.65
      when the 15m trend agrees (RANGING/UNKNOWN keep 0.70).
"""

import numpy as np
import pandas as pd
import pytest

from config.settings import get_settings
from intelligence.regime_detector import RegimeDetector
from engine.market_regime import MarketRegime
from engine.ut_bot_core import UTBotEngine
from engine.signal_quality_policy import (
    five_min_true_impulse_allowed,
    live_5min_choppy_confidence_floor,
)


@pytest.fixture
def settings():
    s = get_settings()
    s.live_filter_leniency_pct = 0.0  # make relaxed_threshold an identity for exact floors
    s.regime_trend_soft_direction_enabled = True
    s.live_5min_trend_continuation_enabled = True
    s.live_5min_trend_continuation_adx = 28.0
    s.live_5min_trend_continuation_strong_conf = 0.65
    s.live_5min_trend_continuation_min_adx = 22.0
    s.live_5min_volatile_aligned_relax_enabled = True
    s.live_5min_volatile_aligned_min_confidence = 0.65
    s.live_5min_choppy_min_confidence = 0.70
    s.live_5min_bplus_min_confidence = 0.70
    s.live_5min_special_bplus_min_confidence = 0.60  # source of special_min_conf
    s.live_5min_special_adx_threshold = 30.0
    s.live_5min_special_intel_threshold = 0.75
    s.regime_range_day_trend_guard_enabled = True
    s.regime_range_day_trend_guard_min_confidence = 0.60
    return s


# ── helpers ──────────────────────────────────────────────────────────
def _grind_then_pullback_df():
    """Up-grind whose final pullback puts price BELOW sma9 but ABOVE sma21 (strict
    direction NEUTRAL), with an early tall high so the 60-bar breakout band does not
    fire. ADX>=20 and RSI>=50 — exactly the bucket that legacy code mislabels VOLATILE.
    """
    closes = [20250.0]            # early high -> 60-bar high (breakout band ~20230)
    p = 20000.0
    for _ in range(57):          # gentle grind well below the early high
        p += 2.0
        closes.append(round(p, 2))
    closes.append(round(p - 6, 2))   # shallow two-bar pullback
    closes.append(round(p - 10, 2))
    rows = [{"open": c - 0.4, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": 10000} for c in closes]
    rows[0] = {"open": 20100.0, "high": 20250.0, "low": 20090.0, "close": 20250.0, "volume": 10000}
    return pd.DataFrame(rows, index=pd.date_range("2026-06-24 09:15", periods=len(closes), freq="5min"))


def _chop_df():
    rng = np.random.default_rng(7)
    cl = [20000 + 30 * np.sin(i / 3.0) + rng.normal(0, 8) for i in range(60)]
    rows = [{"open": c - 1, "high": c + 5, "low": c - 5, "close": c, "volume": 10000} for c in cl]
    return pd.DataFrame(rows, index=pd.date_range("2026-06-24 09:15", periods=60, freq="5min"))


def _touching_breakout_df(direction="UP"):
    base = 100.0 + np.arange(60) * (0.04 if direction == "UP" else -0.04)
    if direction == "UP":
        high = base.copy()
        low = base - 0.08
        open_ = base - 0.03
    else:
        high = base + 0.08
        low = base.copy()
        open_ = base + 0.03
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": base,
            "volume": np.full(60, 10000),
        },
        index=pd.date_range("2026-07-07 09:15", periods=60, freq="5min"),
    )


def _patch_regime_strength(monkeypatch, adx_value=24.0):
    def fake_adx(df, period=14):
        series = pd.Series(float(adx_value), index=df.index)
        return series, series, series

    def fake_atr(df, period=14):
        return pd.Series(0.08, index=df.index)

    monkeypatch.setattr(UTBotEngine, "calculate_adx", staticmethod(fake_adx))
    monkeypatch.setattr(UTBotEngine, "calculate_atr", staticmethod(fake_atr))


def _range_day_ctx(**overrides):
    ctx = {
        "day_type": "RANGE_DAY",
        "confidence": 0.62,
        "opening_range_status": "INSIDE",
        "vwap_cross_count": 4,
        "close_above_vwap_pct": 54.0,
    }
    ctx.update(overrides)
    return ctx


# ── [1] regime soft direction ────────────────────────────────────────
def test_soft_direction_flips_volatile_grind_to_trending(settings):
    df = _grind_then_pullback_df()
    settings.regime_trend_soft_direction_enabled = False
    legacy = RegimeDetector().detect(df, "NIFTY", "5min")
    settings.regime_trend_soft_direction_enabled = True
    fixed = RegimeDetector().detect(df, "NIFTY", "5min")

    # Control: the strict stack is NEUTRAL and legacy dumps it into VOLATILE.
    assert legacy["direction"] == "NEUTRAL"
    assert legacy["adx"] >= 20
    assert legacy["regime"] == "VOLATILE"
    # Fix: the same grind is correctly TRENDING_UP.
    assert fixed["regime"] == "TRENDING_UP"


def test_smoothing_majority_votes_and_breaks_ties_by_recency(settings):
    rd = RegimeDetector()
    rd._regime_history["X_5min"] = ["TRENDING_UP", "TRENDING_UP", "VOLATILE", "TRENDING_UP", "BREAKOUT_UP"]
    assert rd._smooth_regime("X_5min", 5) == "TRENDING_UP"  # 3 of 5 -> majority
    rd._regime_history["Y_5min"] = ["TRENDING_UP", "VOLATILE", "TRENDING_UP", "VOLATILE"]
    assert rd._smooth_regime("Y_5min", 4) == "VOLATILE"     # 2-2 tie -> most recent wins


def test_hysteresis_holds_recent_trend_else_none(settings):
    rd = RegimeDetector()
    rd._regime_history["X_5min"] = ["TRENDING_UP", "BREAKOUT_UP", "VOLATILE", "TRENDING_UP", "BREAKOUT_UP"]
    assert rd._recent_trend_regime("X_5min") == MarketRegime.TRENDING_UP   # 4 up vs 0 down
    rd._regime_history["Z_5min"] = ["VOLATILE", "RANGING", "VOLATILE", "TRENDING_UP", "RANGING"]
    assert rd._recent_trend_regime("Z_5min") is None                       # genuine chop -> no hold


def test_smoothing_is_noop_on_single_fresh_detect(settings):
    # A one-shot detect() must be unaffected by smoothing (window holds just that bar),
    # so the soft-direction tests below still reflect the raw classification.
    df = _grind_then_pullback_df()
    settings.regime_smoothing_enabled = True
    r = RegimeDetector().detect(df, "NIFTY", "5min")
    assert r["regime"] == "TRENDING_UP"


def test_soft_direction_does_not_manufacture_trend_from_chop(settings):
    df = _chop_df()
    settings.regime_trend_soft_direction_enabled = True
    r = RegimeDetector().detect(df, "NIFTY", "5min")
    # Intertwined SMAs / low ADX must NOT become a trend just because soft is on.
    assert r["regime"] in {"RANGING", "VOLATILE"}


def test_confirmed_range_day_demotes_adx_breakout_up(settings, monkeypatch):
    _patch_regime_strength(monkeypatch, adx_value=24.0)

    r = RegimeDetector().detect(
        _touching_breakout_df("UP"),
        "NIFTY",
        "5min",
        session_context=_range_day_ctx(),
    )

    assert r["breakout_direction"] == "UP"
    assert r["range_day_trend_guard"] is True
    assert r["range_day_trend_guard_reason"] == "confirmed_range_day_demoted_BREAKOUT_UP"
    assert r["regime"] == "RANGING"


def test_confirmed_range_day_demotes_adx_breakout_down(settings, monkeypatch):
    _patch_regime_strength(monkeypatch, adx_value=24.0)

    r = RegimeDetector().detect(
        _touching_breakout_df("DOWN"),
        "BANKNIFTY",
        "5min",
        session_context=_range_day_ctx(close_above_vwap_pct=46.0),
    )

    assert r["breakout_direction"] == "DOWN"
    assert r["range_day_trend_guard"] is True
    assert r["range_day_trend_guard_reason"] == "confirmed_range_day_demoted_BREAKOUT_DOWN"
    assert r["regime"] == "RANGING"


def test_range_day_guard_does_not_block_true_directional_day(settings, monkeypatch):
    _patch_regime_strength(monkeypatch, adx_value=24.0)

    r = RegimeDetector().detect(
        _touching_breakout_df("UP"),
        "SENSEX",
        "5min",
        session_context={
            "day_type": "TREND_DAY_UP",
            "confidence": 0.78,
            "opening_range_status": "BREAK_UP",
            "vwap_cross_count": 1,
            "close_above_vwap_pct": 82.0,
        },
    )

    assert r["range_day_trend_guard"] is False
    assert r["regime"] == "BREAKOUT_UP"


def test_range_day_guard_demotes_smoothed_stale_trend(settings, monkeypatch):
    _patch_regime_strength(monkeypatch, adx_value=19.0)
    detector = RegimeDetector()
    detector._regime_history["SENSEX_5min"] = [
        "TRENDING_UP",
        "TRENDING_UP",
        "TRENDING_UP",
        "RANGING",
    ]

    r = detector.detect(
        _touching_breakout_df("UP"),
        "SENSEX",
        "5min",
        session_context=_range_day_ctx(),
    )

    assert r["confidence"] <= 60
    assert r["range_day_trend_guard"] is True
    assert r["range_day_trend_guard_reason"] == "confirmed_range_day_demoted_smoothed_TRENDING_UP"
    assert r["regime"] == "RANGING"


# ── [2] trend-continuation higher-conviction tier ────────────────────
def _impulse(**kw):
    base = dict(
        grade="B+", confidence=0.69, settings=kw.pop("settings"),
        trend_15m_agrees=True, impulse_confirmed=False, explosive_bypass=False,
        extreme_exhaustion=False, regime="TRENDING_UP", adx_value=26.0, intel_score=0.0,
    )
    base.update(kw)
    return five_min_true_impulse_allowed(base.pop("grade"), base.pop("confidence"), base.pop("settings"), **base)


def test_strong_conf_continuation_admits_low_adx_grind(settings):
    # The exact 2026-06-24 NIFTY case: B+ @ 69%, TRENDING_UP, ADX 26, 15m agrees,
    # no impulse reversal — admitted by the new higher-conviction tier.
    assert _impulse(settings=settings) is True


def test_strong_conf_continuation_requires_trending_regime(settings):
    assert _impulse(settings=settings, regime="VOLATILE") is False


def test_strong_conf_continuation_requires_min_adx(settings):
    assert _impulse(settings=settings, adx_value=20.0) is False  # below min_adx 22


def test_strong_conf_continuation_rejects_low_confidence(settings):
    # B @ 50% (the weaker leftover signals) stays rejected.
    assert _impulse(settings=settings, grade="B", confidence=0.50) is False


def test_base_continuation_tier_still_admits_high_adx(settings):
    # Original path: conf >= special floor (0.60) + ADX >= 28.
    assert _impulse(settings=settings, confidence=0.62, adx_value=30.0) is True


# ── [3] VOLATILE 5min floor relaxation ───────────────────────────────
def test_volatile_aligned_floor_relaxes_to_065(settings):
    assert live_5min_choppy_confidence_floor(settings, "VOLATILE", True) == pytest.approx(0.65)


def test_volatile_unaligned_floor_keeps_070(settings):
    assert live_5min_choppy_confidence_floor(settings, "VOLATILE", False) == pytest.approx(0.70)


def test_ranging_floor_never_relaxes(settings):
    assert live_5min_choppy_confidence_floor(settings, "RANGING", True) == pytest.approx(0.70)
    assert live_5min_choppy_confidence_floor(settings, "UNKNOWN", True) == pytest.approx(0.70)


def test_volatile_relax_respects_disable_toggle(settings):
    settings.live_5min_volatile_aligned_relax_enabled = False
    assert live_5min_choppy_confidence_floor(settings, "VOLATILE", True) == pytest.approx(0.70)
