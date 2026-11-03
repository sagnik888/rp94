"""UT SMART EXIT engine: the user's TradingView "UT Bot + Smart Peak Exit v6" indicator.

Replaces ADAPTIVE DUAL in the engine selector (2026-09-25). A line-by-line port:

    entries   the original UT Bot flip: source crosses the ATR trailing stop
              (key_value x ATR(atr_period)), confirmed on the candle close. The entry
              price is the signal bar's CLOSE -- this script does not book the signal
              bar's open (the lookahead the S1 script had).
    exits     1. the opposite UT signal (the trailing stop is the stop: a close through
                 it flips the position), and
              2. the Smart Peak Exit: once the trade has run arm_atr ATRs from entry
                 (and min_bars bars have passed), exit on a close retrace_atr ATRs off
                 the peak/trough confirmed by `confirmations` of four exhaustion votes
                 (RSI rolling over from overbought/oversold, MACD histogram fading, close
                 through the fast EMA, a rejection wick), or on any close hard_retrace_atr
                 ATRs off the peak/trough.

Pine built-ins follow the documented TradingView recurrences: RMA starts from an SMA,
while EMA starts from the first available source value. Missing samples are ignored.
Session bars only: the candle store's frozen 15:15-15:29
bars and post-close prints are dropped first, as for S1.

The per-bar work runs in one numba loop, so the live engine and the offline tuning study
(reports/ut_smart_exit_study_20260925.py) execute exactly the same code.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Optional

import numpy as np
import pandas as pd
from numba import njit

ENGINE_KEY = "ut_smart"
LANE_TAG = "UTS"
# Candles fed to the live engine ahead of the session (scanner._signal_engine_warmup_bars).
FEED_BARS = 300

EXIT_NONE, EXIT_SMART, EXIT_HARD, EXIT_REVERSE = 0, 1, 2, 3
EXIT_NAMES = {EXIT_SMART: "SMART_EXIT", EXIT_HARD: "SMART_HARD_EXIT", EXIT_REVERSE: "UT_REVERSE"}


@dataclass(frozen=True)
class UTSParams:
    key_value: float = 1.0
    atr_period: int = 10
    use_heikin_ashi: bool = False
    smart_exit: bool = True
    arm_atr: float = 0.80
    retrace_atr: float = 0.45
    hard_retrace_atr: float = 1.10
    min_bars: int = 2
    confirmations: int = 2
    rsi_length: int = 14
    rsi_ob: float = 70.0
    rsi_os: float = 30.0
    rsi_lookback: int = 5
    exit_ema: int = 5
    wick_ratio: float = 0.35
    session_end: str = "15:15"


# settings field -> UTSParams field
SETTING_FIELDS = {
    "uts_key_value": "key_value",
    "uts_atr_period": "atr_period",
    "uts_use_heikin_ashi": "use_heikin_ashi",
    "uts_smart_exit": "smart_exit",
    "uts_arm_atr": "arm_atr",
    "uts_retrace_atr": "retrace_atr",
    "uts_hard_retrace_atr": "hard_retrace_atr",
    "uts_min_bars": "min_bars",
    "uts_confirmations": "confirmations",
    "uts_rsi_length": "rsi_length",
    "uts_rsi_ob": "rsi_ob",
    "uts_rsi_os": "rsi_os",
    "uts_rsi_lookback": "rsi_lookback",
    "uts_exit_ema": "exit_ema",
    "uts_wick_ratio": "wick_ratio",
    "uts_session_end": "session_end",
}


# settings field -> UTSParams field, per timeframe (the 15m values are the base fields).
PER_TIMEFRAME_FIELDS = {
    "5min": {"uts_key_value_5min": "key_value", "uts_atr_period_5min": "atr_period"},
    "1min": {"uts_key_value_1min": "key_value", "uts_atr_period_1min": "atr_period"},
}


def params_from_settings(settings=None, timeframe: Optional[str] = None) -> UTSParams:
    """The engine inputs for one timeframe: the shared inputs, with the 5m/1m trail."""
    if settings is None:
        try:
            from config.settings import get_settings

            settings = get_settings()
        except Exception:
            settings = None
    base = UTSParams()
    values = {}
    for setting, field_name in SETTING_FIELDS.items():
        default = getattr(base, field_name)
        raw = getattr(settings, setting, default) if settings is not None else default
        try:
            if isinstance(default, bool):
                values[field_name] = bool(raw)
            elif isinstance(default, int):
                values[field_name] = int(raw)
            elif isinstance(default, float):
                values[field_name] = float(raw)
            else:
                values[field_name] = str(raw or default)
        except (TypeError, ValueError):
            values[field_name] = default
    for setting, field_name in PER_TIMEFRAME_FIELDS.get(str(timeframe or "").lower(), {}).items():
        raw = getattr(settings, setting, None) if settings is not None else None
        if raw is None:
            continue
        try:
            values[field_name] = type(getattr(base, field_name))(raw)
        except (TypeError, ValueError):
            pass
    values["atr_period"] = max(1, values["atr_period"])
    values["min_bars"] = max(0, values["min_bars"])
    values["confirmations"] = max(1, min(4, values["confirmations"]))
    values["rsi_length"] = max(2, values["rsi_length"])
    values["rsi_lookback"] = max(2, values["rsi_lookback"])
    values["exit_ema"] = max(1, values["exit_ema"])
    return UTSParams(**values)


def evaluation_clock(as_of=None) -> pd.Timestamp:
    """Explicit replay clock, or the live market clock, normalized to local time."""
    if as_of is None:
        from datetime import datetime
        import pytz

        as_of = datetime.now(pytz.timezone('Asia/Kolkata'))
    stamp = pd.Timestamp(as_of)
    if pd.isna(stamp):
        raise ValueError('Missing UT SMART EXIT evaluation clock')
    return stamp.tz_convert('Asia/Kolkata').tz_localize(None) if stamp.tzinfo else stamp


def closed_bar_mask(df, timeframe: str, as_of=None):
    minutes = max(1, int(str(timeframe).lower().replace('min', '').replace('m', '')))
    stamps = df.index
    if stamps.tz is not None:
        stamps = stamps.tz_convert('Asia/Kolkata').tz_localize(None)
    return stamps + pd.Timedelta(minutes=minutes) <= evaluation_clock(as_of)


# ─────────────────────────────────────────────────────────────── Pine built-ins


@njit(cache=True)
def _rma(x, n):
    """ta.rma: seeded with the SMA of the first n values, then alpha = 1/n."""
    out = np.full(len(x), np.nan)
    alpha = 1.0 / n
    total = 0.0
    count = 0
    prev = np.nan
    for i in range(len(x)):
        v = x[i]
        if np.isnan(prev):
            if np.isnan(v):
                continue
            total += v
            count += 1
            if count == n:
                prev = total / n
                out[i] = prev
        else:
            if not np.isnan(v):
                prev = alpha * v + (1.0 - alpha) * prev
            out[i] = prev
    return out


@njit(cache=True)
def _ema(x, n):
    """ta.ema's documented equivalent: first source, then alpha = 2/(n+1)."""
    out = np.full(len(x), np.nan)
    alpha = 2.0 / (n + 1.0)
    prev = np.nan
    for i in range(len(x)):
        v = x[i]
        if np.isnan(prev):
            if np.isnan(v):
                continue
            prev = v
        else:
            if not np.isnan(v):
                prev = alpha * v + (1.0 - alpha) * prev
        out[i] = prev
    return out


