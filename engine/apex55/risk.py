"""
apex_emacross/risk.py
ATR-based and Fixed-% SL / TP calculation.
Mirrors Pine Script Section 7 (calcSL / calcTargets) exactly.

Logic:
  • Use ATR-based SL when trending OR volatile (highVol or isTrending)
  • Otherwise fall back to fixedSLPct
  • ATR multiplier is scaled: highVol → ×1.3, lowVol → ×0.8, normal → ×1.0
  • SL1 = primary stop  |  SL2 = catastrophic stop (×1.8)
  • TP1 / TP2 / TP3 = entry ± risk × R-multiple
"""

import numpy as np
from typing import Tuple


def calc_sl(
    is_long:      bool,
    entry_px:     float,
    anchor_low:   float,
    anchor_high:  float,
    atr_v:        float,
    config,
    high_vol:     bool,
    low_vol:      bool,
    is_trending:  bool,
) -> Tuple[float, float, str]:
    """
    Compute SL1, SL2, and the mode string.

    Pine equivalent:
        calcSL(isLong, ep, anchorLow, anchorHigh) inside SECTION 7.

    Returns:
        (sl1, sl2, mode_str)   where mode_str is 'ATR-Based' or 'Fixed %'
    """
    use_fixed = not (high_vol or is_trending)

    if use_fixed:
        pct1 = config.fixed_sl_pct / 100.0
        pct2 = config.fixed_sl_pct * 1.5 / 100.0
        if is_long:
            sl1 = entry_px * (1.0 - pct1)
            sl2 = entry_px * (1.0 - pct2)
        else:
            sl1 = entry_px * (1.0 + pct1)
            sl2 = entry_px * (1.0 + pct2)
        return sl1, sl2, "Fixed %"

    # ATR-based
    mult = (config.atr_mult * 1.3 if high_vol
            else config.atr_mult * 0.8 if low_vol
            else config.atr_mult)

    if is_long:
        sl1 = anchor_low  - atr_v * mult
        sl2 = anchor_low  - atr_v * mult * 1.8
    else:
        sl1 = anchor_high + atr_v * mult
        sl2 = anchor_high + atr_v * mult * 1.8

    return sl1, sl2, "ATR-Based"


def calc_targets(
    is_long:   bool,
    entry_px:  float,
    sl1:       float,
    config,
) -> Tuple[float, float, float]:
    """
    Compute TP1, TP2, TP3 from entry and risk.

    Pine equivalent: calcTargets(isLong, ep, sl1v) in SECTION 7.

    Returns:
        (tp1, tp2, tp3)
    """
    risk = abs(entry_px - sl1)
    if is_long:
        return (
            entry_px + risk * config.t1r,
            entry_px + risk * config.t2r,
            entry_px + risk * config.t3r,
        )
    else:
        return (
            entry_px - risk * config.t1r,
            entry_px - risk * config.t2r,
            entry_px - risk * config.t3r,
        )


def update_trailing_stop(
    trade_dir:   str,
    tsl:         float,
    entry_px:    float,
    sl1:         float,
    peak_px:     float,
    trough_px:   float,
    atr_v:       float,
    t1_hit:      bool,
    t2_hit:      bool,
    prof_locked: bool,
    curr_rr:     float,
    high_vol:    bool,
    config,
) -> Tuple[float, bool]:
    """
    Update the trailing stop level.
    Mirrors Pine Script Sections 12 exactly.

    Returns:
        (new_tsl, new_prof_locked)
    """
    new_tsl        = tsl
    new_prof_locked = prof_locked
    risk = abs(entry_px - sl1)

    if risk <= 0.0:
        return new_tsl, new_prof_locked

    if trade_dir == "LONG":
        # Lock profit at T1
        if config.lock_at_t1 and t1_hit and not prof_locked:
            new_tsl         = max(tsl, entry_px + risk * 0.4)
            new_prof_locked = True

        # Activate trail once R:R threshold is reached
        if curr_rr >= config.trail_start_r:
            loose = (config.trail_mult * 1.5 if (high_vol and not t2_hit)
                     else config.trail_mult * 1.3 if t2_hit
                     else config.trail_mult)
            candidate = peak_px - atr_v * loose
            if candidate > new_tsl:
                new_tsl = candidate

    else:   # SHORT
        if config.lock_at_t1 and t1_hit and not prof_locked:
            new_tsl         = min(tsl, entry_px - risk * 0.4)
            new_prof_locked = True

        if curr_rr >= config.trail_start_r:
            loose = (config.trail_mult * 1.5 if (high_vol and not t2_hit)
                     else config.trail_mult * 1.3 if t2_hit
                     else config.trail_mult)
            candidate = trough_px + atr_v * loose
            if candidate < new_tsl:
                new_tsl = candidate

    return new_tsl, new_prof_locked
