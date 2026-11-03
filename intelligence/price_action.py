"""Structural price-action analyzer — SEPARATE, TOGGLEABLE PATCH.

Adds the structural price-action layer the indicator stack lacks:
  1. Horizontal support/resistance from swing-pivot clusters
  2. Swing market structure (HH/HL=up, LH/LL=down, else range)
  3. Candlestick patterns (engulfing, pin/hammer/shooting-star, inside bar, doji)
  4. Volume profile (POC + value-area high/low)
  5. Premium/Discount zones — penalise chasing the extreme of the swing range
  6. Liquidity pools (equal highs/lows) — penalise entering into a stop-hunt magnet

Swings use a confirmed fractal strength of DEFAULT_SWING_STRENGTH (was a noisy 2).
Items 5-6 are pure CONTEXT FILTERS on the score, never entry triggers.

Design: PURE. Takes an OHLCV DataFrame, returns a context dict with a
direction-aware score in [-1, 1] and a verdict. No imports from the rest of the
system, so the whole feature can be removed by deleting this file, the gated
scanner hook, and the settings flags. Default-OFF until A/B-proven on P&L.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ── primitives ────────────────────────────────────────────────────────────
# Swing strength = fractal half-width. A pivot must be the extreme versus this
# many bars on each side. Bumped from the original noisy 2 to 5 ("internal"
# scale, à la LuxAlgo) so structure/S-R read confirmed swings, not every wiggle.
DEFAULT_SWING_STRENGTH = 5


def swing_points(highs: np.ndarray, lows: np.ndarray,
                 left: int = DEFAULT_SWING_STRENGTH, right: int = DEFAULT_SWING_STRENGTH
                 ) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Fractal pivots: a swing high/low is an extreme vs `left`/`right` neighbours.

    Larger left/right => fewer, stronger, less-noisy pivots (confirmed `right`
    bars later, so look-ahead-safe on a df already sliced to the signal bar)."""
    sh: List[Tuple[int, float]] = []
    sl: List[Tuple[int, float]] = []
    n = len(highs)
    left = max(1, int(left))
    right = max(1, int(right))
    for i in range(left, n - right):
        if all(highs[i] >= highs[i - k] for k in range(1, left + 1)) and \
           all(highs[i] >= highs[i + k] for k in range(1, right + 1)):
            sh.append((i, float(highs[i])))
        if all(lows[i] <= lows[i - k] for k in range(1, left + 1)) and \
           all(lows[i] <= lows[i + k] for k in range(1, right + 1)):
            sl.append((i, float(lows[i])))
    return sh, sl


def support_resistance(highs: np.ndarray, lows: np.ndarray, tol_pct: float = 0.0015,
                       max_levels: int = 8, strength: int = DEFAULT_SWING_STRENGTH
                       ) -> List[Tuple[float, int]]:
    """Cluster swing pivots into horizontal levels. Returns [(price, strength)]."""
    sh, sl = swing_points(highs, lows, strength, strength)
    pts = sorted(p for _, p in sh) + sorted(p for _, p in sl)
    pts.sort()
    if not pts:
        return []
    levels: List[Tuple[float, int]] = []
    cluster = [pts[0]]
    for p in pts[1:]:
        if abs(p - cluster[-1]) / max(1e-9, cluster[-1]) <= tol_pct:
            cluster.append(p)
        else:
            levels.append((sum(cluster) / len(cluster), len(cluster)))
            cluster = [p]
    levels.append((sum(cluster) / len(cluster), len(cluster)))
    levels.sort(key=lambda x: x[1], reverse=True)  # strongest (most touches) first
    return levels[:max_levels]


