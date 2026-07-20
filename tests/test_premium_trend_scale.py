"""Day-type-aware premium/discount penalty (2026-07-10).

On a CONFIRMED aligned trend day the "chasing the extreme" penalty is scaled
(default 0.5x): LONG-in-premium on TREND_DAY_UP and SELL-in-discount on
TREND_DAY_DOWN are continuation entries, not chases. Range days, low-confidence
phases, and counter-trend cases keep the full penalty. Fully reversible via
price_action_premium_trend_scaled=False.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from engine.signal_processor import price_action_context
from intelligence.price_action import PriceActionAnalyzer


def _uptrend_df(bars=80, start=24000.0, step=6.0):
    """Steady uptrend closing at the top of its range (premium zone)."""
    idx = pd.date_range("2026-07-10 09:15", periods=bars, freq="1min")
    closes = start + np.arange(bars) * step
    highs = closes + 4
    lows = closes - 6
    opens = closes - 2
    vol = np.full(bars, 1000.0)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vol},
        index=idx,
    )


def _premium_reason(ctx):
    return next((r for r in ctx.get("reasons", []) if "in premium" in r), "")


def test_full_penalty_by_default_and_scaled_when_requested():
    df = _uptrend_df()
    full = PriceActionAnalyzer().analyze(df, "BUY")
    scaled = PriceActionAnalyzer().analyze(df, "BUY", premium_trend_scale=0.5)
    assert full is not None and scaled is not None
    r_full, r_scaled = _premium_reason(full), _premium_reason(scaled)
    assert r_full and "[trend-scaled]" not in r_full
    assert r_scaled and "[trend-scaled]" in r_scaled
    # Scaled score must be strictly better for the buyer (penalty halved).
    assert scaled["score"] > full["score"]
    assert scaled["score"] - full["score"] == pytest.approx(0.15, abs=1e-9)


def test_scale_never_touches_counter_trend_or_bonuses():
    df = _uptrend_df()
    # SELL in premium is a BONUS (+0.20) — identical with or without scale.
    a = PriceActionAnalyzer().analyze(df, "SELL")
    b = PriceActionAnalyzer().analyze(df, "SELL", premium_trend_scale=0.5)
    assert a is not None and b is not None
    assert a["score"] == pytest.approx(b["score"])


def _scanner_with_ctx(day_type, conf):
    sc = SimpleNamespace()
    df = _uptrend_df()
    sc.candles = SimpleNamespace(get_candles=lambda inst, tf: df)
    sc._cached_timeframe_intel = lambda base, tf, allow_fallback=True: {
        "session_context": {
            "effective_day_type": day_type,
            "phase_confidence": conf,
            "day_type": day_type,
            "confidence": conf,
        }
    }
    return sc


def _sig(side="BUY"):
    return SimpleNamespace(signal_type=side, timestamp=datetime(2026, 7, 10, 11, 0))


def test_context_scales_only_confirmed_aligned_trend(monkeypatch):
    from config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "price_action_module_enabled", True)
    monkeypatch.setattr(settings, "price_action_premium_trend_scaled", True)
    monkeypatch.setattr(settings, "price_action_premium_trend_scale", 0.5)
    monkeypatch.setattr(settings, "price_action_premium_trend_min_confidence", 0.70)

    # Confirmed aligned trend: penalty scaled.
    ctx = price_action_context(
        _scanner_with_ctx("TREND_DAY_UP", 0.80), "NIFTY", "5min", _sig("BUY"), settings
    )
    assert ctx is not None and "[trend-scaled]" in _premium_reason(ctx)

    # Low confidence: full penalty.
    ctx = price_action_context(
        _scanner_with_ctx("TREND_DAY_UP", 0.55), "NIFTY", "5min", _sig("BUY"), settings
    )
    assert ctx is not None and "[trend-scaled]" not in _premium_reason(ctx)

    # Range day: full penalty.
    ctx = price_action_context(
        _scanner_with_ctx("RANGE_DAY", 0.90), "NIFTY", "5min", _sig("BUY"), settings
    )
    assert ctx is not None and "[trend-scaled]" not in _premium_reason(ctx)

    # Counter-trend (SELL on TREND_DAY_UP): full behaviour (bonus case anyway).
    ctx = price_action_context(
        _scanner_with_ctx("TREND_DAY_UP", 0.90), "NIFTY", "5min", _sig("SELL"), settings
    )
    assert ctx is not None and "[trend-scaled]" not in " ".join(ctx.get("reasons", []))

    # Kill switch restores day-type-blind behaviour exactly.
    monkeypatch.setattr(settings, "price_action_premium_trend_scaled", False)
    ctx = price_action_context(
        _scanner_with_ctx("TREND_DAY_UP", 0.90), "NIFTY", "5min", _sig("BUY"), settings
    )
    assert ctx is not None and "[trend-scaled]" not in _premium_reason(ctx)


def test_settings_declared():
    from config.settings import Settings

    fields = Settings.model_fields
    assert fields["price_action_premium_trend_scaled"].default is True
    assert fields["price_action_premium_trend_scale"].default == 0.5
    assert fields["price_action_premium_trend_min_confidence"].default == 0.70