@njit(cache=True)
def _true_range(high, low, close):
    n = len(close)
    tr = np.empty(n)
    for i in range(n):
        hl = high[i] - low[i]
        if i == 0:
            tr[i] = hl
        else:
            tr[i] = max(hl, abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    return tr


@njit(cache=True)
def _rsi(src, n):
    m = len(src)
    up = np.full(m, np.nan)
    down = np.full(m, np.nan)
    for i in range(1, m):
        ch = src[i] - src[i - 1]
        if np.isnan(ch):
            continue
        up[i] = ch if ch > 0 else 0.0
        down[i] = -ch if ch < 0 else 0.0
    ru = _rma(up, n)
    rd = _rma(down, n)
    out = np.full(m, np.nan)
    for i in range(m):
        if np.isnan(ru[i]) or np.isnan(rd[i]):
            continue
        if rd[i] == 0.0:
            out[i] = 100.0
        elif ru[i] == 0.0:
            out[i] = 0.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + ru[i] / rd[i])
    return out


@njit(cache=True)
def _heikin_ashi_close(op, hi, lo, cl):
    n = len(cl)
    ha_close = (op + hi + lo + cl) / 4.0
    return ha_close


@njit(cache=True)
def _trailing_stop(src, n_loss):
    """Section 6: the original UT Bot trailing stop (nz(prev) = 0 during ATR warm-up)."""
    n = len(src)
    ts = np.full(n, np.nan)
    for i in range(n):
        if np.isnan(n_loss[i]):
            continue
        prev = ts[i - 1] if i > 0 and not np.isnan(ts[i - 1]) else 0.0
        s = src[i]
        s1 = src[i - 1] if i > 0 else np.nan
        if s > prev and s1 > prev:
            ts[i] = max(prev, s - n_loss[i])
        elif s < prev and s1 < prev:
            ts[i] = min(prev, s + n_loss[i])
        elif s > prev:
            ts[i] = s - n_loss[i]
        else:
            ts[i] = s + n_loss[i]
    return ts