def swing_structure(highs: np.ndarray, lows: np.ndarray,
                    strength: int = DEFAULT_SWING_STRENGTH) -> str:
    """HH+HL -> UP, LH+LL -> DOWN, else RANGE."""
    sh, sl = swing_points(highs, lows, strength, strength)
    if len(sh) < 2 or len(sl) < 2:
        return "RANGE"
    hh = sh[-1][1] > sh[-2][1]
    hl = sl[-1][1] > sl[-2][1]
    lh = sh[-1][1] < sh[-2][1]
    ll = sl[-1][1] < sl[-2][1]
    if hh and hl:
        return "UP"
    if lh and ll:
        return "DOWN"
    return "RANGE"


def premium_discount(highs: np.ndarray, lows: np.ndarray, close: float,
                     lookback: int = 50, premium_threshold: float = 0.70
                     ) -> Tuple[str, float]:
    """Where does `close` sit inside the recent swing range?

    Returns (zone, position) where position in [0,1] is close's location between
    the range low (0) and high (1). PREMIUM = top of range (don't chase longs),
    DISCOUNT = bottom (don't chase shorts), else EQUILIBRIUM. Mirrors the
    LuxAlgo premium/discount idea using a rolling swing window."""
    n = len(highs)
    if n < 5:
        return "EQUILIBRIUM", 0.5
    window = min(int(lookback), n)
    hi = float(np.nanmax(highs[-window:]))
    lo = float(np.nanmin(lows[-window:]))
    if not np.isfinite(hi) or not np.isfinite(lo) or hi <= lo:
        return "EQUILIBRIUM", 0.5
    pos = (float(close) - lo) / (hi - lo)
    pos = max(0.0, min(1.0, pos))
    disc_threshold = 1.0 - float(premium_threshold)
    if pos >= premium_threshold:
        return "PREMIUM", pos
    if pos <= disc_threshold:
        return "DISCOUNT", pos
    return "EQUILIBRIUM", pos


def liquidity_pools(highs: np.ndarray, lows: np.ndarray, close: float,
                    strength: int = DEFAULT_SWING_STRENGTH, tol_pct: float = 0.0010,
                    ) -> Tuple[Optional[float], Optional[float]]:
    """Equal highs above / equal lows below = resting liquidity (stop-hunt magnets).

    Returns (nearest_equal_high_above, nearest_equal_low_below). A cluster of >=2
    near-equal swing highs above price is a liquidity pool a move is likely to
    sweep before reversing — so a long INTO it is lower quality (and vice-versa)."""
    sh, sl = swing_points(highs, lows, strength, strength)

    def _nearest_cluster(levels: List[float]) -> Optional[float]:
        for i in range(len(levels)):
            cluster = [levels[i]]
            for j in range(i + 1, len(levels)):
                if abs(levels[j] - cluster[-1]) / max(1e-9, cluster[-1]) <= tol_pct:
                    cluster.append(levels[j])
                else:
                    break
            if len(cluster) >= 2:
                return sum(cluster) / len(cluster)
        return None

    highs_above = sorted(p for _, p in sh if p > close)            # ascending -> nearest first
    lows_below = sorted((p for _, p in sl if p < close), reverse=True)  # descending -> nearest first
    return _nearest_cluster(highs_above), _nearest_cluster(lows_below)


def candle_pattern(o, h, l, c, po, ph, pl, pc) -> Tuple[str, str]:
    """Last candle (o,h,l,c) vs prior (po,ph,pl,pc) -> (name, bias)."""
    body = abs(c - o)
    rng = max(h - l, 1e-9)
    upper = h - max(o, c)
    lower = min(o, c) - l
    if c > o and pc < po and c >= po and o <= pc:
        return ("BULL_ENGULF", "BULL")
    if c < o and pc > po and c <= po and o >= pc:
        return ("BEAR_ENGULF", "BEAR")
    if body > 0 and lower >= body * 2 and upper <= body * 0.8:
        return ("HAMMER", "BULL")
    if body > 0 and upper >= body * 2 and lower <= body * 0.8:
        return ("SHOOTING_STAR", "BEAR")
    if h <= ph and l >= pl:
        return ("INSIDE_BAR", "NEUTRAL")
    if body <= rng * 0.1:
        return ("DOJI", "NEUTRAL")
    return ("NONE", "NEUTRAL")


