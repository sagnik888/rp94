"""
apex_emacross/indicators.py
Core technical indicator computations for APEX Hybrid Pro v5.5.

Mirrors Pine Script Sections 3 / 3b / 3c exactly:
  • EMA 5/9/13/21/50/200  (standard EMA, alpha = 2/(n+1))
  • VWAP(hlc3)            (daily session reset)
  • ATR(14)               (Wilder RMA, alpha = 1/n)
  • RSI(14)               (Wilder RMA)
  • MACD(12,26,9)
  • DMI/ADX(14,14)        (Wilder RMA)
  • Volume analytics
  • Multi-timeframe: 5m RSI, 15m EMA9/21
  • Candle-structure fields for bars 0, 1, 2
"""

import numpy as np
import pandas as pd
from typing import Optional, Tuple


# ═══════════════════════════════════════════════════════════════════════════
# LOW-LEVEL MATH HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def rma(series: pd.Series, length: int) -> pd.Series:
    """
    Wilder's Moving Average  (Pine: ta.rma / RMA).
    alpha = 1 / length   (≠ EMA's 2/(length+1))
    Used internally by ta.atr, ta.rsi, ta.dmi in Pine.
    """
    return series.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()


def crossover(a: pd.Series, b: pd.Series) -> pd.Series:
    """True where a crosses ABOVE b  (Pine: ta.crossover)."""
    return ((a > b) & (a.shift(1) <= b.shift(1))).fillna(False)


def crossunder(a: pd.Series, b: pd.Series) -> pd.Series:
    """True where a crosses BELOW b  (Pine: ta.crossunder)."""
    return ((a < b) & (a.shift(1) >= b.shift(1))).fillna(False)


