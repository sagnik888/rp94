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


def option_stop_tolerance_pct(
    configured_cap_pct: float,
    regime,
    *,
    confidence=None,
    grade: str = "",
    fast_entry: bool = False,
    setup=None,
) -> float:
    """Absolute premium-loss ceiling for every bought option entry.

    Strong/ordinary setups get at most 10%; range/chop/volatility can use 15%.
    Fast burst entries and demonstrably weak setups get at most 5%. A lower
    operator limit always wins. Natural/ATR stops may be tighter than this cap.
    UTS supplies its native setup policy instead of using grade-based overrides.
    """
    if setup is not None:
        from engine import uts_lane
        if uts_lane.is_uts(setup):
            return uts_lane.premium_cap_pct(setup, operator_cap=configured_cap_pct, regime=regime)
    try:
        operator_cap = float(configured_cap_pct)
    except (TypeError, ValueError):
        operator_cap = 15.0
    if not 0.0 < operator_cap < float("inf"):
        operator_cap = 15.0
    cap = min(operator_cap, 15.0)
    if normalize_regime(regime) not in {MarketRegime.RANGING, MarketRegime.VOLATILE}:
        cap = min(cap, 10.0)
    try:
        weak_confidence = confidence is not None and float(confidence) <= 0.35
    except (TypeError, ValueError):
        weak_confidence = False
    if fast_entry or weak_confidence or str(grade or "").strip().upper().split(" ")[0] == "C":
        cap = min(cap, 5.0)
    return cap


def is_risky_regime(value) -> bool:
    return normalize_regime(value) in {
        MarketRegime.RANGING,
        MarketRegime.VOLATILE,
        MarketRegime.UNKNOWN,
    }


def uses_tight_trailing(value) -> bool:
    """Regimes whose stop trails the PREVIOUS 1-MINUTE candle's extreme.

    VOLATILE is deliberately absent. Scanner._first_rung_regime_retention carries
    a table measured over 47 stored sessions — heat as a fraction of peak, across
    every winner:

        TRENDING_UP 8%  TRENDING_DOWN 28%  RANGING 35%
        BREAKOUT_DOWN 43%  BREAKOUT_UP 47%  VOLATILE 49%

    and loosens the profit-lock floor to 0.38 for the three highest-heat regimes
    because "the MEDIAN winner gives back 49%" in VOLATILE. Pinning that same
    trade to a 1-minute candle extreme contradicted it outright, and the tighter
    of two stops always binds — so the measured loosening never reached the
    regime it was measured for. VOLATILE keeps every other risky-regime
    treatment (is_risky_regime still returns True); only the trail changes.

    RANGING (35%) and UNKNOWN stay: mid-range heat and no reading at all are both
    cases where the tighter trail is the defensible default.
    """
    return normalize_regime(value) in {
        MarketRegime.RANGING,
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