def volume_profile(
    closes: np.ndarray,
    vols: np.ndarray,
    bins: int = 24,
    *,
    highs: Optional[np.ndarray] = None,
    lows: Optional[np.ndarray] = None,
    value_area_pct: float = 0.70,
) -> Optional[Dict[str, float]]:
    """Volume-by-price: POC + value area.

    v1 assigned all candle volume to close. v2 distributes each candle's volume
    across every touched price bin, so wide acceptance/rejection bars move the
    profile where the auction actually traded. The old close-only call signature
    still works for compatibility.
    """
    if len(closes) < 5 or float(np.nansum(vols)) <= 0:
        return None
    close_arr = np.asarray(closes, dtype=float)
    vol_arr = np.asarray(vols, dtype=float)
    high_arr = np.asarray(highs, dtype=float) if highs is not None else close_arr
    low_arr = np.asarray(lows, dtype=float) if lows is not None else close_arr
    lo = float(np.nanmin(np.minimum(low_arr, close_arr)))
    hi = float(np.nanmax(np.maximum(high_arr, close_arr)))
    if hi <= lo:
        return None
    bins = max(8, int(bins or 24))
    value_area_pct = max(0.05, min(0.95, float(value_area_pct or 0.70)))
    edges = np.linspace(lo, hi, bins + 1)
    vol_bin = np.zeros(bins)
    for close, high, low, vol in zip(close_arr, high_arr, low_arr, vol_arr):
        if not np.isfinite(vol) or vol <= 0:
            continue
        if not np.isfinite(high) or not np.isfinite(low) or high <= low:
            idx = int(np.clip(np.digitize(close, edges) - 1, 0, bins - 1))
            vol_bin[idx] += float(vol)
            continue
        start = int(np.clip(np.searchsorted(edges, low, side="right") - 1, 0, bins - 1))
        end = int(np.clip(np.searchsorted(edges, high, side="left"), 0, bins - 1))
        if end < start:
            end = start
        touched = max(1, end - start + 1)
        vol_bin[start:end + 1] += float(vol) / touched
    if float(vol_bin.sum()) <= 0:
        return None
    poc_i = int(np.argmax(vol_bin))
    poc = (edges[poc_i] + edges[poc_i + 1]) / 2.0
    total = vol_bin.sum()
    target = total * value_area_pct
    lo_i = hi_i = poc_i
    acc = vol_bin[poc_i]
    while acc < target and (lo_i > 0 or hi_i < bins - 1):
        left = vol_bin[lo_i - 1] if lo_i > 0 else -1.0
        right = vol_bin[hi_i + 1] if hi_i < bins - 1 else -1.0
        if right >= left and hi_i < bins - 1:
            hi_i += 1
            acc += vol_bin[hi_i]
        elif lo_i > 0:
            lo_i -= 1
            acc += vol_bin[lo_i]
        else:
            break
    return {
        "poc": float(poc),
        "vah": float(edges[hi_i + 1]),
        "val": float(edges[lo_i]),
        "profile_low": float(edges[0]),
        "profile_high": float(edges[-1]),
        "bins": int(bins),
        "value_area_pct": round(value_area_pct, 3),
    }