@njit(cache=True)
def _ut_position(src, ts):
    """Pine section 7's strict crossing state, independent of Smart Exit state."""
    out = np.zeros(len(src), np.int8)
    position = 0
    for i in range(len(src)):
        previous_stop = ts[i - 1] if i > 0 and not np.isnan(ts[i - 1]) else 0.0
        previous_source = src[i - 1] if i > 0 else np.nan
        if previous_source < previous_stop and src[i] > previous_stop:
            position = 1
        elif previous_source > previous_stop and src[i] < previous_stop:
            position = -1
        out[i] = position
    return out


# ─────────────────────────────────────────────────────────────── the state machine


@njit(cache=True)
def _run(src, op, hi, lo, cl, atr, ts, rsi, hist, ema_exit,
         smart_exit, arm_atr, retrace_atr, hard_retrace_atr, min_bars, confirmations,
         rsi_ob, rsi_os, rsi_lookback, wick_ratio, mintick=0.05):
    """Sections 8-24, bar by bar. Returns per-bar arrays:

    signal  +1 BUY / -1 SELL on the bar that confirmed it
    state   tradeState after the bar (1 long, -1 short, 0 flat)
    exit    EXIT_* code for a smart exit that closed the position on this bar
    runup, retrace  in ATRs, as the bar evaluated them (0 when flat)
    """
    n = len(src)
    signal = np.zeros(n, np.int8)
    state_out = np.zeros(n, np.int8)
    exit_out = np.zeros(n, np.int8)
    runup_out = np.zeros(n)
    retrace_out = np.zeros(n)
    state = 0
    entry = np.nan
    peak = np.nan
    trough = np.nan
    bars = 0
    for i in range(n):
        buy = False
        sell = False
        if i > 0 and not np.isnan(ts[i]) and not np.isnan(ts[i - 1]):
            # rawBuy = src > trail and crossover(ema(src,1), trail)
            buy = src[i] > ts[i] and src[i - 1] <= ts[i - 1]
            sell = src[i] < ts[i] and src[i - 1] >= ts[i - 1]
        a = atr[i] if not np.isnan(atr[i]) else 0.0

        # 11/12. peak / trough and bars in trade
        if state == 1:
            bars += 1
            if np.isnan(peak) or hi[i] > peak:
                peak = hi[i]
        elif state == -1:
            bars += 1
            if np.isnan(trough) or lo[i] < trough:
                trough = lo[i]

        # 13-15. run-up and retracement in ATRs
        long_runup = peak - entry if state == 1 and not np.isnan(peak) and not np.isnan(entry) else 0.0
        short_runup = entry - trough if state == -1 and not np.isnan(trough) and not np.isnan(entry) else 0.0
        long_runup_atr = long_runup / a if a > 0 else 0.0
        short_runup_atr = short_runup / a if a > 0 else 0.0
        long_armed = state == 1 and long_runup_atr >= arm_atr and bars >= min_bars
        short_armed = state == -1 and short_runup_atr >= arm_atr and bars >= min_bars
        long_retrace = peak - cl[i] if state == 1 and not np.isnan(peak) else 0.0
        short_retrace = cl[i] - trough if state == -1 and not np.isnan(trough) else 0.0
        long_retrace_atr = long_retrace / a if a > 0 else 0.0
        short_retrace_atr = short_retrace / a if a > 0 else 0.0

        # 16. RSI rollover from an extreme within the lookback (current bar included)
        hi_rsi = np.nan
        lo_rsi = np.nan
        for k in range(max(0, i - rsi_lookback + 1), i + 1):
            if not np.isnan(rsi[k]):
                hi_rsi = rsi[k] if np.isnan(hi_rsi) else max(hi_rsi, rsi[k])
                lo_rsi = rsi[k] if np.isnan(lo_rsi) else min(lo_rsi, rsi[k])
        prev_rsi = rsi[i - 1] if i > 0 else np.nan
        long_rsi = (not np.isnan(hi_rsi)) and hi_rsi >= rsi_ob and rsi[i] < prev_rsi
        short_rsi = (not np.isnan(lo_rsi)) and lo_rsi <= rsi_os and rsi[i] > prev_rsi
        # 17. MACD histogram fading
        prev_hist = hist[i - 1] if i > 0 else np.nan
        long_macd = hist[i] < prev_hist
        short_macd = hist[i] > prev_hist
        # 18. close through the fast EMA
        long_ema = cl[i] < ema_exit[i]
        short_ema = cl[i] > ema_exit[i]
        # 19. Pine uses syminfo.mintick as the range floor. The symbol feed
        # may override the index default through indicators' mintick value.
        rng = max(hi[i] - lo[i], mintick)
        upper = hi[i] - max(op[i], cl[i])
        lower = min(op[i], cl[i]) - lo[i]
        long_rej = upper / rng >= wick_ratio and cl[i] < op[i]
        short_rej = lower / rng >= wick_ratio and cl[i] > op[i]
        # 20. exhaustion score
        long_score = int(long_rsi) + int(long_macd) + int(long_ema) + int(long_rej)
        short_score = int(short_rsi) + int(short_macd) + int(short_ema) + int(short_rej)
        # 21-23. smart exit
        long_weak = long_retrace_atr >= retrace_atr and long_score >= confirmations
        short_weak = short_retrace_atr >= retrace_atr and short_score >= confirmations
        long_hard = long_armed and long_retrace_atr >= hard_retrace_atr
        short_hard = short_armed and short_retrace_atr >= hard_retrace_atr
        exit_long_smart = smart_exit and long_armed and long_weak and not buy and not sell
        exit_short_smart = smart_exit and short_armed and short_weak and not buy and not sell
        exit_long = exit_long_smart or (smart_exit and long_hard and not buy and not sell)
        exit_short = exit_short_smart or (smart_exit and short_hard and not buy and not sell)

        if state == 1:
            runup_out[i] = long_runup_atr
            retrace_out[i] = long_retrace_atr
        elif state == -1:
            runup_out[i] = short_runup_atr
            retrace_out[i] = short_retrace_atr

        # 24. state management
        if buy:
            signal[i] = 1
            state = 1
            entry = cl[i]
            peak = hi[i]  # Literal Pine indicator seed; live premium peak is separate.
            trough = np.nan
            bars = 0
        elif sell:
            signal[i] = -1
            state = -1
            entry = cl[i]
            trough = lo[i]  # Literal Pine indicator seed; this is chart state only.
            peak = np.nan
            bars = 0
        elif exit_long:
            exit_out[i] = EXIT_SMART if exit_long_smart else EXIT_HARD
            state = 0
            entry = np.nan
            peak = np.nan
            trough = np.nan
            bars = 0
        elif exit_short:
            exit_out[i] = EXIT_SMART if exit_short_smart else EXIT_HARD
            state = 0
            entry = np.nan
            peak = np.nan
            trough = np.nan
            bars = 0
        state_out[i] = state
    return signal, state_out, exit_out, runup_out, retrace_out


