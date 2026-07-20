"""
apex_emacross/confluence.py
10-component weighted Bull / Bear confluence score engine.
Mirrors Pine Script Section 4 exactly (APEX v5.5).

NOTE: In v5.5 (EMA Cross Edition) the score does NOT gate entries.
      It is computed purely for dashboard display and context.

Components (max raw score can exceed 100 → capped):
  C1  ADX + DI direction        0–20
  C2  MACD line + acceleration  0–18
  C3  VWAP deviation            0–14
  C4  EMA stack alignment       0–14
  C5  Volume × body position    0–12
  C6  RSI distance from 50      0–10
  C7  15m HTF EMA alignment     0–8  (4 if use_htf=False)
  C8  5m RSI side               0–4
  C9  New 5-bar high / low      0–4
  C10 Candlestick pattern bonus 0–15
  ─────────────────────────────────
  Total raw max                 0–119  → capped at 100
"""

import numpy as np
import pandas as pd


def compute_confluence(df: pd.DataFrame, config) -> pd.DataFrame:
    """
    Compute bull_score and bear_score (0-100) for every bar.

    Args:
        df:     DataFrame enriched by compute_all_indicators() and
                compute_patterns().
        config: ApexConfig instance.

    Returns:
        df copy with [bull_score, bear_score, score_diff, bias_text] added.
    """
    out  = df.copy()
    minA = config.min_adx

    # ── Aliases ───────────────────────────────────────────────────────────
    adx   = out["adx"]
    dip   = out["di_plus"]
    dim   = out["di_minus"]
    macdL = out["macd_line"]
    macdS = out["macd_signal"]
    maccB = out["macd_acc_bull"]
    maccR = out["macd_acc_bear"]
    rsi   = out["rsi"]
    rsi5  = out["rsi_5m"]
    e9_15 = out["ema9_15"]
    e21_15 = out["ema21_15"]
    slope15 = out["ema9_15_slope"]
    vwap  = out["vwap"]
    close = out["close"]
    vdev  = out["vwap_dev"]
    ema9  = out["ema9"]
    ema21 = out["ema21"]
    ema50 = out["ema50"]
    vol_s = out["vol_spike"]
    vol_a = out["vol_above"]
    bp    = out["body_pct"]
    new5h = out["new5bar_high"]
    new5l = out["new5bar_low"]
    p_str_bull = out.get("pat_strong_bull", pd.Series(False, index=out.index))
    p_val_bull = out.get("pat_valid_bull",  pd.Series(False, index=out.index))
    p_str_bear = out.get("pat_strong_bear", pd.Series(False, index=out.index))
    p_val_bear = out.get("pat_valid_bear",  pd.Series(False, index=out.index))

    # ── C1: ADX + DI Direction ────────────────────────────────────────────
    adx_range = np.maximum(1.0, 25.0 - minA)
    adx_ramp  = 10.0 + (adx - minA) * (10.0 / adx_range)   # 10→20 as ADX 20→25+
    adx_ramp  = adx_ramp.clip(upper=20.0)

    c1_bull = np.where((adx >= minA) & (dip > dim),
                       np.where(adx > 25.0, 20.0, adx_ramp), 0.0)
    c1_bear = np.where((adx >= minA) & (dim > dip),
                       np.where(adx > 25.0, 20.0, adx_ramp), 0.0)

    # ── C2: MACD Line + Histogram Acceleration ────────────────────────────
    c2_bull = np.where(macdL > macdS, np.where(maccB, 18.0, 10.0), 0.0)
    c2_bear = np.where(macdL < macdS, np.where(maccR, 18.0, 10.0), 0.0)

    # ── C3: VWAP Deviation ────────────────────────────────────────────────
    c3_bull = np.where(close > vwap, np.where(vdev > 0.003, 14.0, 7.0), 0.0)
    c3_bear = np.where(close < vwap, np.where(vdev > 0.003, 14.0, 7.0), 0.0)

    # ── C4: EMA Stack Alignment ───────────────────────────────────────────
    c4_bull = np.where((ema9 > ema21) & (ema21 > ema50), 14.0,
               np.where(ema9 > ema21, 7.0, 0.0))
    c4_bear = np.where((ema9 < ema21) & (ema21 < ema50), 14.0,
               np.where(ema9 < ema21, 7.0, 0.0))

    # ── C5: Volume × Body Position ───────────────────────────────────────
    c5_bull = np.where(vol_s & (bp > 0.6), 12.0,
               np.where(vol_a & (bp > 0.5),  6.0, 0.0))
    c5_bear = np.where(vol_s & (bp < 0.4), 12.0,
               np.where(vol_a & (bp < 0.5),  6.0, 0.0))

    # ── C6: RSI Distance from 50 ─────────────────────────────────────────
    c6_bull = np.clip((rsi - 50.0) * 0.5, 0.0, 10.0)
    c6_bear = np.clip((50.0 - rsi) * 0.5, 0.0, 10.0)

    # ── C7: 15m HTF EMA Alignment ────────────────────────────────────────
    if config.use_htf:
        c7_bull = np.where((e9_15 > e21_15) & (slope15 > 0.0), 8.0, 0.0)
        c7_bear = np.where((e9_15 < e21_15) & (slope15 < 0.0), 8.0, 0.0)
    else:
        c7_bull = np.full(len(out), 4.0)   # give base 4 pts if HTF disabled
        c7_bear = np.full(len(out), 4.0)

    # ── C8: 5m RSI Side ──────────────────────────────────────────────────
    c8_bull = np.where(rsi5 > 50.0, 4.0, 0.0)
    c8_bear = np.where(rsi5 < 50.0, 4.0, 0.0)

    # ── C9: New 5-bar High / Low ─────────────────────────────────────────
    c9_bull = np.where(new5h, 4.0, 0.0)
    c9_bear = np.where(new5l, 4.0, 0.0)

    # ── C10: Candlestick Pattern Bonus ───────────────────────────────────
    if config.use_pat_score:
        c10_bull = np.where(p_str_bull, 15.0, np.where(p_val_bull, 10.0, 0.0))
        c10_bear = np.where(p_str_bear, 15.0, np.where(p_val_bear, 10.0, 0.0))
    else:
        c10_bull = np.zeros(len(out))
        c10_bear = np.zeros(len(out))

    # ── Total Score ───────────────────────────────────────────────────────
    bull_raw = (c1_bull + c2_bull + c3_bull + c4_bull + c5_bull
                + c6_bull + c7_bull + c8_bull + c9_bull + c10_bull)
    bear_raw = (c1_bear + c2_bear + c3_bear + c4_bear + c5_bear
                + c6_bear + c7_bear + c8_bear + c9_bear + c10_bear)

    out["bull_score"] = np.clip(bull_raw, 0.0, 100.0)
    out["bear_score"] = np.clip(bear_raw, 0.0, 100.0)
    out["score_diff"] = out["bull_score"] - out["bear_score"]

    # ── Bias Text (mirrors Pine dashboard label) ──────────────────────────
    out["bias_text"] = np.select(
        [
            out["score_diff"] >= 40.0,
            out["score_diff"] <= -40.0,
            out["score_diff"] > 0.0,
            out["score_diff"] < 0.0,
        ],
        ["STR BULL", "STR BEAR", "MILD BULL", "MILD BEAR"],
        default="NEUTRAL",
    )

    # Individual component scores for debugging / dashboard
    out["c1_bull"] = c1_bull; out["c1_bear"] = c1_bear
    out["c2_bull"] = c2_bull; out["c2_bear"] = c2_bear
    out["c3_bull"] = c3_bull; out["c3_bear"] = c3_bear
    out["c4_bull"] = c4_bull; out["c4_bear"] = c4_bear
    out["c5_bull"] = c5_bull; out["c5_bear"] = c5_bear
    out["c6_bull"] = c6_bull; out["c6_bear"] = c6_bear
    out["c7_bull"] = c7_bull; out["c7_bear"] = c7_bear
    out["c8_bull"] = c8_bull; out["c8_bear"] = c8_bear
    out["c9_bull"] = c9_bull; out["c9_bear"] = c9_bear
    out["c10_bull"]= c10_bull; out["c10_bear"]= c10_bear

    return out