def market_structure_profile(
    df: pd.DataFrame,
    *,
    lookback: int = 200,
    bins: int = 32,
    value_area_pct: float = 0.70,
    session_only: bool = True,
) -> Optional[Dict[str, Any]]:
    """Rolling/session market-structure profile with acceptance/rejection state."""
    if df is None or len(df) < 12:
        return None
    try:
        frame = df.copy()
        if session_only and isinstance(frame.index, pd.DatetimeIndex) and len(frame.index):
            last_date = frame.index[-1].date()
            session_frame = frame[frame.index.date == last_date]
            if len(session_frame) >= 12:
                frame = session_frame
        lookback = max(20, int(lookback or 200))
        frame = frame.tail(min(len(frame), lookback))
        close = frame["close"].astype(float).to_numpy()
        high = frame["high"].astype(float).to_numpy()
        low = frame["low"].astype(float).to_numpy()
        volume = (
            frame["volume"].astype(float).to_numpy()
            if "volume" in frame.columns
            else np.ones(len(frame), dtype=float)
        )
        profile = volume_profile(
            close,
            volume,
            bins=bins,
            highs=high,
            lows=low,
            value_area_pct=value_area_pct,
        )
        if not profile:
            return None
        last_close = float(close[-1])
        prev_close = float(close[-2]) if len(close) >= 2 else last_close
        last_high = float(high[-1])
        last_low = float(low[-1])
        poc = float(profile["poc"])
        vah = float(profile["vah"])
        val = float(profile["val"])

        state = "IN_VALUE_AREA"
        signal = "ROTATING"
        if last_close > vah:
            state = "ABOVE_VALUE"
            signal = "ACCEPTING_HIGHER" if prev_close > vah else "UPSIDE_ACCEPTANCE"
        elif last_close < val:
            state = "BELOW_VALUE"
            signal = "ACCEPTING_LOWER" if prev_close < val else "DOWNSIDE_ACCEPTANCE"
        elif last_high > vah and last_close <= vah:
            signal = "REJECTING_HIGHER"
        elif last_low < val and last_close >= val:
            signal = "REJECTING_LOWER"
        elif abs(last_close - poc) / max(1e-9, last_close) <= 0.0008:
            signal = "POC_MAGNET"

        width = max(1e-9, vah - val)
        position = (last_close - val) / width
        nearest_level = min(
            (("POC", poc), ("VAH", vah), ("VAL", val)),
            key=lambda item: abs(last_close - item[1]),
        )
        return {
            **profile,
            "state": state,
            "signal": signal,
            "close": round(last_close, 4),
            "distance_to_poc_pct": round((last_close - poc) / max(1e-9, last_close) * 100.0, 4),
            "distance_to_vah_pct": round((last_close - vah) / max(1e-9, last_close) * 100.0, 4),
            "distance_to_val_pct": round((last_close - val) / max(1e-9, last_close) * 100.0, 4),
            "value_position": round(max(-1.0, min(2.0, position)), 4),
            "nearest_level": nearest_level[0],
            "nearest_level_price": round(float(nearest_level[1]), 4),
            "lookback_bars": int(len(frame)),
            "session_only": bool(session_only),
        }
    except Exception:
        return None


def aroon_context(highs: np.ndarray, lows: np.ndarray, period: int = 25) -> Dict[str, Any]:
    """Aroon trend freshness/age context. Soft telemetry, not an entry trigger."""
    try:
        period = max(5, int(period or 25))
        highs = np.asarray(highs, dtype=float)
        lows = np.asarray(lows, dtype=float)
        if len(highs) < period or len(lows) < period:
            return {"available": False}
        window_h = highs[-period:]
        window_l = lows[-period:]
        since_high = (period - 1) - int(np.nanargmax(window_h))
        since_low = (period - 1) - int(np.nanargmin(window_l))
        up = 100.0 * (period - since_high) / period
        down = 100.0 * (period - since_low) / period
        osc = up - down
        trend = "UP" if osc >= 35 else ("DOWN" if osc <= -35 else "NEUTRAL")

        osc_series = []
        for idx in range(period - 1, len(highs)):
            wh = highs[idx - period + 1:idx + 1]
            wl = lows[idx - period + 1:idx + 1]
            sh = (period - 1) - int(np.nanargmax(wh))
            sl = (period - 1) - int(np.nanargmin(wl))
            osc_series.append(100.0 * (period - sh) / period - 100.0 * (period - sl) / period)
        age = 0
        if trend != "NEUTRAL":
            sign = 1 if trend == "UP" else -1
            for value in reversed(osc_series):
                if value * sign > 0:
                    age += 1
                else:
                    break
        freshness = "NEUTRAL"
        late_trend = False
        if trend != "NEUTRAL":
            if age <= 5:
                freshness = "FRESH"
            elif age <= 15:
                freshness = "DEVELOPING"
            else:
                freshness = "LATE"
                late_trend = True
        return {
            "available": True,
            "period": period,
            "aroon_up": round(up, 2),
            "aroon_down": round(down, 2),
            "oscillator": round(osc, 2),
            "trend": trend,
            "freshness": freshness,
            "trend_age_bars": int(age),
            "late_trend_caution": bool(late_trend),
        }
    except Exception:
        return {"available": False}


