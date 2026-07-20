"""APEX Hybrid Pro v5.5 signal adapter for the UT1 scanner.

The provided APEX Python package mirrors the indicator, pattern, confluence,
session, and risk math from the Pine script. Its bundled trade manager is an
older EMA-cross entry variant, so this adapter uses the uploaded Pine v5.5
five-gate entry rules directly and emits UTBotSignal-compatible Lane B signals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import math
import pandas as pd

from engine.apex55.config import ApexConfig
from engine.apex55.confluence import compute_confluence
from engine.apex55.indicators import compute_all_indicators
from engine.apex55.patterns import compute_patterns
from engine.apex55.risk import calc_sl
from engine.apex55.session import compute_session_mask
from engine.ut_bot_core import UTBotSignal


@dataclass(frozen=True)
class ApexSignalContext:
    bull_score: float
    bear_score: float
    score_diff: float
    setup: str
    vol_regime: str
    body_pct: float
    rel_vol: float
    sl_mode: str


def _instrument_label(instrument: str) -> str:
    inst = str(instrument or "").upper()
    if inst == "BANKNIFTY":
        return "Bank Nifty"
    if inst == "MIDCPNIFTY":
        return "Midcap"
    if inst == "SENSEX":
        return "Sensex"
    if inst == "NIFTY":
        return "Nifty 50"
    return "Index"


def _apex_config(instrument: str) -> ApexConfig:
    cfg = ApexConfig()
    cfg.instrument_type = _instrument_label(instrument)
    cfg.detected_instrument = cfg.instrument_type
    cfg.min_score = 70
    cfg.conflict_margin = 20
    cfg.min_adx = 20.0
    cfg.use_htf = True
    cfg.signal_cooldown = 5
    return cfg


def _normalize_ohlcv(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df is None or len(df) == 0:
        return None
    required = {"open", "high", "low", "close"}
    if not required.issubset(set(df.columns)):
        return None
    out = df.copy()
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out = out[["open", "high", "low", "close", "volume"]].copy()
    for col in out.columns:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
    if out.empty:
        return None
    try:
        out = out.sort_index()
    except Exception:
        pass
    return out


def _is_finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _bool(value: object) -> bool:
    try:
        if value is pd.NA:
            return False
    except Exception:
        pass
    return bool(value)


def _cross_up(curr_a: float, curr_b: float, prev_a: float, prev_b: float) -> bool:
    return curr_a > curr_b and prev_a <= prev_b


def _cross_down(curr_a: float, curr_b: float, prev_a: float, prev_b: float) -> bool:
    return curr_a < curr_b and prev_a >= prev_b


def _setup_reason(row: pd.Series, prev: Optional[pd.Series], is_buy: bool) -> str:
    close = float(row.get("close", 0.0) or 0.0)
    open_px = float(row.get("open", close) or close)
    high = float(row.get("high", close) or close)
    low = float(row.get("low", close) or close)
    ema21 = float(row.get("ema21", close) or close)
    ema50 = float(row.get("ema50", close) or close)
    rsi = float(row.get("rsi", 50.0) or 50.0)
    macd = float(row.get("macd_line", 0.0) or 0.0)
    macd_sig = float(row.get("macd_signal", 0.0) or 0.0)
    vol_spike = _bool(row.get("vol_spike", False))
    bull_trend = _bool(row.get("bull_trend", False))
    bear_trend = _bool(row.get("bear_trend", False))
    sw_h20 = float(row.get("sw_h20", high) or high)
    sw_l20 = float(row.get("sw_l20", low) or low)
    prev_high = float(prev.get("high", high) if prev is not None else high)
    prev_low = float(prev.get("low", low) if prev is not None else low)
    prev_macd = float(prev.get("macd_line", macd) if prev is not None else macd)
    prev_macd_sig = float(prev.get("macd_signal", macd_sig) if prev is not None else macd_sig)

    is_breakout = high > sw_h20 and vol_spike and close > open_px
    is_breakdown = low < sw_l20 and vol_spike and close < open_px
    is_pull_buy = bull_trend and close < ema21 and close > ema50 and 40.0 < rsi < 60.0
    is_pull_sell = bear_trend and close > ema21 and close < ema50 and 40.0 < rsi < 60.0
    is_mom_buy = _cross_up(macd, macd_sig, prev_macd, prev_macd_sig) and vol_spike and close > ema21
    is_mom_sell = _cross_down(macd, macd_sig, prev_macd, prev_macd_sig) and vol_spike and close < ema21
    is_rev_buy = rsi < 35.0 and close > open_px and close > prev_high and macd > macd_sig
    is_rev_sell = rsi > 65.0 and close < open_px and close < prev_low and macd < macd_sig
    bull_pat = str(row.get("pat_bull_name", "") or "")
    bear_pat = str(row.get("pat_bear_name", "") or "")

    if is_buy:
        if is_breakout:
            return "Breakout"
        if is_pull_buy:
            return "Pullback"
        if is_mom_buy:
            return "Momentum"
        if is_rev_buy:
            return "Reversal"
        if _bool(row.get("pat_valid_bull", False)) and bull_pat:
            return bull_pat
        return "Trend"
    if is_breakdown:
        return "Breakdown"
    if is_pull_sell:
        return "Pullback"
    if is_mom_sell:
        return "Momentum"
    if is_rev_sell:
        return "Reversal"
    if _bool(row.get("pat_valid_bear", False)) and bear_pat:
        return bear_pat
    return "Trend"


def prepare_apex_frame(
    df: pd.DataFrame,
    *,
    instrument: str,
    df_5m: Optional[pd.DataFrame] = None,
    df_15m: Optional[pd.DataFrame] = None,
) -> tuple[pd.DataFrame, ApexConfig]:
    base = _normalize_ohlcv(df)
    if base is None:
        return pd.DataFrame(), _apex_config(instrument)
    cfg = _apex_config(instrument)
    frame_5m = _normalize_ohlcv(df_5m)
    frame_15m = _normalize_ohlcv(df_15m)
    out = compute_all_indicators(base, frame_5m, frame_15m)
    out = compute_patterns(out, cfg)
    out = compute_confluence(out, cfg)
    out["session_ok"] = compute_session_mask(out, cfg)
    return out, cfg


def build_apex_signals(
    df: pd.DataFrame,
    *,
    instrument: str,
    timeframe: str,
    df_5m: Optional[pd.DataFrame] = None,
    df_15m: Optional[pd.DataFrame] = None,
    latest_only: bool = False,
) -> list[UTBotSignal]:
    frame, cfg = prepare_apex_frame(df, instrument=instrument, df_5m=df_5m, df_15m=df_15m)
    if frame.empty:
        return []

    signals: list[UTBotSignal] = []
    last_sig_bar = -999
    last_sig_state = 0
    rows = list(frame.iterrows())
    for pos, (ts, row) in enumerate(rows):
        if pos == 0:
            continue
        prev = rows[pos - 1][1]
        atr = float(row.get("atr", 0.0) or 0.0)
        adx = float(row.get("adx", 0.0) or 0.0)
        close = float(row.get("close", 0.0) or 0.0)
        open_px = float(row.get("open", close) or close)
        high = float(row.get("high", close) or close)
        low = float(row.get("low", close) or close)
        if close <= 0 or not (_is_finite(atr) and _is_finite(adx)):
            continue
        data_ready = pos >= cfg.min_bars_required
        if not data_ready:
            continue

        dyn_min = float(cfg.min_score)
        bull_score = float(row.get("bull_score", 0.0) or 0.0)
        bear_score = float(row.get("bear_score", 0.0) or 0.0)
        body_abs = abs(close - open_px)
        body_ok = body_abs >= atr * 0.15
        body_pct = float(row.get("body_pct", 0.5) or 0.5)
        regime_ok = adx >= float(cfg.min_adx)
        htf_bull = (not cfg.use_htf) or (
            float(row.get("ema9_15", 0.0) or 0.0) > float(row.get("ema21_15", 0.0) or 0.0)
            and float(row.get("ema9_15_slope", 0.0) or 0.0) > 0.0
        )
        htf_bear = (not cfg.use_htf) or (
            float(row.get("ema9_15", 0.0) or 0.0) < float(row.get("ema21_15", 0.0) or 0.0)
            and float(row.get("ema9_15_slope", 0.0) or 0.0) < 0.0
        )
        score_bull = bull_score >= dyn_min
        score_bear = bear_score >= dyn_min
        conf_bull = (bull_score - bear_score) >= float(cfg.conflict_margin) and bear_score < 45.0
        conf_bear = (bear_score - bull_score) >= float(cfg.conflict_margin) and bull_score < 45.0
        vol_ok = _bool(row.get("vol_above", False)) or _bool(row.get("vol_spike", False))
        vb_bull = vol_ok and body_pct > 0.5 and body_ok
        vb_bear = vol_ok and body_pct < 0.5 and body_ok
        session_ok = _bool(row.get("session_ok", True))
        can_signal = (pos - last_sig_bar) >= int(cfg.signal_cooldown)

        buy = (
            regime_ok and htf_bull and score_bull and conf_bull and vb_bull
            and session_ok and can_signal and last_sig_state <= 0
        )
        sell = (
            regime_ok and htf_bear and score_bear and conf_bear and vb_bear
            and session_ok and can_signal and last_sig_state >= 0
        )
        if not (buy or sell):
            continue

        signal_type = "BUY" if buy else "SELL"
        is_long = signal_type == "BUY"
        anchor_slice = frame.iloc[max(0, pos - 2): pos + 1]
        anchor_low = float(anchor_slice["low"].min() or low)
        anchor_high = float(anchor_slice["high"].max() or high)
        sl1, _sl2, sl_mode = calc_sl(
            is_long,
            close,
            anchor_low,
            anchor_high,
            atr,
            cfg,
            bool(row.get("high_vol", False)),
            bool(row.get("low_vol", False)),
            bool(row.get("is_trending", False)),
        )
        stop_distance = abs(close - float(sl1 or close)) or max(atr, close * 0.001, 0.05)
        if not _is_finite(sl1) or stop_distance <= 0:
            sl1 = close - stop_distance if is_long else close + stop_distance

        signal_time = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        setup = _setup_reason(row, prev, is_long)
        signal = UTBotSignal(
            timestamp=signal_time,
            signal_type=signal_type,
            price=close,
            trailing_stop=float(sl1),
            stop_distance=float(abs(close - float(sl1))),
            atr_value=atr,
            adx_value=adx,
            position_state=1 if is_long else -1,
            bar_index=pos,
            instrument=instrument,
            timeframe=timeframe,
            suggested_qty=0,
            raw_candle={
                "open": open_px,
                "high": high,
                "low": low,
                "close": close,
            },
            source_engine="apex_hybrid",
            source_lane="B",
        )
        ctx = ApexSignalContext(
            bull_score=round(bull_score, 2),
            bear_score=round(bear_score, 2),
            score_diff=round(bull_score - bear_score, 2),
            setup=setup,
            vol_regime=str(row.get("vol_regime", "NORMAL") or "NORMAL"),
            body_pct=round(float(body_pct or 0.0), 4),
            rel_vol=round(float(row.get("rel_vol", 1.0) or 1.0), 4),
            sl_mode=str(sl_mode or ""),
        )
        setattr(signal, "source_engines", ["apex_hybrid"])
        setattr(signal, "apex_context", ctx.__dict__)
        setattr(
            signal,
            "apex_reasons",
            [
                f"APEX {setup}",
                f"score {max(bull_score, bear_score):.0f}",
                f"diff {abs(bull_score - bear_score):.0f}",
                f"ADX {adx:.1f}",
                f"relVol {float(row.get('rel_vol', 1.0) or 1.0):.1f}x",
            ],
        )
        signals.append(signal)
        last_sig_bar = pos
        last_sig_state = 1 if is_long else -1

    if latest_only:
        if not signals:
            return []
        latest_ts = frame.index[-1]
        recent = [sig for sig in signals if getattr(sig, "timestamp", None) == (latest_ts.to_pydatetime() if hasattr(latest_ts, "to_pydatetime") else latest_ts)]
        return recent or signals[-1:]
    return signals
