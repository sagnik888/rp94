"""Canonical market-regime names and policy traits."""

from __future__ import annotations

from enum import Enum


class MarketRegime(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"
    BREAKOUT_UP = "BREAKOUT_UP"
    BREAKOUT_DOWN = "BREAKOUT_DOWN"
    UNKNOWN = "UNKNOWN"


_ALIASES = {
    "TRENDING": MarketRegime.TRENDING_UP,
    "STRONG_TREND": MarketRegime.TRENDING_UP,
    "BREAKOUT": MarketRegime.UNKNOWN,
    "CHOPPY": MarketRegime.RANGING,
    "SIDEWAYS": MarketRegime.RANGING,
    "MEAN_REVERTING": MarketRegime.RANGING,
    "RANGEBOUND": MarketRegime.RANGING,
    "MOMENTUM": MarketRegime.UNKNOWN,
}


def normalize_regime(value, *, direction: str = "") -> MarketRegime:
    text = str(getattr(value, "value", value) or "UNKNOWN").strip().upper()
    if text == "BREAKOUT":
        side = str(direction or "").strip().upper()
        if side == "UP":
            return MarketRegime.BREAKOUT_UP
        if side == "DOWN":
            return MarketRegime.BREAKOUT_DOWN
        return MarketRegime.UNKNOWN
    if text in {"TRENDING", "STRONG_TREND"}:
        return (
            MarketRegime.TRENDING_DOWN
            if str(direction or "").strip().upper() == "DOWN"
            else MarketRegime.TRENDING_UP
        )
    try:
        return MarketRegime(text)
    except ValueError:
        return _ALIASES.get(text, MarketRegime.UNKNOWN)


def regime_name(value, *, direction: str = "") -> str:
    return normalize_regime(value, direction=direction).value


def is_trending_regime(value) -> bool:
    return normalize_regime(value) in {
        MarketRegime.TRENDING_UP,
        MarketRegime.TRENDING_DOWN,
        MarketRegime.BREAKOUT_UP,
        MarketRegime.BREAKOUT_DOWN,
    }


def futures_stop_tolerance_pct(configured_cap_pct: float, regime) -> float:
    """Return the regime-selected FUT stop percentage under the configured cap.

    The setting remains the operator's absolute ceiling. Regime policy may make
    the stop tighter, but never wider:
      * ranging/unknown: 0.10%
      * volatile:        0.15%
      * trend/breakout:  0.20%

    With the default 0.15% setting this resolves to 0.10% in ranging/unknown
    conditions and 0.15% in volatile/trending/breakout conditions. If the
    setting is later reduced to 0.02%, every regime resolves to 0.02%.
    """
    try:
        configured_cap = max(0.0, float(configured_cap_pct))
    except (TypeError, ValueError):
        configured_cap = 0.0

    canonical = normalize_regime(regime)
    if canonical in {MarketRegime.RANGING, MarketRegime.UNKNOWN}:
        regime_cap = 0.10
    elif canonical == MarketRegime.VOLATILE:
        regime_cap = 0.15
    else:
        regime_cap = 0.20
    return min(configured_cap, regime_cap)


def is_risky_regime(value) -> bool:
    return normalize_regime(value) in {
        MarketRegime.RANGING,
        MarketRegime.VOLATILE,
        MarketRegime.UNKNOWN,
    }


def uses_tight_trailing(value) -> bool:
    return normalize_regime(value) in {
        MarketRegime.RANGING,
        MarketRegime.VOLATILE,
        MarketRegime.UNKNOWN,
    }


def regime_aligns(value, signal_type: str) -> bool:
    regime = normalize_regime(value)
    side = str(signal_type or "").strip().upper()
    return (
        side == "BUY"
        and regime in {MarketRegime.TRENDING_UP, MarketRegime.BREAKOUT_UP}
    ) or (
        side == "SELL"
        and regime in {MarketRegime.TRENDING_DOWN, MarketRegime.BREAKOUT_DOWN}
    )