def wavetrend_context(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    *,
    n1: int = 10,
    n2: int = 21,
) -> Dict[str, Any]:
    """WaveTrend oscillator telemetry for dashboard-only exhaustion context."""
    try:
        h = pd.Series(np.asarray(highs, dtype=float))
        l = pd.Series(np.asarray(lows, dtype=float))
        c = pd.Series(np.asarray(closes, dtype=float))
        if len(c) < max(30, int(n1 or 10) + int(n2 or 21)):
            return {"available": False}
        ap = (h + l + c) / 3.0
        n1 = max(2, int(n1 or 10))
        n2 = max(3, int(n2 or 21))
        esa = ap.ewm(span=n1, adjust=False).mean()
        dev = (ap - esa).abs().ewm(span=n1, adjust=False).mean()
        ci = (ap - esa) / (0.015 * dev.replace(0.0, np.nan))
        wt1 = ci.ewm(span=n2, adjust=False).mean().fillna(0.0)
        wt2 = wt1.rolling(4).mean().fillna(0.0)
        value = float(wt1.iloc[-1])
        signal = float(wt2.iloc[-1])
        bias = "BULLISH" if value > signal else ("BEARISH" if value < signal else "NEUTRAL")
        exhaustion = "OVERBOUGHT" if value >= 60 else ("OVERSOLD" if value <= -60 else "NEUTRAL")
        return {
            "available": True,
            "wt1": round(value, 2),
            "wt2": round(signal, 2),
            "bias": bias,
            "exhaustion": exhaustion,
            "dashboard_only": True,
        }
    except Exception:
        return {"available": False}


