"""Adaptive holding for bought options: cut what never turns green, then trail wide.

Off by default (``adaptive_hold_enabled``). The operator asked for a hold that "reads
the move and holds longer when the move needs it" after 2026-09-11, when four 13:25
LONGs would have lost ~Rs.1,200 closed at 15 minutes but made ~Rs.8,100 held to 30.

The walk-forward evidence at introduction is NEGATIVE, and that is why the default is
off. Leave-one-day-out over a 144-combination grid, real archived 1-min premium paths,
executable-ask entries, shared cost model:

    619 live signals, 10 days     adaptive -Rs.202/trade   current-style -Rs.161
    227 accepted trades, 35 days  adaptive -Rs.233/trade   current-style -Rs.153
                                  (difference CI -153..-14 per trade)

Widening the trail while the index keeps making new extremes changed the result by
-Rs.3/trade. Re-run that test on newer data before switching this on.

The rule, per bought option (CE for LONG, PE for SHORT; always long premium):

* never-green cut: at ``never_green_minutes`` after entry, if the best premium seen has
  not reached ``never_green_min_r`` x initial risk, close. (2026-08: peak_pnl == 0 was a
  loser 72 times out of 72.)
* activation: once the premium gain reaches ``activate_r`` x initial risk, the hold takes
  over profit-taking: the trail below governs instead of the profit locks, give-back
  guards, fixed target and runner lock.
* trail: stop = max(initial stop, breakeven after costs, peak - k x premium ATR), where k
  widens by ``momentum_widen_atr`` while the index is still making new extremes in the
  trade's direction. The stop only ratchets up.

It never holds into end-of-day: the caller disables it from the EOD-protect start, and
the 15:15 force exit is a separate path this module cannot reach.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class AdaptiveHoldParams:
    never_green_minutes: float = 5.0
    never_green_min_r: float = 0.15
    activate_r: float = 0.5
    trail_atr_mult: float = 3.5
    momentum_widen_atr: float = 1.5
    atr_period: int = 14


@dataclass(frozen=True)
class AdaptiveHoldDecision:
    action: str  # "NONE" | "HOLD" | "EXIT"
    reason: str = ""
    stop: float = 0.0  # premium-space trailing stop while holding
    detail: str = ""
    never_green_checked: bool = False


NONE = AdaptiveHoldDecision("NONE")


def true_range_atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int
) -> float:
    """Mean true range of the last ``period`` bars; 0.0 when there are too few."""
    n = min(len(highs), len(lows), len(closes))
    if n < 2 or period <= 0:
        return 0.0
    ranges = []
    for i in range(max(1, n - period), n):
        high, low, prev_close = float(highs[i]), float(lows[i]), float(closes[i - 1])
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(ranges) / len(ranges) if ranges else 0.0


def momentum_aligned(
    highs: Sequence[float],
    lows: Sequence[float],
    direction: str,
    *,
    recent: int = 3,
    lookback: int = 15,
) -> bool:
    """Whether the last ``recent`` bars made a new extreme versus the ``lookback`` before."""
    n = min(len(highs), len(lows))
    if n < recent + 3:
        return False
    start = max(0, n - recent - lookback)
    prior_h, prior_l = highs[start:n - recent], lows[start:n - recent]
    recent_h, recent_l = highs[n - recent:n], lows[n - recent:n]
    if not prior_h or not prior_l:
        return False
    if str(direction or "").upper() == "LONG":
        return max(recent_h) > max(prior_h)
    return min(recent_l) < min(prior_l)


def decide(
    *,
    entry: float,
    initial_stop: float,
    peak_premium: float,
    current_premium: float,
    minutes_held: float,
    premium_atr: float,
    momentum: bool,
    breakeven: float,
    params: AdaptiveHoldParams,
    active: bool,
    never_green_checked: bool,
    prior_stop: float = 0.0,
) -> AdaptiveHoldDecision:
    """One evaluation of the rule. Pure: the caller owns all state."""
    risk = float(entry) - float(initial_stop)
    if entry <= 0 or initial_stop <= 0 or risk <= 0:
        return AdaptiveHoldDecision("NONE", detail="no premium risk basis")
    peak_gain = max(0.0, float(peak_premium) - float(entry))
    checked = bool(never_green_checked)
    if (
        not active
        and params.never_green_minutes > 0
        and not checked
        and minutes_held >= params.never_green_minutes
    ):
        checked = True
        if peak_gain < params.never_green_min_r * risk:
            return AdaptiveHoldDecision(
                "EXIT",
                reason="ADAPTIVE_NEVER_GREEN",
                detail=(
                    f"best gain {peak_gain:.2f} < {params.never_green_min_r:.2f}R "
                    f"({params.never_green_min_r * risk:.2f}) after {minutes_held:.1f} min"
                ),
                never_green_checked=True,
            )
    if not active and peak_gain < params.activate_r * risk:
        return AdaptiveHoldDecision("NONE", never_green_checked=checked)
    k = params.trail_atr_mult + (params.momentum_widen_atr if momentum else 0.0)
    trail = float(peak_premium) - k * float(premium_atr) if premium_atr > 0 else 0.0
    stop = max(float(initial_stop), float(breakeven), trail, float(prior_stop or 0.0))
    detail = (
        f"peak {peak_premium:.2f}, trail k={k:.1f} x ATR {premium_atr:.2f}"
        f"{' (widened: index still extending)' if momentum else ''} -> stop {stop:.2f}"
    )
    if current_premium <= stop:
        return AdaptiveHoldDecision(
            "EXIT", reason="ADAPTIVE_TRAIL_STOP", stop=stop, detail=detail,
            never_green_checked=checked,
        )
    return AdaptiveHoldDecision("HOLD", stop=stop, detail=detail, never_green_checked=checked)
