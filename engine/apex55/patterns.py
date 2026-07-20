"""
apex_emacross/patterns.py
39-pattern candlestick recognition engine.
Mirrors Pine Script Section 3e exactly (APEX v5.5).

Pattern groups
─────────────
  Single-bar  (9):  Doji, Dragonfly, Gravestone, Hammer, Inv-Hammer,
                    Bull/Bear Marubozu, Bull/Bear Belt-Hold
  Double-bar (14):  Engulfing, Harami, Harami-Cross, Piercing, Dark-Cloud,
                    Tweezer, Kicker, Meeting-Lines  (×2 directions each)
  Triple-bar (12):  Morning/Evening Star (+Doji), 3-Soldiers/Crows,
                    3-Inside/Outside, Abandoned Baby  (×2 directions)
  Continuation (4): Rising/Falling 3-Methods, Upside/Downside Tasuki

All Series use .fillna(False) to avoid NaN propagation at early bars.
"""

import numpy as np
import pandas as pd


# ── Vectorised np.select name builder ────────────────────────────────────
def _name_select(conditions_names: list, default: str = "") -> np.ndarray:
    conds  = [c for c, _ in conditions_names]
    names  = [n for _, n in conditions_names]
    return np.select(conds, names, default=default)


def compute_patterns(df: pd.DataFrame, config) -> pd.DataFrame:
    """
    Detect all 39 candlestick patterns on an indicators-enriched DataFrame.

    Args:
        df:     Output of compute_all_indicators() — must already contain all
                body/wick/bull/bear/doji columns for bars 0-2, plus ema21,
                atr, p_min_body, p_big_body, vol_above, vol_spike, rel_vol,
                bull_trend, bear_trend.
        config: ApexConfig instance.

    Returns:
        df copy with pattern boolean columns + summary columns added.
    """
    out = df.copy()

    if not config.show_patterns:
        for col in ["pat_valid_bull", "pat_valid_bear",
                    "pat_strong_bull", "pat_strong_bear",
                    "pat_bull_name",   "pat_bear_name"]:
            out[col] = False if "name" not in col else ""
        return out

    # ── Aliases ───────────────────────────────────────────────────────────
    c, o, h, l = out["close"], out["open"], out["high"], out["low"]
    c1, o1, h1, l1 = c.shift(1), o.shift(1), h.shift(1), l.shift(1)
    c2, o2, h2, l2 = c.shift(2), o.shift(2), h.shift(2), l.shift(2)
    c3, o3, h3, l3 = c.shift(3), o.shift(3), h.shift(3), l.shift(3)
    c4, o4, h4, l4 = c.shift(4), o.shift(4), h.shift(4), l.shift(4)

    body0, body1, body2 = out["body0"], out["body1"], out["body2"]
    rng0, rng1          = out["range0"], out["range1"]
    uw0, uw1, uw2       = out["u_wick0"], out["u_wick1"], out["u_wick2"]
    lw0, lw1, lw2       = out["l_wick0"], out["l_wick1"], out["l_wick2"]
    bull0, bull1, bull2 = out["bull0"], out["bull1"], out["bull2"]
    bear0, bear1, bear2 = out["bear0"], out["bear1"], out["bear2"]
    doji0, doji1        = out["doji0"], out["doji1"]

    mn  = out["p_min_body"]    # atr * 0.15
    big = out["p_big_body"]    # atr * 0.55
    atr = out["atr"]
    e21 = out["ema21"]

    vol_ok  = (out["vol_above"] | out["vol_spike"]).fillna(False)
    sz_ok   = (rng0 >= atr * 0.25).fillna(False)

    # ── Context helpers (Pine Section 3e) ─────────────────────────────────
    p_near_res  = (h >= h.shift(1).rolling(20).max() * 0.997).fillna(False)
    p_near_sup  = (l <= l.shift(1).rolling(20).min() * 1.003).fillna(False)
    p_dn1       = bear1.fillna(False)
    p_dn2       = (bear1 & bear2).fillna(False)
    p_up1       = bull1.fillna(False)
    p_up2       = (bull1 & bull2).fillna(False)
    p_in_dn     = ((c < e21) & p_dn1).fillna(False)
    p_in_up     = ((c > e21) & p_up1).fillna(False)
    bull_rev_c  = (p_dn2 | (p_dn1 & (c < e21)) | (p_near_sup & p_dn1)).fillna(False)
    bear_rev_c  = (p_up2 | (p_up1 & (c > e21)) | (p_near_res & p_up1)).fillna(False)

    # Helper: apply group gate  (returns False series if group disabled)
    idx = out.index

    def _s(cond, grp="single"):
        gate = (grp == "single" and config.pat_single_grp) or \
               (grp == "double" and config.pat_double_grp) or \
               (grp == "triple" and config.pat_triple_grp) or \
               (grp == "cont"   and config.pat_cont_grp)
        return cond.fillna(False) if gate else pd.Series(False, index=idx)

    # ═══════════════════════════════════════════════════════════════════════
    # SINGLE-BAR PATTERNS
    # ═══════════════════════════════════════════════════════════════════════
    pat_doji        = _s(doji0 & (rng0 >= mn), "single")
    pat_dragonfly   = _s(doji0 & (rng0 >= mn) & (lw0 >= rng0 * 0.65) & (uw0 <= rng0 * 0.10), "single")
    pat_gravestone  = _s(doji0 & (rng0 >= mn) & (uw0 >= rng0 * 0.65) & (lw0 <= rng0 * 0.10), "single")
    pat_hammer_s    = _s((body0 >= mn) & (lw0 >= body0 * 2.0) & (uw0 <= body0 * 0.35), "single")
    pat_inv_hmr_s   = _s((body0 >= mn) & (uw0 >= body0 * 2.0) & (lw0 <= body0 * 0.35), "single")
    pat_bull_marub  = _s(bull0 & (body0 >= big) & (uw0 <= body0 * 0.05) & (lw0 <= body0 * 0.05), "single")
    pat_bear_marub  = _s(bear0 & (body0 >= big) & (uw0 <= body0 * 0.05) & (lw0 <= body0 * 0.05), "single")
    pat_bull_belt   = _s(bull0 & (body0 >= big * 0.65) & (lw0 <= body0 * 0.06), "single")
    pat_bear_belt   = _s(bear0 & (body0 >= big * 0.65) & (uw0 <= body0 * 0.06), "single")

    # ═══════════════════════════════════════════════════════════════════════
    # DOUBLE-BAR PATTERNS
    # ═══════════════════════════════════════════════════════════════════════
    pat_bull_engulf  = _s(bear1 & bull0 & (body1 >= mn) & (body0 >= mn) & (o <= c1) & (c >= o1), "double")
    pat_bear_engulf  = _s(bull1 & bear0 & (body1 >= mn) & (body0 >= mn) & (o >= c1) & (c <= o1), "double")
    pat_bull_harami  = _s(bear1 & bull0 & (body1 >= big*0.65) & (body0 < body1*0.55) & (o > c1) & (c < o1), "double")
    pat_bear_harami  = _s(bull1 & bear0 & (body1 >= big*0.65) & (body0 < body1*0.55) & (o < c1) & (c > o1), "double")
    pat_bull_haramix = _s(bear1 & doji0 & (body1 >= big*0.65) & (h <= o1) & (l >= c1), "double")
    pat_bear_haramix = _s(bull1 & doji0 & (body1 >= big*0.65) & (h <= c1) & (l >= o1), "double")
    pat_piercing     = _s(bear1 & bull0 & (body1 >= big*0.55) & (o < c1) &
                          (c > c1 + (o1 - c1) * 0.50) & (c < o1), "double")
    pat_dark_cloud   = _s(bull1 & bear0 & (body1 >= big*0.55) & (o > c1) &
                          (c < o1 + (c1 - o1) * 0.50) & (c > o1), "double")
    pat_tweez_bot    = _s(bear1 & bull0 & ((l - l1).abs() <= atr * 0.12), "double")
    pat_tweez_top    = _s(bull1 & bear0 & ((h - h1).abs() <= atr * 0.12), "double")
    pat_bull_kicker  = _s(bear1 & bull0 & (body1 >= big*0.55) & (body0 >= big*0.55) & (o >= o1), "double")
    pat_bear_kicker  = _s(bull1 & bear0 & (body1 >= big*0.55) & (body0 >= big*0.55) & (o <= o1), "double")
    pat_bull_meet    = _s(bear1 & bull0 & (body1 >= big*0.55) & (body0 >= big*0.55) & ((c - c1).abs() <= atr * 0.08), "double")
    pat_bear_meet    = _s(bull1 & bear0 & (body1 >= big*0.55) & (body0 >= big*0.55) & ((c - c1).abs() <= atr * 0.08), "double")

    # ═══════════════════════════════════════════════════════════════════════
    # TRIPLE-BAR PATTERNS
    # ═══════════════════════════════════════════════════════════════════════
    mid2_bear = c2 + (o2 - c2) * 0.50   # midpoint of bear candle 2 bars ago
    mid2_bull = o2 + (c2 - o2) * 0.50   # midpoint of bull candle 2 bars ago

    pat_morn_star    = _s(bear2 & (body1 <= out["body2"]*0.45) & bull0 &
                          (out["body2"] >= big*0.55) & (body0 >= big*0.40) & (c >= mid2_bear), "triple")
    pat_eve_star     = _s(bull2 & (body1 <= out["body2"]*0.45) & bear0 &
                          (out["body2"] >= big*0.55) & (body0 >= big*0.40) & (c <= mid2_bull), "triple")
    pat_morn_star_d  = _s(bear2 & doji1 & bull0 &
                          (out["body2"] >= big*0.55) & (body0 >= big*0.40) & (c >= mid2_bear), "triple")
    pat_eve_star_d   = _s(bull2 & doji1 & bear0 &
                          (out["body2"] >= big*0.55) & (body0 >= big*0.40) & (c <= mid2_bull), "triple")

    body2s = out["body2"]
    uw2s   = out["u_wick2"]; lw2s = out["l_wick2"]
    pat_3white   = _s(bull0 & bull1 & bull2 & (c>c1) & (c1>c2) &
                      (body0>=big*0.40) & (body1>=big*0.40) & (body2s>=big*0.40) &
                      (uw0<=body0*0.35) & (uw1<=body1*0.35) & (uw2s<=body2s*0.35) &
                      (o>o1) & (o1>o2), "triple")
    pat_3crows   = _s(bear0 & bear1 & bear2 & (c<c1) & (c1<c2) &
                      (body0>=big*0.40) & (body1>=big*0.40) & (body2s>=big*0.40) &
                      (lw0<=body0*0.35) & (lw1<=body1*0.35) & (lw2s<=body2s*0.35) &
                      (o<o1) & (o1<o2), "triple")
    pat_3in_up   = _s(bear2 & bull1 & bull0 & (body2s>=big*0.55) &
                      (o1>=c2) & (c1<=o2) & (c>o2), "triple")
    pat_3in_dn   = _s(bull2 & bear1 & bear0 & (body2s>=big*0.55) &
                      (o1<=c2) & (c1>=o2) & (c<o2), "triple")
    pat_3out_up  = _s(bear2 & bull1 & bull0 & (body2s>=mn) &
                      (body1>=body2s*0.90) & (o1<=c2) & (c1>=o2) & (c>c1), "triple")
    pat_3out_dn  = _s(bull2 & bear1 & bear0 & (body2s>=mn) &
                      (body1>=body2s*0.90) & (o1>=c2) & (c1<=o2) & (c<c1), "triple")

    # Abandoned Baby — strict gap: doji must gap away from both neighbors
    min_oc1 = pd.concat([o1, c1], axis=1).min(axis=1)
    max_oc1 = pd.concat([o1, c1], axis=1).max(axis=1)
    min_oc2 = pd.concat([o2, c2], axis=1).min(axis=1)
    max_oc2 = pd.concat([o2, c2], axis=1).max(axis=1)

    pat_bull_abab = _s(bear2 & doji1 & bull0 &
                       (out["body2"]>=big*0.50) & (body0>=big*0.35) &
                       (h1 <= min_oc2) & (l >= max_oc1), "triple")
    pat_bear_abab = _s(bull2 & doji1 & bear0 &
                       (out["body2"]>=big*0.50) & (body0>=big*0.35) &
                       (l1 >= max_oc2) & (h <= min_oc1), "triple")

    # ═══════════════════════════════════════════════════════════════════════
    # CONTINUATION PATTERNS  (5-bar lookback for rising/falling 3-methods)
    # ═══════════════════════════════════════════════════════════════════════
    body4 = (c4 - o4).abs()
    pat_rising3  = _s((c4>o4) & (body4>=big*0.50) &
                      (c3<o3) & (c2<o2) & (c1<o1) &
                      (h3<h4) & (h2<h4) & (h1<h4) &
                      (l3>l4) & (l2>l4) & (l1>l4) &
                      bull0 & (c>c4), "cont")
    pat_falling3 = _s((c4<o4) & (body4>=big*0.50) &
                      (c3>o3) & (c2>o2) & (c1>o1) &
                      (l3>l4) & (l2>l4) & (l1>l4) &
                      (h3<h4) & (h2<h4) & (h1<h4) &
                      bear0 & (c<c4), "cont")
    pat_up_tasuki  = _s(bull2 & bull1 & bear0 & (o1>c2) & (o<c1) & (c>c2) & (c<o1), "cont")
    pat_dn_tasuki  = _s(bear2 & bear1 & bull0 & (o1<c2) & (o>c1) & (c<c2) & (c>o1), "cont")

    # ═══════════════════════════════════════════════════════════════════════
    # RAW AGGREGATIONS
    # ═══════════════════════════════════════════════════════════════════════
    raw_bull_s = (pat_dragonfly | (pat_hammer_s & p_in_dn) | (pat_inv_hmr_s & p_in_dn)
                  | pat_bull_belt | pat_bull_marub).fillna(False)
    raw_bear_s = (pat_gravestone | (pat_hammer_s & p_in_up) | (pat_inv_hmr_s & p_in_up)
                  | pat_bear_belt | pat_bear_marub).fillna(False)
    raw_bull_d = (pat_bull_engulf | pat_bull_harami | pat_bull_haramix
                  | pat_piercing  | pat_tweez_bot  | pat_bull_kicker | pat_bull_meet).fillna(False)
    raw_bear_d = (pat_bear_engulf | pat_bear_harami | pat_bear_haramix
                  | pat_dark_cloud | pat_tweez_top | pat_bear_kicker | pat_bear_meet).fillna(False)
    raw_bull_t = (pat_morn_star | pat_morn_star_d | pat_3white
                  | pat_3in_up  | pat_3out_up    | pat_bull_abab).fillna(False)
    raw_bear_t = (pat_eve_star  | pat_eve_star_d  | pat_3crows
                  | pat_3in_dn  | pat_3out_dn    | pat_bear_abab).fillna(False)
    raw_bull_c = (pat_rising3  | pat_up_tasuki).fillna(False)
    raw_bear_c = (pat_falling3 | pat_dn_tasuki).fillna(False)

    # ═══════════════════════════════════════════════════════════════════════
    # VALIDATION  (size ✓, volume ✓, context ✓)
    # ═══════════════════════════════════════════════════════════════════════
    bull_trend = out["bull_trend"].fillna(False)
    bear_trend = out["bear_trend"].fillna(False)

    valid_bull_rev  = (raw_bull_s | raw_bull_d | raw_bull_t) & sz_ok & vol_ok & bull_rev_c
    valid_bear_rev  = (raw_bear_s | raw_bear_d | raw_bear_t) & sz_ok & vol_ok & bear_rev_c
    valid_bull_cont = raw_bull_c & sz_ok & vol_ok & bull_trend
    valid_bear_cont = raw_bear_c & sz_ok & vol_ok & bear_trend

    out["pat_valid_bull"] = (valid_bull_rev | valid_bull_cont).fillna(False)
    out["pat_valid_bear"] = (valid_bear_rev | valid_bear_cont).fillna(False)

    # Strong-pattern subset
    strong_bull = (pat_bull_engulf | pat_3white   | pat_morn_star | pat_morn_star_d
                   | pat_bull_abab | pat_3in_up   | pat_3out_up   | pat_bull_kicker
                   | pat_rising3).fillna(False)
    strong_bear = (pat_bear_engulf | pat_3crows   | pat_eve_star  | pat_eve_star_d
                   | pat_bear_abab | pat_3in_dn   | pat_3out_dn   | pat_bear_kicker
                   | pat_falling3).fillna(False)

    out["pat_strong_bull"] = (out["pat_valid_bull"] & strong_bull).fillna(False)
    out["pat_strong_bear"] = (out["pat_valid_bear"] & strong_bear).fillna(False)

    # ── Store individual flags for downstream use ─────────────────────────
    _flags = {
        "pat_dragonfly": pat_dragonfly, "pat_gravestone": pat_gravestone,
        "pat_hammer_s": pat_hammer_s,   "pat_inv_hmr_s": pat_inv_hmr_s,
        "pat_bull_marub": pat_bull_marub,"pat_bear_marub": pat_bear_marub,
        "pat_bull_belt": pat_bull_belt,  "pat_bear_belt": pat_bear_belt,
        "pat_bull_engulf": pat_bull_engulf,"pat_bear_engulf": pat_bear_engulf,
        "pat_bull_harami": pat_bull_harami,"pat_bear_harami": pat_bear_harami,
        "pat_bull_haramix": pat_bull_haramix,"pat_bear_haramix": pat_bear_haramix,
        "pat_piercing": pat_piercing,    "pat_dark_cloud": pat_dark_cloud,
        "pat_tweez_bot": pat_tweez_bot,  "pat_tweez_top": pat_tweez_top,
        "pat_bull_kicker": pat_bull_kicker,"pat_bear_kicker": pat_bear_kicker,
        "pat_bull_meet": pat_bull_meet,  "pat_bear_meet": pat_bear_meet,
        "pat_morn_star": pat_morn_star,  "pat_eve_star": pat_eve_star,
        "pat_morn_star_d": pat_morn_star_d,"pat_eve_star_d": pat_eve_star_d,
        "pat_3white": pat_3white,        "pat_3crows": pat_3crows,
        "pat_3in_up": pat_3in_up,        "pat_3in_dn": pat_3in_dn,
        "pat_3out_up": pat_3out_up,      "pat_3out_dn": pat_3out_dn,
        "pat_bull_abab": pat_bull_abab,  "pat_bear_abab": pat_bear_abab,
        "pat_rising3": pat_rising3,      "pat_falling3": pat_falling3,
        "pat_up_tasuki": pat_up_tasuki,  "pat_dn_tasuki": pat_dn_tasuki,
        "p_in_dn": p_in_dn,             "p_in_up": p_in_up,
    }
    for k, s in _flags.items():
        out[k] = s

    # ═══════════════════════════════════════════════════════════════════════
    # PATTERN NAMES  (priority-order mirrors Pine's if-else chain)
    # ═══════════════════════════════════════════════════════════════════════
    vb = out["pat_valid_bull"]
    vbr = out["pat_valid_bear"]

    bull_name_pairs = [
        (vb & pat_bull_abab,   "Aband.Baby U"),
        (vb & pat_3white,      "3 White Sold"),
        (vb & pat_morn_star_d, "Morn.StarDji"),
        (vb & pat_morn_star,   "Morning Star"),
        (vb & pat_3in_up,      "3 Inside Up"),
        (vb & pat_3out_up,     "3 Outside Up"),
        (vb & pat_bull_kicker, "Bull Kicker"),
        (vb & pat_bull_engulf, "Bull Engulf"),
        (vb & pat_bull_haramix,"HaramiCross+"),
        (vb & pat_bull_harami, "Bull Harami"),
        (vb & pat_piercing,    "Piercing"),
        (vb & pat_tweez_bot,   "TweezBot"),
        (vb & pat_bull_meet,   "MeetLines+"),
        (vb & pat_rising3,     "Rising3Meth"),
        (vb & pat_up_tasuki,   "Up Tasuki"),
        (vb & pat_hammer_s & p_in_dn,   "Hammer"),
        (vb & pat_inv_hmr_s & p_in_dn,  "Inv.Hammer"),
        (vb & pat_dragonfly,   "Dragonfly"),
        (vb & pat_bull_marub,  "Marubozu+"),
        (vb & pat_bull_belt,   "BeltHold+"),
    ]
    bear_name_pairs = [
        (vbr & pat_bear_abab,   "Aband.Baby D"),
        (vbr & pat_3crows,      "3 Blk Crows"),
        (vbr & pat_eve_star_d,  "Eve.StarDoji"),
        (vbr & pat_eve_star,    "Evening Star"),
        (vbr & pat_3in_dn,      "3 Inside Dn"),
        (vbr & pat_3out_dn,     "3 Outside Dn"),
        (vbr & pat_bear_kicker, "Bear Kicker"),
        (vbr & pat_bear_engulf, "Bear Engulf"),
        (vbr & pat_bear_haramix,"HaramiCross-"),
        (vbr & pat_bear_harami, "Bear Harami"),
        (vbr & pat_dark_cloud,  "DarkCloud"),
        (vbr & pat_tweez_top,   "TweezTop"),
        (vbr & pat_bear_meet,   "MeetLines-"),
        (vbr & pat_falling3,    "Falling3Meth"),
        (vbr & pat_dn_tasuki,   "Dn Tasuki"),
        (vbr & pat_hammer_s & p_in_up,  "HangingMan"),
        (vbr & pat_inv_hmr_s & p_in_up, "Shoot.Star"),
        (vbr & pat_gravestone,  "Gravestone"),
        (vbr & pat_bear_marub,  "Marubozu-"),
        (vbr & pat_bear_belt,   "BeltHold-"),
    ]

    out["pat_bull_name"] = _name_select(bull_name_pairs, default="")
    out["pat_bear_name"] = _name_select(bear_name_pairs, default="")

    return out