# ═══════════════════════════════════════════════════════════════════════════
# INDICATOR FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def compute_emas(close: pd.Series) -> dict:
    """
    All six EMAs used by APEX v5.5  (Pine Section 3).
    Returns dict: {ema5, ema9, ema13, ema21, ema50, ema200}
    """
    spans = {"ema5": 5, "ema9": 9, "ema13": 13,
             "ema21": 21, "ema50": 50, "ema200": 200}
    return {k: close.ewm(span=v, adjust=False).mean() for k, v in spans.items()}


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Session VWAP using HLC3 source with daily reset.
    Pine: ta.vwap(hlc3) — resets at each new NSE session.
    Requires DatetimeIndex.  Falls back to cumulative VWAP on failure.
    """
    hlc3 = (df["high"] + df["low"] + df["close"]) / 3.0
    try:
        # Group by calendar date and compute cumulative sum within each day
        dates = df.index.date
        cum_tp_vol = (hlc3 * df["volume"]).groupby(dates).cumsum()
        cum_vol    = df["volume"].groupby(dates).cumsum()
        return (cum_tp_vol / cum_vol.replace(0.0, np.nan)).ffill()
    except Exception:
        return (hlc3 * df["volume"]).cumsum() / df["volume"].cumsum()


def compute_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """
    ATR with Wilder's smoothing  (Pine: ta.atr(14)).
    True Range = max(H-L, |H-C[1]|, |L-C[1]|)
    """
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return rma(tr, length)


def compute_rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """RSI with Wilder's smoothing  (Pine: ta.rsi(close, 14))."""
    delta     = close.diff()
    avg_gain  = rma(delta.clip(lower=0.0), length)
    avg_loss  = rma((-delta).clip(lower=0.0), length)
    rs        = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def compute_macd(
    close: pd.Series,
    fast: int = 12, slow: int = 26, signal_period: int = 9,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    MACD line, signal line, histogram  (Pine: ta.macd(close,12,26,9)).
    Returns: (macd_line, macd_signal, macd_hist)
    """
    macd_line   = (close.ewm(span=fast,  adjust=False).mean() -
                   close.ewm(span=slow,  adjust=False).mean())
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line


def compute_dmi_adx(
    df: pd.DataFrame,
    di_length: int = 14, adx_length: int = 14,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Directional Movement Index + ADX  (Pine: ta.dmi(14,14)).
    Returns: (di_plus, di_minus, adx)
    """
    h, l, pc  = df["high"], df["low"], df["close"].shift(1)
    tr        = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    up        = h.diff()
    dn        = -(l.diff())
    dm_plus   = up.where((up > dn)  & (up  > 0), 0.0)
    dm_minus  = dn.where((dn > up)  & (dn  > 0), 0.0)

    tr_s  = rma(tr,       di_length)
    dmp_s = rma(dm_plus,  di_length)
    dmm_s = rma(dm_minus, di_length)

    di_plus  = (100.0 * dmp_s / tr_s.replace(0.0, np.nan)).fillna(0.0)
    di_minus = (100.0 * dmm_s / tr_s.replace(0.0, np.nan)).fillna(0.0)
    dx       = (100.0 * (di_plus - di_minus).abs() /
                (di_plus + di_minus).replace(0.0, np.nan)).fillna(0.0)
    adx      = rma(dx, adx_length)
    return di_plus, di_minus, adx


# ═══════════════════════════════════════════════════════════════════════════
# MULTI-TIMEFRAME HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def resample_ohlcv(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Resample minute OHLCV to a higher timeframe (e.g. 5m → 15m)."""
    rule = f"{minutes}min"
    return (
        df.resample(rule, closed="right", label="right")
          .agg({"open": "first", "high": "max",
                "low": "min",   "close": "last", "volume": "sum"})
          .dropna(subset=["close"])
    )


def _map_htf_to_base(htf: pd.Series, base_idx: pd.DatetimeIndex) -> pd.Series:
    """
    Maps a higher-timeframe series back to the base index.
    Equivalent to Pine's request.security with lookahead_on + [1] offset:
      • shift(1)  → use previous COMPLETED HTF bar  (no lookahead)
      • ffill()   → propagate value until next HTF bar closes
    """
    return htf.shift(1).reindex(base_idx, method="ffill")


def get_mtf_rsi_5m(
    df_base: pd.DataFrame,
    df_5m: Optional[pd.DataFrame] = None,
) -> pd.Series:
    """
    5-minute RSI(14) mapped to base timeframe.
    Pine: request.security(ticker,"5", ta.rsi(close[1],14), lookahead_on)
    """
    target = df_5m if df_5m is not None else resample_ohlcv(df_base, 5)
    return _map_htf_to_base(compute_rsi(target["close"], 14), df_base.index).fillna(50.0)


def get_mtf_ema_15m(
    df_base: pd.DataFrame,
    df_15m: Optional[pd.DataFrame] = None,
) -> Tuple[pd.Series, pd.Series]:
    """
    15-minute EMA9 and EMA21 mapped to base timeframe.
    Pine: request.security(ticker,"15", ta.ema(close[1],9/21), lookahead_on)
    Returns: (ema9_15, ema21_15)
    """
    target = df_15m if df_15m is not None else resample_ohlcv(df_base, 15)
    c15    = target["close"]
    ema9   = _map_htf_to_base(c15.ewm(span=9,  adjust=False).mean(), df_base.index).ffill()
    ema21  = _map_htf_to_base(c15.ewm(span=21, adjust=False).mean(), df_base.index).ffill()
    return ema9, ema21


# ═══════════════════════════════════════════════════════════════════════════
# MAIN FUNCTION: compute_all_indicators
# ═══════════════════════════════════════════════════════════════════════════

def compute_all_indicators(
    df: pd.DataFrame,
    df_5m: Optional[pd.DataFrame] = None,
    df_15m: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Compute ALL APEX v5.5 indicators on an OHLCV DataFrame.

    Args:
        df:    OHLCV DataFrame with columns [open, high, low, close, volume]
               and a DatetimeIndex (IST tz recommended for session filter).
        df_5m:  Optional pre-fetched 5m OHLCV  (for MTF RSI).
        df_15m: Optional pre-fetched 15m OHLCV (for HTF EMA gate).

    Returns:
        New DataFrame with ALL indicator columns added (does not modify input).
    """
    out = df.copy()
    o, h, l, c, v = out["open"], out["high"], out["low"], out["close"], out["volume"]

    # ── EMAs ─────────────────────────────────────────────────────────────
    emas = compute_emas(c)
    out.update(pd.DataFrame(emas, index=out.index))   # inplace update
    for k, s in emas.items():
        out[k] = s

    # ── VWAP ─────────────────────────────────────────────────────────────
    out["vwap"] = compute_vwap(out)

    # ── ATR & Volatility Regime ───────────────────────────────────────────
    out["atr"]     = compute_atr(out, 14)
    out["atr_ma"]  = out["atr"].rolling(50).mean()
    out["atr_exp"] = (out["atr"] / out["atr_ma"].replace(0.0, np.nan)).fillna(1.0)
    out["high_vol"]   = out["atr_exp"] > 1.25
    out["normal_vol"] = (out["atr_exp"] >= 0.85) & (out["atr_exp"] <= 1.25)
    out["low_vol"]    = out["atr_exp"] < 0.85
    out["vol_regime"] = np.select(
        [out["high_vol"], out["low_vol"]],
        ["HIGH",          "LOW"],
        default="NORMAL",
    )

    # ── RSI ──────────────────────────────────────────────────────────────
    out["rsi"] = compute_rsi(c, 14)

    # ── MACD ─────────────────────────────────────────────────────────────
    out["macd_line"], out["macd_signal"], out["macd_hist"] = compute_macd(c, 12, 26, 9)
    out["macd_acc_bull"] = (out["macd_hist"] > out["macd_hist"].shift(1)).fillna(False)
    out["macd_acc_bear"] = (out["macd_hist"] < out["macd_hist"].shift(1)).fillna(False)

    # ── DMI / ADX ─────────────────────────────────────────────────────────
    out["di_plus"], out["di_minus"], out["adx"] = compute_dmi_adx(out, 14, 14)

    # ── Volume Analytics ──────────────────────────────────────────────────
    out["vol_sma"]  = v.rolling(20).mean()
    out["rel_vol"]  = (v / out["vol_sma"].replace(0.0, np.nan)).fillna(1.0)
    out["vol_spike"] = (v > out["vol_sma"] * 1.5).fillna(False)
    out["vol_above"] = (out["rel_vol"] > 1.2).fillna(False)

    # Body percent  →  (close - low) / (high - low)
    rng = h - l
    out["body_pct"] = np.where(rng > 0.0, (c - l) / rng, 0.5)

    # ── Trend Conditions ─────────────────────────────────────────────────
    out["bull_trend"] = (c > out["ema50"]) & (out["ema50"] > out["ema200"]) & (out["adx"] > 20.0)
    out["bear_trend"] = (c < out["ema50"]) & (out["ema50"] < out["ema200"]) & (out["adx"] > 20.0)
    out["is_trending"] = out["adx"] > 20.0

    # ── Multi-Timeframe ───────────────────────────────────────────────────
    out["rsi_5m"]        = get_mtf_rsi_5m(df, df_5m)
    ema9_15, ema21_15    = get_mtf_ema_15m(df, df_15m)
    out["ema9_15"]       = ema9_15
    out["ema21_15"]      = ema21_15
    out["ema9_15_slope"] = ema9_15 - ema9_15.shift(3)   # slope over 3 bars

    # ── Candle Structure  (bars 0 / 1 / 2 in Pine notation) ──────────────
    for lag, suffix in [(0, "0"), (1, "1"), (2, "2")]:
        _c = c.shift(lag); _o = o.shift(lag)
        _h = h.shift(lag); _l = l.shift(lag)
        _mx = pd.concat([_c, _o], axis=1).max(axis=1)
        _mn = pd.concat([_c, _o], axis=1).min(axis=1)
        out[f"body{suffix}"]   = (_c - _o).abs()
        out[f"range{suffix}"]  = _h - _l
        out[f"u_wick{suffix}"] = _h - _mx
        out[f"l_wick{suffix}"] = _mn - _l
        out[f"bull{suffix}"]   = (_c > _o).fillna(False)
        out[f"bear{suffix}"]   = (_c < _o).fillna(False)
        if suffix in ("0", "1"):
            _rng = out[f"range{suffix}"]
            _bdy = out[f"body{suffix}"]
            out[f"doji{suffix}"] = ((_rng > 0.0) & (_bdy <= _rng * 0.10)).fillna(False)

    # Pattern body-size thresholds
    out["p_min_body"] = out["atr"] * 0.15
    out["p_big_body"] = out["atr"] * 0.55

    # ── 5-bar New Highs / Lows ────────────────────────────────────────────
    out["new5bar_high"] = (c > h.rolling(5).max().shift(1)).fillna(False)
    out["new5bar_low"]  = (c < l.rolling(5).min().shift(1)).fillna(False)

    # ── VWAP Deviation ────────────────────────────────────────────────────
    out["vwap_dev"] = np.where(out["vwap"] > 0.0,
                               (c - out["vwap"]).abs() / out["vwap"], 0.0)

    # ── Swing High / Low (20-bar lookback) ───────────────────────────────
    out["sw_h20"] = h.rolling(20).max()
    out["sw_l20"] = l.rolling(20).min()

    return out
