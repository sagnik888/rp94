"""Shared loss-guard policy helpers (pure) used by Scanner + TradeManager.

Keeps severity → cut-R mapping and stacking caps in one place so live HFT,
session/hist, and TM backstop stay consistent (audit R2/R4/R6).
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


def cap_loss_guard_severity(
    severity: int,
    *,
    price_points: int = 0,
    intel_points: int = 0,
    max_total: int = 8,
    max_price: int = 5,
    max_intel: int = 5,
) -> int:
    """Prevent wick+grind+OI+PCR from instantly maxing severe cut (audit R4)."""
    p = min(max(0, int(price_points)), max(1, int(max_price)))
    i = min(max(0, int(intel_points)), max(1, int(max_intel)))
    if price_points or intel_points:
        capped = p + i
    else:
        capped = max(0, int(severity))
    return min(max(0, capped), max(1, int(max_total)))


def cut_r_from_severity(
    severity: int,
    *,
    base_r: float = 0.8,
    min_r: float = 0.35,
    soft_r: float = 0.55,
    big_thr: int = 5,
    soft_thr: int = 3,
    pullback_class: str = "",
) -> Tuple[float, str]:
    """Map severity + pullback class → early-loss cut R and label."""
    base_r = max(0.05, min(1.0, float(base_r)))
    min_r = max(0.10, min(base_r, float(min_r)))
    soft_r = max(min_r, min(base_r, float(soft_r)))
    big_thr = max(3, int(big_thr))
    soft_thr = max(2, int(soft_thr))
    pb = str(pullback_class or "")

    # Small pullback / noise: never accelerate below base unless severity already soft+
    if pb in {"noise", "small_pullback"} and severity < soft_thr:
        return base_r, "pullback_hold"
    if severity >= big_thr:
        return min(base_r, min_r), "BIG_REVERSE"
    if severity >= soft_thr:
        return min(base_r, soft_r), "SOFT_REVERSE"
    return base_r, "base"


def cascade_multiplier_for_inst(settings: Any, inst_type: str) -> float:
    """OPT cascade mult 2×–3× max; FUT always 1× (capital allocation bias)."""
    segment = str(inst_type or "FUT").upper()
    legacy = max(1.0, float(getattr(settings, "cascade_lot_multiplier", 2.0) or 2.0))
    if segment == "OPT":
        mult = float(getattr(settings, "cascade_lot_multiplier_opt", 0.0) or 0.0)
        if mult <= 0:
            mult = legacy
        opt_min = max(1.0, float(getattr(settings, "cascade_lot_multiplier_opt_min", 2.0) or 2.0))
        # Hard ceiling 3× as operator policy ("2x-3x max")
        opt_max = min(3.0, max(opt_min, float(getattr(settings, "cascade_lot_multiplier_opt", 3.0) or 3.0)))
        if opt_max < opt_min:
            opt_max = opt_min
        return max(opt_min, min(opt_max, mult))
    mult = float(getattr(settings, "cascade_lot_multiplier_fut", 0.0) or 0.0)
    return 1.0 if mult <= 0 else max(1.0, mult)


def heavy_trend_multiplier_for_inst(settings: Any, inst_type: str) -> float:
    """Non-cascade heavy trend (2/4 index align): OPT ×2 only; FUT 1×."""
    segment = str(inst_type or "FUT").upper()
    if segment == "OPT":
        mult = float(getattr(settings, "heavy_trend_lot_multiplier_opt", 2.0) or 2.0)
        return max(1.0, min(2.0, mult))  # hard ceiling 2×
    return 1.0  # FUT always flat on heavy-trend scale


def cascade_max_lots_for_inst(settings: Any, inst_type: str) -> int:
    segment = str(inst_type or "FUT").upper()
    global_max = max(0, int(getattr(settings, "cascade_lot_max_lots", 0) or 0))
    if segment == "OPT":
        seg = max(0, int(getattr(settings, "cascade_lot_max_lots_opt", 0) or 0))
    else:
        seg = max(0, int(getattr(settings, "cascade_lot_max_lots_fut", 0) or 0))
    # Segment cap wins when set; else global.
    if seg > 0:
        return seg
    return global_max


def apply_cascade_lot_scale(
    base_lots: int,
    *,
    mult: float,
    max_lots: int = 0,
    inst_type: str = "",
) -> Tuple[int, str]:
    """Scale lots once: final = floor(base × mult). Optional hard max (0 = unlimited).

    With base OPT lots=1 and cascade mult 2–3: result is 2–3 lots (not 6).
    Stress-test “6 lots” only happens when base=2 and mult=3 (2×3=6) — one scale.
    """
    import math

    base = max(1, int(base_lots or 1))
    mult = max(1.0, float(mult or 1.0))
    scaled = max(1, int(math.floor(base * mult)))
    seg = str(inst_type or "").upper() or "FUT"
    note = f"cascade[{seg}] base={base} ×{mult:.2f} → {scaled} lots (single scale)"
    if int(max_lots or 0) > 0 and scaled > int(max_lots):
        note += f" capped→{int(max_lots)}"
        scaled = int(max_lots)
    return scaled, note