# ─────────────────────────────────────────────────────────────── public API


def drop_off_session_bars(df: Optional[pd.DataFrame], session_end: str = "15:15") -> Optional[pd.DataFrame]:
    """Session bars only: [09:15, session_end). Same rule as S1 (see its docstring)."""
    if df is None or len(df) == 0:
        return df
    try:
        hh, mm = (int(x) for x in str(session_end).split(":")[:2])
    except ValueError:
        hh, mm = 15, 15
    market_index = df.index
    if market_index.tz is not None:
        market_index = market_index.tz_convert("Asia/Kolkata")
    minutes = market_index.hour * 60 + market_index.minute
    keep = (minutes >= 9 * 60 + 15) & (minutes < hh * 60 + mm)
    return df[keep]


def indicators(df: pd.DataFrame, p: UTSParams) -> dict:
    """Sections 5-9 on one frame: ATR, trailing stop, RSI, MACD histogram, exit EMA."""
    op = df["open"].to_numpy(dtype=float)
    hi = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    cl = df["close"].to_numpy(dtype=float)
    src = _heikin_ashi_close(op, hi, lo, cl) if p.use_heikin_ashi else cl
    atr = _rma(_true_range(hi, lo, cl), int(p.atr_period))
    ts = _trailing_stop(src, float(p.key_value) * atr)
    rsi = _rsi(src, int(p.rsi_length))
    macd = _ema(src, 12) - _ema(src, 26)
    hist = macd - _ema(macd, 9)
    ema_exit = _ema(src, int(p.exit_ema))
    mintick = float(df.attrs.get("mintick", 0.05))
    if not np.isfinite(mintick) or mintick <= 0:
        mintick = 0.05
    return {"open": op, "high": hi, "low": lo, "close": cl, "src": src, "atr": atr, "trail": ts,
            "rsi": rsi, "hist": hist, "ema_exit": ema_exit, "mintick": mintick}