class PriceActionAnalyzer:
    """Combine the primitives into a direction-aware verdict for a signal."""

    def analyze(self, df: pd.DataFrame, signal_type: str, *,
                near_level_pct: float = 0.0020,
                swing_strength: int = DEFAULT_SWING_STRENGTH,
                use_premium_discount: bool = True,
                use_liquidity: bool = True,
                premium_threshold: float = 0.70,
                pd_lookback: int = 50,
                liquidity_tol_pct: float = 0.0010,
                premium_trend_scale: float = 1.0,
                adverse_threshold: float = -0.40) -> Optional[Dict]:
        """premium_trend_scale scales ONLY the aligned-continuation chase penalty
        (LONG in premium / SELL in discount) — on a confirmed trend day the top
        (bottom) of the range is where continuation lives, so the caller may
        soften that specific penalty (2026-07-10: four costly blind blocks).
        Counter-trend penalties and all bonuses are never scaled."""
        if df is None or len(df) < 12:
            return None
        try:
            o = df["open"].astype(float).to_numpy()
            h = df["high"].astype(float).to_numpy()
            l = df["low"].astype(float).to_numpy()
            c = df["close"].astype(float).to_numpy()
            v = df["volume"].astype(float).to_numpy() if "volume" in df.columns else np.ones(len(c))
        except Exception:
            return None
        close = float(c[-1])
        if close <= 0:
            return None
        buy = str(signal_type).upper() in ("BUY", "LONG")

        levels = support_resistance(h, l, strength=swing_strength)
        res = sorted((p for p, _ in levels if p > close))
        sup = sorted((p for p, _ in levels if p < close), reverse=True)
        nearest_res = res[0] if res else None
        nearest_sup = sup[0] if sup else None
        structure = swing_structure(h, l, strength=swing_strength)
        pat, bias = candle_pattern(o[-1], h[-1], l[-1], c[-1], o[-2], h[-2], l[-2], c[-2])
        vprof = market_structure_profile(df)
        aroon = aroon_context(h, l)
        wavetrend = wavetrend_context(h, l, c)

        score = 0.0
        reasons: List[str] = []

        # 1. Swing structure alignment
        if buy:
            score += {"UP": 0.40, "RANGE": 0.0, "DOWN": -0.40}[structure]
        else:
            score += {"DOWN": 0.40, "RANGE": 0.0, "UP": -0.40}[structure]
        if structure != "RANGE":
            reasons.append(f"structure={structure}")

        # 2. Level proximity (the key S/R insight)
        near = float(near_level_pct)
        if nearest_res is not None and (nearest_res - close) / close <= near:
            if buy:
                score -= 0.40; reasons.append("LONG into resistance")
            else:
                score += 0.30; reasons.append("SELL rejecting resistance")
        if nearest_sup is not None and (close - nearest_sup) / close <= near:
            if buy:
                score += 0.30; reasons.append("LONG bouncing support")
            else:
                score -= 0.40; reasons.append("SELL into support")

        # 3. Candle pattern bias
        if bias != "NEUTRAL":
            if (buy and bias == "BULL") or (not buy and bias == "BEAR"):
                score += 0.30; reasons.append(f"{pat} (with)")
            else:
                score -= 0.30; reasons.append(f"{pat} (against)")

        # 4. Market-structure profile / POC + value-area acceptance
        if vprof is not None:
            profile_signal = str(vprof.get("signal") or "")
            if buy:
                score += 0.10 if close >= vprof["poc"] else -0.10
                if profile_signal in {"ACCEPTING_HIGHER", "UPSIDE_ACCEPTANCE"}:
                    score += 0.20; reasons.append("profile accepting higher")
                elif profile_signal == "REJECTING_HIGHER":
                    score -= 0.20; reasons.append("profile rejecting higher")
                elif profile_signal == "REJECTING_LOWER":
                    score += 0.12; reasons.append("profile rejecting lower")
                elif vprof.get("state") == "BELOW_VALUE":
                    score -= 0.12; reasons.append("below value area")
            else:
                score += 0.10 if close <= vprof["poc"] else -0.10
                if profile_signal in {"ACCEPTING_LOWER", "DOWNSIDE_ACCEPTANCE"}:
                    score += 0.20; reasons.append("profile accepting lower")
                elif profile_signal == "REJECTING_LOWER":
                    score -= 0.20; reasons.append("profile rejecting lower")
                elif profile_signal == "REJECTING_HIGHER":
                    score += 0.12; reasons.append("profile rejecting higher")
                elif vprof.get("state") == "ABOVE_VALUE":
                    score -= 0.12; reasons.append("above value area")

        # 4b. Aroon freshness: soft context only, with late-trend caution.
        if aroon.get("available"):
            ar_trend = str(aroon.get("trend") or "")
            aligned = (buy and ar_trend == "UP") or ((not buy) and ar_trend == "DOWN")
            opposed = (buy and ar_trend == "DOWN") or ((not buy) and ar_trend == "UP")
            if aligned:
                score += 0.08
                reasons.append(f"Aroon {ar_trend.lower()} {str(aroon.get('freshness') or '').lower()}")
                if aroon.get("late_trend_caution"):
                    score -= 0.08
                    reasons.append("late Aroon trend caution")
            elif opposed:
                score -= 0.08
                reasons.append(f"Aroon opposes ({ar_trend.lower()})")

        # 5. Premium/Discount — don't chase the extreme of the swing range.
        # On confirmed aligned trend days the caller passes premium_trend_scale
        # < 1.0 so continuation entries at the extreme are penalised less.
        pd_scale = max(0.0, min(1.0, float(premium_trend_scale or 1.0)))
        zone = "EQUILIBRIUM"
        if use_premium_discount:
            zone, _pos = premium_discount(h, l, close, lookback=pd_lookback,
                                          premium_threshold=premium_threshold)
            if zone == "PREMIUM":
                if buy:
                    score -= 0.30 * pd_scale
                    reasons.append(
                        "LONG in premium (top of range)"
                        + (" [trend-scaled]" if pd_scale < 1.0 else "")
                    )
                else:
                    score += 0.20; reasons.append("SELL in premium")
            elif zone == "DISCOUNT":
                if buy:
                    score += 0.20; reasons.append("LONG in discount")
                else:
                    score -= 0.30 * pd_scale
                    reasons.append(
                        "SELL in discount (bottom of range)"
                        + (" [trend-scaled]" if pd_scale < 1.0 else "")
                    )

        # 6. Liquidity pools — equal highs/lows just ahead are stop-hunt magnets.
        eqh, eql = (None, None)
        if use_liquidity:
            eqh, eql = liquidity_pools(h, l, close, strength=swing_strength,
                                       tol_pct=liquidity_tol_pct)
            near = float(near_level_pct)
            if buy and eqh is not None and (eqh - close) / close <= near:
                score -= 0.25; reasons.append("LONG into equal-highs liquidity")
            if (not buy) and eql is not None and (close - eql) / close <= near:
                score -= 0.25; reasons.append("SELL into equal-lows liquidity")

        score = max(-1.0, min(1.0, score))
        # The ADVERSE boundary was the literal -0.40 written here, while
        # ``price_action_adverse_threshold`` (default -0.50) was read only by the
        # exit-side loss-recovery checks. Operators reasonably read that setting as
        # governing this entry veto; it never did. It is a real parameter now, with the
        # SAME -0.40 default, so no verdict moves -- and the boundary that decided is
        # recorded in the result instead of being implied by a constant.
        adverse_at = float(adverse_threshold)
        verdict = "ADVERSE" if score <= adverse_at else ("FAVORABLE" if score >= 0.40 else "NEUTRAL")
        target_context = "profile target unavailable"
        trailing_context = "profile trailing neutral"
        if vprof:
            if buy:
                target_context = (
                    f"next structure {vprof.get('vah', 0):.2f} VAH / "
                    f"{vprof.get('poc', 0):.2f} POC"
                )
                trailing_context = (
                    "trail against VAL on acceptance"
                    if str(vprof.get("state")) == "ABOVE_VALUE"
                    else "watch POC/VAL loss"
                )
            else:
                target_context = (
                    f"next structure {vprof.get('val', 0):.2f} VAL / "
                    f"{vprof.get('poc', 0):.2f} POC"
                )
                trailing_context = (
                    "trail against VAH on acceptance"
                    if str(vprof.get("state")) == "BELOW_VALUE"
                    else "watch POC/VAH reclaim"
                )
        return {
            "score": round(score, 3),
            "verdict": verdict,
            "adverse_threshold": round(adverse_at, 4),
            "structure": structure,
            "pattern": pat,
            "nearest_resistance": nearest_res,
            "nearest_support": nearest_sup,
            "poc": vprof["poc"] if vprof else None,
            "vah": vprof["vah"] if vprof else None,
            "val": vprof["val"] if vprof else None,
            "market_structure_profile": vprof or {},
            "profile_signal": vprof.get("signal") if vprof else "",
            "target_context": target_context,
            "trailing_context": trailing_context,
            "aroon": aroon,
            "wavetrend": wavetrend,
            "zone": zone,
            "equal_highs": eqh,
            "equal_lows": eql,
            "reasons": reasons,
        }
