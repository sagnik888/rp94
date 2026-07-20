"""
MACD Histogram Momentum Filter  (STANDALONE — not wired into any engine yet)
============================================================================

Why this exists
---------------
The scanner engines (utbot / ema / consensus / auto / utbot_ema /
utbot_consensus / quad_union) are all *trend/flip* detectors — ATR-trailing stop, EMA
cross, Supertrend agreement. They answer "which way is price trending and did
it just flip?" but NOT "is that momentum *accelerating or fading?*". MACD
histogram encodes exactly that second dimension.

This module ports the ChrisMoody "CM_MacD_Ult_MTF" 4-state histogram colouring
into a momentum *confluence verdict* a caller can use to CONFIRM / treat with
CAUTION / VETO a trend-engine signal — especially to reject:
  * entries into FADING momentum (e.g. a SELL while down-momentum is decelerating
    — the classic exhaustion entry that gets stopped on the bounce), and
  * CHOP (histogram hugging the zero line and flipping sign repeatedly).

Design constraints honoured
---------------------------
* MACD is lagging and (per regime_detector's own note) jittery — so this is a
  *confluence/veto* layer, never a standalone signal source, and it is NOT
  imported by any engine. Wire it in later behind a default-OFF setting after
  shadow-validation.
* Signal line defaults to EMA (matches intelligence/regime_detector and standard
  MACD). ChrisMoody's script used sma(macd, signal); pass signal_ma="sma" to
  reproduce it exactly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

# 4 histogram states (== ChrisMoody's 4 colours)
BULL_ACCEL = "BULL_ACCEL"   # hist > 0 and rising   (aqua)   — bullish momentum building
BULL_FADE = "BULL_FADE"     # hist > 0 and falling  (blue)   — bullish momentum fading
BEAR_ACCEL = "BEAR_ACCEL"   # hist <= 0 and falling (red)    — bearish momentum building
BEAR_FADE = "BEAR_FADE"     # hist <= 0 and rising  (maroon) — bearish momentum fading

CONFIRM = "CONFIRM"
CAUTION = "CAUTION"
VETO = "VETO"


@dataclass(frozen=True)
class MacdMomentum:
    macd: float
    signal: float
    hist: float
    hist_prev: float
    state: str
    is_chop: bool
    verdict: str            # set by evaluate(signal_type); "" for the raw read
    reason: str = ""


def compute_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    signal_ma: str = "ema",
) -> pd.DataFrame:
    """Return a DataFrame with macd, signal, hist columns (EMA MACD by default)."""
    close = pd.Series(close).astype(float)
    macd_line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    if str(signal_ma).lower() == "sma":
        signal_line = macd_line.rolling(signal).mean()
    else:
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})


def classify_state(hist_now: float, hist_prev: float) -> str:
    """ChrisMoody 4-state colouring."""
    rising = hist_now > hist_prev
    if hist_now > 0:
        return BULL_ACCEL if rising else BULL_FADE
    return BEAR_FADE if rising else BEAR_ACCEL


def _is_chop(hist_series: pd.Series, lookback: int, flip_tol: int) -> bool:
    """Chop = histogram repeatedly crossing zero within `lookback` bars."""
    tail = hist_series.dropna().tail(max(2, lookback)).values
    if len(tail) < 3:
        return False
    signs = np.sign(tail)
    flips = int(np.sum(signs[1:] != signs[:-1]))
    return flips >= flip_tol


def evaluate(
    close: pd.Series,
    signal_type: str,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    signal_ma: str = "ema",
    chop_lookback: int = 6,
    chop_flip_tol: int = 3,
) -> Optional[MacdMomentum]:
    """Produce a momentum confluence verdict for a BUY/SELL trend-engine signal.

    Returns None if there is not enough data. Verdict semantics:
      CONFIRM  — histogram accelerating WITH the signal direction.
      CAUTION  — momentum fading, early-turn, or chop: size down / require extra confluence.
      VETO     — histogram accelerating AGAINST the signal direction.
    """
    df = compute_macd(close, fast, slow, signal, signal_ma)
    hist = df["hist"]
    if hist.dropna().shape[0] < 2:
        return None
    hist_now = float(hist.iloc[-1])
    hist_prev = float(hist.iloc[-2])
    if not (np.isfinite(hist_now) and np.isfinite(hist_prev)):
        return None
    state = classify_state(hist_now, hist_prev)
    chop = _is_chop(hist, chop_lookback, chop_flip_tol)
    side = str(signal_type or "").upper()

    if chop:
        verdict, reason = CAUTION, f"MACD-hist chop (>= {chop_flip_tol} zero-flips/{chop_lookback} bars)"
    elif side in ("BUY", "LONG"):
        verdict, reason = {
            BULL_ACCEL: (CONFIRM, "bullish momentum accelerating"),
            BULL_FADE: (CAUTION, "bullish momentum fading"),
            BEAR_FADE: (CAUTION, "below zero but turning up (early/unconfirmed)"),
            BEAR_ACCEL: (VETO, "bearish momentum accelerating against BUY"),
        }[state]
    elif side in ("SELL", "SHORT"):
        verdict, reason = {
            BEAR_ACCEL: (CONFIRM, "bearish momentum accelerating"),
            BEAR_FADE: (CAUTION, "down-momentum fading (exhaustion risk)"),
            BULL_FADE: (CAUTION, "above zero but turning down (early/unconfirmed)"),
            BULL_ACCEL: (VETO, "bullish momentum accelerating against SELL"),
        }[state]
    else:
        verdict, reason = CAUTION, "unknown signal_type"

    return MacdMomentum(
        macd=round(float(df["macd"].iloc[-1]), 4),
        signal=round(float(df["signal"].iloc[-1]), 4),
        hist=round(hist_now, 4),
        hist_prev=round(hist_prev, 4),
        state=state,
        is_chop=chop,
        verdict=verdict,
        reason=reason,
    )