def run(ind: dict, p: UTSParams) -> dict:
    signal, state, exit_code, runup, retrace = _run(
        ind["src"], ind["open"], ind["high"], ind["low"], ind["close"], ind["atr"], ind["trail"],
        ind["rsi"], ind["hist"], ind["ema_exit"],
        bool(p.smart_exit), float(p.arm_atr), float(p.retrace_atr), float(p.hard_retrace_atr),
        int(p.min_bars), int(p.confirmations), float(p.rsi_ob), float(p.rsi_os),
        int(p.rsi_lookback), float(p.wick_ratio), float(ind.get("mintick", 0.05)),
    )
    return {"signal": signal, "state": state, "ut_position": _ut_position(ind["src"], ind["trail"]),
            "exit": exit_code, "runup_atr": runup, "retrace_atr": retrace}


def trades(index: pd.DatetimeIndex, ind: dict, out: dict) -> list:
    """The chart's own trades: one per signal, closed by the next signal (reverse) or a
    smart exit. Open at the end of the data -> exit fields None."""
    rows = []
    open_row = None
    sig, ex, cl = out["signal"], out["exit"], ind["close"]
    for i in range(len(sig)):
        if sig[i] != 0:
            if open_row is not None:
                open_row.update(exit_bar=i, exit_time=index[i], exit_price=float(cl[i]), exit_reason="UT_REVERSE")
                rows.append(open_row)
            open_row = {"bar": i, "time": index[i], "direction": int(sig[i]), "price": float(cl[i]),
                        "trail": float(ind["trail"][i]), "atr": float(ind["atr"][i]),
                        "exit_bar": None, "exit_time": None, "exit_price": None, "exit_reason": ""}
        elif ex[i] != EXIT_NONE and open_row is not None:
            open_row.update(exit_bar=i, exit_time=index[i], exit_price=float(cl[i]),
                            exit_reason=EXIT_NAMES[int(ex[i])])
            rows.append(open_row)
            open_row = None
    if open_row is not None:
        rows.append(open_row)
    return rows


def field_names() -> tuple:
    return tuple(f.name for f in fields(UTSParams))
