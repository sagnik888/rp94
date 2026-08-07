"""How urgently an entry needs to be filled, from the momentum behind it.

The broker chooses MARKET or LIMIT on CONFIDENCE alone: "a limit is only meaningful when
the conviction is high enough that missing the fill beats paying the spread"
(kite_broker._submit_entry). That reasoning is sound and incomplete — it weighs how good
the setup is, and never how fast the premium is moving away from the bid.

MACD histogram acceleration answers exactly that. A histogram expanding in the trade's
direction means the move is still accelerating, so a resting limit is likely to be left
behind: the fill you miss costs more than the spread you saved. A flat or contracting
histogram means the premium is not running, and patience is cheap.

So urgency does not replace confidence — it raises the confidence bar a LIMIT has to clear.
A strong signal into an accelerating move takes the market; the same signal into a stalling
one still waits for its price.

Pure functions, no imports from the trading stack, so the broker never has to know what
MACD is.
"""

from __future__ import annotations

from typing import Any


def _enabled(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def momentum_urgency(
    macd_histogram: float,
    macd_previous: float,
    direction: str,
) -> float:
    """0.0 (no hurry) to 1.0 (the move is running away), from MACD acceleration.

    Only acceleration IN THE TRADE'S FAVOUR creates urgency. A histogram accelerating
    against the trade is a reason to reconsider the entry altogether, which is the
    signal filters' job — not a reason to pay the spread faster.
    """
    try:
        now = float(macd_histogram or 0.0)
        prev = float(macd_previous or 0.0)
    except (TypeError, ValueError):
        return 0.0

    want_up = str(direction or "").strip().upper() in {"LONG", "BUY", "CE"}
    # Signed so that "with the trade" is always positive.
    now_signed = now if want_up else -now
    prev_signed = prev if want_up else -prev

    if now_signed <= 0:
        return 0.0                      # momentum is not behind this trade at all
    growth = now_signed - prev_signed
    if growth <= 0:
        return 0.0                      # still positive but decelerating: patience is fine

    # Scale the expansion against the histogram's own size, so this reads the same on a
    # NIFTY histogram of 12 and a MIDCPNIFTY one of 40 rather than needing a per-index
    # constant — the mistake that made several thresholds in this system index-specific.
    scale = max(abs(now_signed), abs(prev_signed))
    if scale <= 0:
        return 0.0
    return max(0.0, min(1.0, growth / scale))


def limit_confidence_penalty(urgency: float, settings: Any) -> float:
    """How much higher the LIMIT confidence bar sits, given the urgency.

    Returns 0.0 when disabled or when nothing is accelerating, so the broker's existing
    threshold is untouched in every case this does not speak to.
    """
    if not _enabled(getattr(settings, "macd_aware_entry_pricing", False), False):
        return 0.0
    try:
        value = float(urgency or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    try:
        span = float(getattr(settings, "macd_entry_urgency_max_penalty", 0.15) or 0.0)
    except (TypeError, ValueError):
        span = 0.15
    span = max(0.0, min(0.5, span))
    return max(0.0, min(1.0, value)) * span
