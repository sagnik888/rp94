"""S1 signal engine: a Python port of the "S1" TradingView indicator (Pine v6).

Ported sections of the Pine script:
    3     core indicators (EMA 9/13/21/50/200, session VWAP, ATR14, RSI14, MACD 12/26/9,
          DMI 14, relative volume, body position) and the lookahead-safe 15m/5m values
    3c/3e the candlestick pattern engine with its validity gates
    4     the 10-component bull/bear confluence score
    5     the setup reason label
    7-13  stops, R-multiple targets, the five entry gates, the trailing stop, the exit
          engine and the loss-streak circuit breaker

Two state machines, chosen by S1Params.entry_mode:
    "pine"  (default) the TradingView state machine, bar for bar: the trade is booked at
            the signal bar's OPEN and managed on that same bar. That open price is
            lookahead (the signal needs the bar's close), so it is never used as a fill;
            it is used only so the in-trade / cooldown / circuit-breaker state, and
            therefore which bars signal, matches the TradingView chart.
    "close" the tradable version: entry at the raw market bar's close, management from
            the next bar on raw prices, stops that gap through fill at the raw open.
            Gates and momentum still read the selected indicator candle type.

Inputs the user set away from the Pine defaults (2026-09-23): min score 60, zero cooldown,
3 exit-confirmation bars, 3 consecutive losses per index, 5-bar pause.
The reference chart uses Heikin Ashi; standard candles remain selectable. Each requested
timeframe is converted independently from raw OHLC before the Pine indicators run.

Stops. The "pine" state machine always uses the chart's own stops: fixed SL1/SL2 percents
(sl1_pct / sl2_pct, current system inputs 0.10 / 0.40) with the SL2 gap exit, exactly as
calcSL does. Any other stop there would change trade length, the loss streak and therefore
which bars can signal. Live levels use the signal close with a fixed 0.10% SL1 by default
and no SL2. The existing optional anchor and ATR stop modes remain configurable for
live levels and the "close" state machine.

Parity limits. The state machine starts flat at the first bar of the frame it is given,
so a loss streak or an open chart trade older than the window is not known. Matching
signals also requires identical OHLCV, settings and history. The live feed supplies
900 warmup bars. Historical 15m charts sample the first 5m intrabar's previous RSI.
Pine's realtime lower-timeframe request can repaint after reload; this engine uses
stable historical mapping, with no future intrabar dependency.

The optional swing forecast (projection, direction, confidence and entry filter) is
removed at the user's request. The core S1 gates and Pine trade state remain intact.

The engine is stateless between calls: every call replays the whole frame.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from engine.ut_bot_core import UTBotSignal, _nb_heikin_ashi

# Bars the Pine waits for before any signal (dataReady: bar_index >= 200).
S1_WARMUP_BARS = 200
# History the live feed hands the engine. EMA200 keeps (1 - 2/201)^n of its seed error, so
# 900 bars leaves ~1e-4 and the stack matches a chart that has years of history.
S1_FEED_BARS = 900

S1_INDICES = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")


@dataclass(frozen=True)
class S1Params:
    candle_type: str = "heikin_ashi"  # source used by the user's TradingView chart
    min_score: float = 60.0
    conflict_margin: float = 20.0
    min_adx: float = 20.0
    use_htf: bool = True
    signal_cooldown: int = 0
    sl_mode: str = "anchor"         # live stop: "fixed" | "anchor" | "atr" (user: dynamic 0.10-0.25%)
    sl1_pct: float = 0.10            # live SL1 % in fixed live mode
    # The chart's own calcSL inputs drive the pine state machine. 2026-09-24: the user's
    # TradingView labels print SL1 0.20% / SL2 0.40%; running the state machine on the
    # 0.10% live stop ended the 13:40 NIFTY trade inside its own bar and produced SELLs at
    # 13:45, 13:55 and 14:20 that the chart never showed.
    chart_sl1_pct: float = 0.20
    chart_sl2_pct: float = 0.40
    # TradingView's index charts show 09:15-15:15. The feed also keeps pre-open prints and
    # 15:15-15:29 bars that are frozen and then jump to the post-close price with bulk
    # volume; they are dropped before S1 computes (drop_off_session_bars).
    session_end: str = "15:15"
    sl_min_pct: float = 0.10         # dynamic modes clamp
    sl_max_pct: float = 0.25
    sl_atr_mult: float = 1.0         # atr mode
    use_sl2: bool = False            # live stop only; the pine state machine keeps SL2
    # Fixed live SL1 % per timeframe (2026-10-08 user: 5m 0.10%, 15m 0.15%); 0 = sl_mode.
    sl1_pct_5min: float = 0.10
    sl1_pct_15min: float = 0.15
    sl2_pct: float = 0.40            # chart SL2 %
    t1_r: float = 2.0
    t2_r: float = 3.0
    t3_r: float = 4.0
    use_trail: bool = True
    trail_start_r: float = 1.5
    trail_mult: float = 1.8
    lock_at_t1: bool = True
    exit_conf_bars: int = 3
    max_consec_loss: int = 3
    circuit_pause: int = 5
    use_pattern_score: bool = True
    entry_mode: str = "pine"         # "pine" | "close"
    session_rules: bool = False      # intraday cutoff / session-end exit (not in the Pine)
    warmup_bars: int = S1_WARMUP_BARS
    entry_cutoff: str = "15:00"
    session_exit: str = "15:15"


def _index_key(instrument: str) -> str:
    return "".join(ch for ch in str(instrument or "").lower() if ch.isalnum())


def params_from_settings(settings=None, instrument: str = "") -> S1Params:
    """Read the S1 inputs from settings; the circuit breaker is per index."""
    if settings is None:
        try:
            from config.settings import get_settings

            settings = get_settings()
        except Exception:
            return S1Params()
    base = S1Params()
    g = lambda name, default: getattr(settings, name, default)
    key = _index_key(instrument)
    # Per-index loss streak: a custom value wins; 0/unset follows the system-wide
    # "Stop After Consecutive Losses" (max_consecutive_losses).
    consec = g(f"s1_max_consec_loss_{key}", None) if key else None
    try:
        consec = int(consec) if consec is not None else 0
    except (TypeError, ValueError):
        consec = 0
    if consec <= 0:
        try:
            consec = int(g("max_consecutive_losses", 0) or 0)
        except (TypeError, ValueError):
            consec = 0
    return replace(
        base,
        candle_type=str(g("s1_candle_type", base.candle_type)),
        min_score=float(g("s1_min_score", base.min_score)),
        conflict_margin=float(g("s1_conflict_margin", base.conflict_margin)),
        min_adx=float(g("s1_min_adx", base.min_adx)),
        use_htf=bool(g("s1_use_htf", base.use_htf)),
        signal_cooldown=int(g("s1_signal_cooldown", base.signal_cooldown)),
        sl_mode=str(g("s1_sl_mode", base.sl_mode)),
        sl1_pct=float(g("s1_sl1_pct", base.sl1_pct)),
        sl1_pct_5min=float(g("s1_sl1_pct_5min", base.sl1_pct_5min) or 0.0),
        sl1_pct_15min=float(g("s1_sl1_pct_15min", base.sl1_pct_15min) or 0.0),
        chart_sl1_pct=float(g("s1_chart_sl1_pct", base.chart_sl1_pct)),
        chart_sl2_pct=float(g("s1_chart_sl2_pct", base.chart_sl2_pct)),
        session_end=str(g("s1_session_end", base.session_end)),
        sl_min_pct=float(g("s1_sl_min_pct", base.sl_min_pct)),
        sl_max_pct=float(g("s1_sl_max_pct", base.sl_max_pct)),
        sl_atr_mult=float(g("s1_sl_atr_mult", base.sl_atr_mult)),
        use_sl2=bool(g("s1_use_sl2", base.use_sl2)),
        sl2_pct=float(g("s1_sl2_pct", base.sl2_pct)),
        t1_r=float(g("s1_t1_r", base.t1_r)),
        t2_r=float(g("s1_t2_r", base.t2_r)),
        t3_r=float(g("s1_t3_r", base.t3_r)),
        trail_start_r=float(g("s1_trail_start_r", base.trail_start_r)),
        trail_mult=float(g("s1_trail_mult", base.trail_mult)),
        exit_conf_bars=int(g("s1_exit_conf_bars", base.exit_conf_bars)),
        max_consec_loss=int(consec if consec > 0 else base.max_consec_loss),
        circuit_pause=int(g("s1_circuit_pause", base.circuit_pause)),
        entry_mode=str(g("s1_entry_mode", base.entry_mode)),
    )


# ---------------------------------------------------------------------------
# Pine primitives
# ---------------------------------------------------------------------------

def s1_candle_frame(df: pd.DataFrame, candle_type: str = "heikin_ashi") -> pd.DataFrame:
    """Build the indicator's chart candles from raw market OHLC, without mutating it.

    Each timeframe must be converted independently. Aggregating Heikin Ashi candles
    from a lower timeframe does not reproduce that timeframe's TradingView candles.
    """
    kind = str(candle_type).strip().lower()
    if kind not in ("standard", "heikin_ashi"):
        raise ValueError(f"Unsupported S1 candle type: {candle_type}")
    result = df.copy()
    if kind == "heikin_ashi" and len(result):
        values = _nb_heikin_ashi(*[df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close")])
        for key, value in zip(("open", "high", "low", "close"), values):
            result[key] = value
    return result


def _exchange_index(index) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(index)
    return index.tz_convert("Asia/Kolkata").tz_localize(None) if index.tz is not None else index


def _target_candle_frame(df: pd.DataFrame, candle_type: str) -> pd.DataFrame:
    target = df.copy()
    target.index = _exchange_index(target.index)
    target = target[~target.index.duplicated(keep="last")].sort_index()
    return s1_candle_frame(target, candle_type)


def _raw_timeframe_frame(df: pd.DataFrame, source_minutes: int, minutes: int) -> pd.DataFrame:
    """Fallback when an independent raw target frame is unavailable.

    Only complete, aligned groups are admitted. Aggregate raw OHLC first; HA is
    applied afterward. The target candle becomes available at its closing time.
    """
    if minutes % source_minutes or source_minutes > minutes:
        raise ValueError(f"S1 requires actual {minutes}min candles for this timeframe")
    source = df.copy()
    source.index = _exchange_index(source.index)
    source = source[~source.index.duplicated(keep="last")].sort_index()
    groups = source.resample(f"{minutes}min")
    aggregations = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in source:
        aggregations["volume"] = "sum"
    target = groups.agg(aggregations)
    counts = groups["close"].count()
    on_grid = (source.index.minute % source_minutes == 0) & (source.index.second == 0) & (source.index.microsecond == 0)
    aligned = pd.Series(on_grid, index=source.index).resample(f"{minutes}min").min() == 1
    return target.loc[(counts == minutes // source_minutes) & aligned].dropna(subset=["open", "high", "low", "close"])


def _confirmed_tf_value(index, target: pd.DataFrame, minutes: int, fn) -> pd.Series:
    """Pine close[1]/lookahead_on, made available only after target bar closes."""
    close = target["close"].astype(float).copy()
    close.index = _exchange_index(close.index)
    close = close[~close.index.duplicated(keep="last")].sort_index()
    value = fn(close)
    value.index = value.index + pd.Timedelta(minutes=minutes)
    aligned = value.reindex(_exchange_index(index), method="ffill")
    return pd.Series(aligned.to_numpy(), index=index, dtype=float)

def _ema(values: pd.Series, length: int) -> pd.Series:
    return values.ewm(span=length, adjust=False).mean()


def _rma(values: pd.Series, length: int) -> pd.Series:
    # Pine seeds Wilder smoothing with the first length non-missing observations.
    valid = np.flatnonzero(values.notna().to_numpy())
    seeded = values.astype(float).copy()
    if len(valid) < length:
        return pd.Series(np.nan, index=values.index, dtype=float)
    start = int(valid[length - 1])
    seed = float(values.iloc[valid[:length]].mean())
    seeded.iloc[:start] = np.nan
    seeded.iloc[start] = seed
    return seeded.ewm(alpha=1.0 / length, adjust=False, ignore_na=True).mean()


def _rsi(close: pd.Series, length: int = 14) -> pd.Series:
    change = close.diff()
    up = _rma(change.clip(lower=0.0), length)
    down = _rma((-change).clip(lower=0.0), length)
    rsi = 100.0 - 100.0 / (1.0 + up / down.replace(0.0, np.nan))
    rsi = rsi.where(up != 0.0, 0.0).where(down != 0.0, 100.0)
    return rsi.where(change.notna().cumsum() > 0)


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.where(prev.notna(), high - low)


def _dmi(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14):
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0).where(up.notna())
    minus_dm = down.where((down > up) & (down > 0), 0.0).where(down.notna())
    # DMI uses ta.tr (missing without a previous close); ATR uses ta.tr(true).
    trur = _rma(_true_range(high, low, close).where(close.shift(1).notna()), length)
    plus = (100.0 * _rma(plus_dm, length) / trur.replace(0.0, np.nan)).ffill()
    minus = (100.0 * _rma(minus_dm, length) / trur.replace(0.0, np.nan)).ffill()
    total = (plus + minus).replace(0.0, 1.0)
    adx = 100.0 * _rma((plus - minus).abs() / total, length)
    return plus, minus, adx


def _session_vwap(hlc3: pd.Series, volume: pd.Series) -> pd.Series:
    day = pd.Series(hlc3.index.normalize(), index=hlc3.index)
    pv = (hlc3 * volume).groupby(day).cumsum()
    vol = volume.groupby(day).cumsum()
    return (pv / vol.replace(0.0, np.nan)).astype(float)


def _bucket_prev_value(close: pd.Series, minutes: int, fn) -> pd.Series:
    """Value of fn over `minutes` buckets, taken from the bucket BEFORE each bar's own.

    This is request.security(tf, fn(close[1]), lookahead_on): at any bar, only buckets
    that have fully closed are read.
    """
    bucket = close.index.floor(f"{minutes}min")
    series = close.groupby(bucket).last()
    value = fn(series).shift(1)
    return pd.Series(value.reindex(bucket).to_numpy(), index=close.index)


def _timeframe_minutes(timeframe: str, index: pd.DatetimeIndex) -> int:
    text = str(timeframe or "").lower().replace("min", "").replace("m", "").strip()
    try:
        return max(1, int(float(text)))
    except ValueError:
        if len(index) > 1:
            step = pd.Series(index).diff().dropna().median()
            return max(1, int(step.total_seconds() // 60))
        return 1


def _hhmm(text: str) -> int:
    hh, mm = str(text).split(":")[:2]
    return int(hh) * 60 + int(mm)


def _session_clock(index, tf_min: int) -> Tuple[np.ndarray, np.ndarray]:
    """Minute-of-day at each bar's CLOSE, and whether the bar is its session's last."""
    n = len(index)
    if not isinstance(index, pd.DatetimeIndex) or n == 0:
        return np.zeros(n), np.zeros(n, dtype=bool)
    close_min = np.asarray(index.hour * 60 + index.minute, dtype=float) + tf_min
    day = np.asarray(index.normalize())
    last = np.zeros(n, dtype=bool)
    last[:-1] = day[1:] != day[:-1]
    return close_min, last


def _shift(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full(a.shape, np.nan, dtype=float)
    if k < len(a):
        out[k:] = a[: len(a) - k]
    return out


# ---------------------------------------------------------------------------
# Indicators, patterns, scores (Sections 3-5)
# ---------------------------------------------------------------------------

def compute_features(
    df: pd.DataFrame,
    timeframe: str = "",
    params: Optional[S1Params] = None,
    df_5min: Optional[pd.DataFrame] = None,
    df_15min: Optional[pd.DataFrame] = None,
) -> Dict[str, np.ndarray]:
    """Pine values on the selected chart type; all supplied frames are raw OHLCV."""
    p = params or S1Params()
    chart = s1_candle_frame(df, p.candle_type)
    o = chart["open"].astype(float)
    h = chart["high"].astype(float)
    l = chart["low"].astype(float)
    c = chart["close"].astype(float)
    v = df["volume"].astype(float).fillna(0.0) if "volume" in df.columns else pd.Series(0.0, index=df.index)
    tf_min = _timeframe_minutes(timeframe, df.index)

    ema9, ema13, ema21 = _ema(c, 9), _ema(c, 13), _ema(c, 21)
    ema50, ema200 = _ema(c, 50), _ema(c, 200)
    vwap = _session_vwap((h + l + c) / 3.0, v)
    atr = _rma(_true_range(h, l, c), 14)
    atr_ma = atr.rolling(50).mean()
    atr_exp = (atr / atr_ma).where(atr_ma > 0, 1.0)
    rsi = _rsi(c, 14)
    macd = _ema(c, 12) - _ema(c, 26)
    macd_sig = _ema(macd, 9)
    macd_hist = macd - macd_sig
    di_plus, di_minus, adx = _dmi(h, l, c, 14)
    vol_sma = v.rolling(20).mean()
    rel_vol = (v / vol_sma).where(vol_sma > 0, 1.0)
    vol_spike = (v > vol_sma * 1.5).fillna(False)
    vol_above = rel_vol > 1.2
    rng = h - l
    body_pct = ((c - l) / rng).where(rng > 0, 0.5)

    if isinstance(c.index, pd.DatetimeIndex):
        raw15 = df if tf_min == 15 else df_15min
        if raw15 is None or not len(raw15):
            raw15 = _raw_timeframe_frame(df, tf_min, 15)
        chart15 = _target_candle_frame(raw15, p.candle_type)
        ema9_15 = _confirmed_tf_value(c.index, chart15, 15, lambda s: _ema(s, 9))
        ema21_15 = _confirmed_tf_value(c.index, chart15, 15, lambda s: _ema(s, 21))
    else:
        ema9_15 = _ema(c, 9).shift(1)
        ema21_15 = _ema(c, 21).shift(1)
    ema9_15_slope = ema9_15 - ema9_15.shift(3).fillna(0.0)
    if tf_min <= 5 and isinstance(c.index, pd.DatetimeIndex):
        raw5 = df if tf_min == 5 else df_5min
        if raw5 is None or not len(raw5):
            raw5 = _raw_timeframe_frame(df, tf_min, 5)
        rsi5m = _confirmed_tf_value(c.index, _target_candle_frame(raw5, p.candle_type), 5, _rsi)
    elif df_5min is not None and len(df_5min) and isinstance(c.index, pd.DatetimeIndex):
        rsi5m = _lower_tf_rsi(c.index, tf_min, _target_candle_frame(df_5min, p.candle_type))
    else:
        raise ValueError("S1 requires actual 5min candles for charts above 5min")

    f = {k: np.asarray(s, dtype=float) for k, s in {
        "open": o, "high": h, "low": l, "close": c, "volume": v,
        "ema9": ema9, "ema13": ema13, "ema21": ema21, "ema50": ema50, "ema200": ema200,
        "vwap": vwap, "atr": atr, "atr_exp": atr_exp, "rsi": rsi,
        "macd": macd, "macd_sig": macd_sig, "macd_hist": macd_hist,
        "di_plus": di_plus, "di_minus": di_minus, "adx": adx,
        "rel_vol": rel_vol, "body_pct": body_pct,
        "ema9_15": ema9_15, "ema21_15": ema21_15, "ema9_15_slope": ema9_15_slope, "rsi5m": rsi5m,
    }.items()}
    f["close_min"], f["last_of_session"] = _session_clock(df.index, tf_min)
    f["vol_spike"] = np.asarray(vol_spike, dtype=bool)
    f["vol_above"] = np.asarray(vol_above, dtype=bool)
    f["rel_vol"] = np.asarray(rel_vol.fillna(1.0), dtype=float)
    f["high_vol"] = f["atr_exp"] > 1.25
    adx_a = f["adx"]
    f["bull_trend"] = (f["close"] > f["ema50"]) & (f["ema50"] > f["ema200"]) & (adx_a > 20.0)
    f["bear_trend"] = (f["close"] < f["ema50"]) & (f["ema50"] < f["ema200"]) & (adx_a > 20.0)
    f.update(_patterns(f))
    f.update(_scores(f, p))
    return f


def _lower_tf_rsi(index: pd.DatetimeIndex, tf_min: int, df_5min: pd.DataFrame) -> pd.Series:
    """Pine historical lookahead_on: first intrabar, evaluated with close[1].

    Selecting the last intrabar incorrectly used later 5m prices for a 15m signal.
    Convert aware timestamps to exchange time before aligning with naive IST bars.
    """
    c5 = df_5min["close"].astype(float).copy()
    c5.index = _exchange_index(c5.index)
    c5 = c5[~c5.index.duplicated(keep="last")].sort_index()
    chart_index = _exchange_index(index)
    value = _rsi(c5, 14).shift(1)
    pos = value.index.searchsorted(chart_index, side="left")
    out = np.full(len(index), np.nan)
    ok = pos < len(value)
    if len(value):
        picked = value.index[np.minimum(pos, len(value) - 1)]
        ok &= np.asarray(picked < chart_index + pd.Timedelta(minutes=tf_min))
        out[ok] = value.to_numpy()[pos[ok]]
    return pd.Series(out, index=index)


def _patterns(f: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    o, h, l, c, atr = f["open"], f["high"], f["low"], f["close"], np.nan_to_num(f["atr"])
    n = len(c)
    bar = np.arange(n)
    o1, o2, o3, o4 = (_shift(o, k) for k in (1, 2, 3, 4))
    h1, h2, h3, h4 = (_shift(h, k) for k in (1, 2, 3, 4))
    l1, l2, l3, l4 = (_shift(l, k) for k in (1, 2, 3, 4))
    c1, c2, c3, c4 = (_shift(c, k) for k in (1, 2, 3, 4))

    body0, rng0 = np.abs(c - o), h - l
    uw0, lw0 = h - np.maximum(c, o), np.minimum(c, o) - l
    bull0, bear0 = c > o, c < o
    doji0 = (rng0 > 0) & (body0 <= rng0 * 0.10)
    body1, rng1 = np.abs(c1 - o1), h1 - l1
    uw1, lw1 = h1 - np.maximum(c1, o1), np.minimum(c1, o1) - l1
    bull1, bear1 = c1 > o1, c1 < o1
    doji1 = (rng1 > 0) & (body1 <= rng1 * 0.10)
    body2 = np.abs(c2 - o2)
    uw2, lw2 = h2 - np.maximum(c2, o2), np.minimum(c2, o2) - l2
    bull2, bear2 = c2 > o2, c2 < o2
    min_body, big_body = atr * 0.15, atr * 0.55

    hh20 = pd.Series(h1).rolling(20).max().to_numpy()
    ll20 = pd.Series(l1).rolling(20).min().to_numpy()
    near_res = h >= hh20 * 0.997
    near_sup = l <= ll20 * 1.003
    prior_dn1 = (bar >= 2) & bear1
    prior_dn2 = (bar >= 3) & bear1 & bear2
    prior_up1 = (bar >= 2) & bull1
    prior_up2 = (bar >= 3) & bull1 & bull2
    ema21 = f["ema21"]
    in_dn = (c < ema21) & prior_dn1
    in_up = (c > ema21) & prior_up1
    bull_rev_ctx = prior_dn2 | (prior_dn1 & (c < ema21)) | (near_sup & prior_dn1)
    bear_rev_ctx = prior_up2 | (prior_up1 & (c > ema21)) | (near_res & prior_up1)
    size_ok = rng0 >= atr * 0.25
    vol_ok = f["vol_above"] | f["vol_spike"]
    b2, b3, b5 = bar >= 2, bar >= 3, bar >= 5

    # Single bar
    dragonfly = doji0 & (rng0 >= min_body) & (lw0 >= rng0 * 0.65) & (uw0 <= rng0 * 0.10)
    gravestone = doji0 & (rng0 >= min_body) & (uw0 >= rng0 * 0.65) & (lw0 <= rng0 * 0.10)
    hammer = (body0 >= min_body) & (lw0 >= body0 * 2.0) & (uw0 <= body0 * 0.35)
    inv_hammer = (body0 >= min_body) & (uw0 >= body0 * 2.0) & (lw0 <= body0 * 0.35)
    bull_maru = bull0 & (body0 >= big_body) & (uw0 <= body0 * 0.05) & (lw0 <= body0 * 0.05)
    bear_maru = bear0 & (body0 >= big_body) & (uw0 <= body0 * 0.05) & (lw0 <= body0 * 0.05)
    bull_belt = bull0 & (body0 >= big_body * 0.65) & (lw0 <= body0 * 0.06)
    bear_belt = bear0 & (body0 >= big_body * 0.65) & (uw0 <= body0 * 0.06)
    # Double bar
    bull_engulf = b2 & bear1 & bull0 & (body1 >= min_body) & (body0 >= min_body) & (o <= c1) & (c >= o1)
    bear_engulf = b2 & bull1 & bear0 & (body1 >= min_body) & (body0 >= min_body) & (o >= c1) & (c <= o1)
    bull_harami = b2 & bear1 & bull0 & (body1 >= big_body * 0.65) & (body0 < body1 * 0.55) & (o > c1) & (c < o1)
    bear_harami = b2 & bull1 & bear0 & (body1 >= big_body * 0.65) & (body0 < body1 * 0.55) & (o < c1) & (c > o1)
    bull_harami_x = b2 & bear1 & doji0 & (body1 >= big_body * 0.65) & (h <= o1) & (l >= c1)
    bear_harami_x = b2 & bull1 & doji0 & (body1 >= big_body * 0.65) & (h <= c1) & (l >= o1)
    piercing = b2 & bear1 & bull0 & (body1 >= big_body * 0.55) & (o < c1) & (c > c1 + (o1 - c1) * 0.50) & (c < o1)
    dark_cloud = b2 & bull1 & bear0 & (body1 >= big_body * 0.55) & (o > c1) & (c < o1 + (c1 - o1) * 0.50) & (c > o1)
    tweez_bot = b2 & bear1 & bull0 & (np.abs(l - l1) <= atr * 0.12)
    tweez_top = b2 & bull1 & bear0 & (np.abs(h - h1) <= atr * 0.12)
    big_pair = (body1 >= big_body * 0.55) & (body0 >= big_body * 0.55)
    bull_kicker = b2 & bear1 & bull0 & big_pair & (o >= o1)
    bear_kicker = b2 & bull1 & bear0 & big_pair & (o <= o1)
    meet = np.abs(c - c1) <= atr * 0.08
    bull_meet = b2 & bear1 & bull0 & big_pair & meet
    bear_meet = b2 & bull1 & bear0 & big_pair & meet
    # Triple bar
    star_bodies = (body2 >= big_body * 0.55) & (body0 >= big_body * 0.40)
    morning = b3 & bear2 & (body1 <= body2 * 0.45) & bull0 & star_bodies & (c >= c2 + (o2 - c2) * 0.50)
    evening = b3 & bull2 & (body1 <= body2 * 0.45) & bear0 & star_bodies & (c <= o2 + (c2 - o2) * 0.50)
    morning_doji = b3 & bear2 & doji1 & bull0 & star_bodies & (c >= c2 + (o2 - c2) * 0.50)
    evening_doji = b3 & bull2 & doji1 & bear0 & star_bodies & (c <= o2 + (c2 - o2) * 0.50)
    bodies3 = (body0 >= big_body * 0.40) & (body1 >= big_body * 0.40) & (body2 >= big_body * 0.40)
    soldiers = (b3 & bull0 & bull1 & bull2 & (c > c1) & (c1 > c2) & bodies3
                & (uw0 <= body0 * 0.35) & (uw1 <= body1 * 0.35) & (uw2 <= body2 * 0.35) & (o > o1) & (o1 > o2))
    crows = (b3 & bear0 & bear1 & bear2 & (c < c1) & (c1 < c2) & bodies3
             & (lw0 <= body0 * 0.35) & (lw1 <= body1 * 0.35) & (lw2 <= body2 * 0.35) & (o < o1) & (o1 < o2))
    inside_up = b3 & bear2 & bull1 & bull0 & (body2 >= big_body * 0.55) & (o1 >= c2) & (c1 <= o2) & (c > o2)
    inside_dn = b3 & bull2 & bear1 & bear0 & (body2 >= big_body * 0.55) & (o1 <= c2) & (c1 >= o2) & (c < o2)
    outside_up = b3 & bear2 & bull1 & bull0 & (body2 >= min_body) & (body1 >= body2 * 0.90) & (o1 <= c2) & (c1 >= o2) & (c > c1)
    outside_dn = b3 & bull2 & bear1 & bear0 & (body2 >= min_body) & (body1 >= body2 * 0.90) & (o1 >= c2) & (c1 <= o2) & (c < c1)
    baby_bodies = (body2 >= big_body * 0.50) & (body0 >= big_body * 0.35)
    bull_baby = b3 & bear2 & doji1 & bull0 & baby_bodies & (h1 <= np.minimum(o2, c2)) & (l >= np.maximum(o1, c1))
    bear_baby = b3 & bull2 & doji1 & bear0 & baby_bodies & (l1 >= np.maximum(o2, c2)) & (h <= np.minimum(o1, c1))
    # Continuation
    rising3 = (b5 & (c4 > o4) & ((c4 - o4) >= big_body * 0.50) & (c3 < o3) & (c2 < o2) & (c1 < o1)
               & (h3 < h4) & (h2 < h4) & (h1 < h4) & (l3 > l4) & (l2 > l4) & (l1 > l4) & bull0 & (c > c4))
    falling3 = (b5 & (c4 < o4) & ((o4 - c4) >= big_body * 0.50) & (c3 > o3) & (c2 > o2) & (c1 > o1)
                & (l3 > l4) & (l2 > l4) & (l1 > l4) & (h3 < h4) & (h2 < h4) & (h1 < h4) & bear0 & (c < c4))
    up_tasuki = b3 & bull2 & bull1 & bear0 & (o1 > c2) & (o < c1) & (c > c2) & (c < o1)
    dn_tasuki = b3 & bear2 & bear1 & bull0 & (o1 < c2) & (o > c1) & (c < c2) & (c > o1)

    raw_bull_rev = (dragonfly | (hammer & in_dn) | (inv_hammer & in_dn) | bull_belt | bull_maru
                    | bull_engulf | bull_harami | bull_harami_x | piercing | tweez_bot | bull_kicker | bull_meet
                    | morning | morning_doji | soldiers | inside_up | outside_up | bull_baby)
    raw_bear_rev = (gravestone | (hammer & in_up) | (inv_hammer & in_up) | bear_belt | bear_maru
                    | bear_engulf | bear_harami | bear_harami_x | dark_cloud | tweez_top | bear_kicker | bear_meet
                    | evening | evening_doji | crows | inside_dn | outside_dn | bear_baby)
    valid_bull = ((raw_bull_rev & size_ok & vol_ok & bull_rev_ctx)
                  | ((rising3 | up_tasuki) & size_ok & vol_ok & f["bull_trend"]))
    valid_bear = ((raw_bear_rev & size_ok & vol_ok & bear_rev_ctx)
                  | ((falling3 | dn_tasuki) & size_ok & vol_ok & f["bear_trend"]))
    strong_bull = valid_bull & (bull_engulf | soldiers | morning | morning_doji | bull_baby
                                | inside_up | outside_up | bull_kicker | rising3)
    strong_bear = valid_bear & (bear_engulf | crows | evening | evening_doji | bear_baby
                                | inside_dn | outside_dn | bear_kicker | falling3)
    return {"pat_valid_bull": valid_bull, "pat_valid_bear": valid_bear,
            "pat_strong_bull": strong_bull, "pat_strong_bear": strong_bear}


def _scores(f: Dict[str, np.ndarray], p: S1Params) -> Dict[str, np.ndarray]:
    c, adx = f["close"], f["adx"]
    with np.errstate(invalid="ignore"):
        adx_pts = np.where(adx > 25.0, 20.0, 10.0 + (adx - p.min_adx) * (10.0 / max(1.0, 25.0 - p.min_adx)))
        adx_ok = adx >= p.min_adx
        macd, sig, hist = f["macd"], f["macd_sig"], f["macd_hist"]
        hist1 = _shift(hist, 1)
        acc_bull, acc_bear = hist > hist1, hist < hist1
        vwap = f["vwap"]
        dev = np.where(vwap > 0, np.abs(c - vwap) / vwap, 0.0)
        vwap_pts = np.where(dev > 0.003, 14.0, 7.0)
        e9, e21, e50 = f["ema9"], f["ema21"], f["ema50"]
        vs, va, bp = f["vol_spike"], f["vol_above"], f["body_pct"]
        rsi = f["rsi"]
        e9h, e21h, slope = f["ema9_15"], f["ema21_15"], f["ema9_15_slope"]
        hh5 = _shift(pd.Series(f["high"]).rolling(5).max().to_numpy(), 1)
        ll5 = _shift(pd.Series(f["low"]).rolling(5).min().to_numpy(), 1)
        pat = 1.0 if p.use_pattern_score else 0.0

        bull = (np.where(adx_ok & (f["di_plus"] > f["di_minus"]), adx_pts, 0.0)
                + np.where((macd > sig) & acc_bull, 18.0, np.where(macd > sig, 10.0, 0.0))
                + np.where(c > vwap, vwap_pts, 0.0)
                + np.where((e9 > e21) & (e21 > e50), 14.0, np.where(e9 > e21, 7.0, 0.0))
                + np.where(vs & (bp > 0.6), 12.0, np.where(va & (bp > 0.5), 6.0, 0.0))
                + np.clip(np.nan_to_num((rsi - 50.0) * 0.5), 0.0, 10.0)
                + (np.where((e9h > e21h) & (slope > 0), 8.0, 0.0) if p.use_htf else 4.0)
                + np.where(f["rsi5m"] > 50.0, 4.0, 0.0)
                + np.where(c > hh5, 4.0, 0.0)
                + pat * np.where(f["pat_strong_bull"], 15.0, np.where(f["pat_valid_bull"], 10.0, 0.0)))
        bear = (np.where(adx_ok & (f["di_minus"] > f["di_plus"]), adx_pts, 0.0)
                + np.where((macd < sig) & acc_bear, 18.0, np.where(macd < sig, 10.0, 0.0))
                + np.where(c < vwap, vwap_pts, 0.0)
                + np.where((e9 < e21) & (e21 < e50), 14.0, np.where(e9 < e21, 7.0, 0.0))
                + np.where(vs & (bp < 0.4), 12.0, np.where(va & (bp < 0.5), 6.0, 0.0))
                + np.clip(np.nan_to_num((50.0 - rsi) * 0.5), 0.0, 10.0)
                + (np.where((e9h < e21h) & (slope < 0), 8.0, 0.0) if p.use_htf else 4.0)
                + np.where(f["rsi5m"] < 50.0, 4.0, 0.0)
                + np.where(c < ll5, 4.0, 0.0)
                + pat * np.where(f["pat_strong_bear"], 15.0, np.where(f["pat_valid_bear"], 10.0, 0.0)))
    return {"bull_score": np.minimum(bull, 100.0), "bear_score": np.minimum(bear, 100.0),
            "macd_acc_bull": acc_bull, "macd_acc_bear": acc_bear}


def _setup_reason(f: Dict[str, np.ndarray], i: int, is_buy: bool) -> str:
    c, o, h, l = f["close"], f["open"], f["high"], f["low"]
    lo = max(0, i - 20)
    spike = bool(f["vol_spike"][i])
    rsi = f["rsi"][i]
    macd, sig = f["macd"], f["macd_sig"]
    cross_up = i > 0 and macd[i] > sig[i] and macd[i - 1] <= sig[i - 1]
    cross_dn = i > 0 and macd[i] < sig[i] and macd[i - 1] >= sig[i - 1]
    if is_buy:
        if i >= 20 and h[i] > np.max(h[lo:i]) and spike and c[i] > o[i]:
            return "Breakout"
        if f["bull_trend"][i] and f["ema50"][i] < c[i] < f["ema21"][i] and 40.0 < rsi < 60.0:
            return "Pullback"
        if cross_up and spike and c[i] > f["ema21"][i]:
            return "Momentum"
        if i > 0 and rsi < 35.0 and c[i] > o[i] and c[i] > h[i - 1] and macd[i] > sig[i]:
            return "Reversal"
        return "Pattern" if f["pat_valid_bull"][i] else "Trend"
    if i >= 20 and l[i] < np.min(l[lo:i]) and spike and c[i] < o[i]:
        return "Breakdown"
    if f["bear_trend"][i] and f["ema21"][i] < c[i] < f["ema50"][i] and 40.0 < rsi < 60.0:
        return "Pullback"
    if cross_dn and spike and c[i] < f["ema21"][i]:
        return "Momentum"
    if i > 0 and rsi > 65.0 and c[i] < o[i] and c[i] < l[i - 1] and macd[i] < sig[i]:
        return "Reversal"
    return "Pattern" if f["pat_valid_bear"][i] else "Trend"


# ---------------------------------------------------------------------------
# Gates, entry, trailing stop, exits (Sections 7-13)
# ---------------------------------------------------------------------------

@dataclass
class S1Trade:
    direction: int
    entry_bar: int
    entry_price: float
    sl1: float
    sl2: float
    targets: Tuple[float, float, float]
    setup: str
    bull_score: float
    bear_score: float
    signal_close: float = float("nan")
    sl_pct: float = float("nan")
    exit_bar: int = -1
    exit_price: float = float("nan")
    exit_reason: str = ""

    @property
    def pnl_points(self) -> float:
        return (self.exit_price - self.entry_price) * self.direction


@dataclass
class S1Run:
    features: Dict[str, np.ndarray]
    trades: List[S1Trade]
    positions: np.ndarray
    stops: np.ndarray


def _pine_levels(p: S1Params, ep: float, d: int):
    """calcSL / calcTargets exactly as the Pine computes them, with the chart's inputs."""
    sl1_pct, chart_sl2 = p.chart_sl1_pct, p.chart_sl2_pct
    sl1 = ep * (1.0 - sl1_pct / 100.0) if d == 1 else ep * (1.0 + sl1_pct / 100.0)
    sl2_pct = chart_sl2 if chart_sl2 > sl1_pct else max(sl1_pct + 0.05, chart_sl2)
    sl2 = ep * (1.0 - sl2_pct / 100.0) if d == 1 else ep * (1.0 + sl2_pct / 100.0)
    rk = abs(ep - sl1)
    targets = tuple(ep + rk * r if d == 1 else ep - rk * r for r in (p.t1_r, p.t2_r, p.t3_r))
    return sl1, sl2, targets, rk


def drop_off_session_bars(df: Optional[pd.DataFrame], session_end: str = "15:15",
                          session_start: str = "09:15") -> Optional[pd.DataFrame]:
    """Keep only bars that START inside [session_start, session_end), the bars the user's
    TradingView index chart shows. The feed also keeps 09:05/09:10 pre-open prints and
    15:15-15:29 bars that are frozen and then jump to the post-close price with bulk
    volume; both would distort every indicator and the Heikin Ashi chain."""
    if df is None or not len(df) or not isinstance(df.index, pd.DatetimeIndex):
        return df
    start, end = _hhmm(session_start), _hhmm(session_end)
    index = df.index.tz_convert("Asia/Kolkata") if df.index.tz is not None else df.index
    minutes = np.asarray(index.hour * 60 + index.minute)
    keep = (minutes >= start) & (minutes < end)
    return df if keep.all() else df[keep]


def _stop_distance(p: S1Params, f: Dict[str, np.ndarray], i: int, ep: float, d: int) -> float:
    """The live-order SL1 distance (sl_mode, clamped to [sl_min_pct, sl_max_pct])."""
    if p.sl_mode == "fixed":
        return ep * p.sl1_pct / 100.0
    if p.sl_mode == "atr":
        raw = p.sl_atr_mult * float(np.nan_to_num(f["atr"][i]))
    else:  # anchor: beyond the extreme of the last 3 bars (the Pine's unused anchorLow/High)
        lo = max(0, i - 2)
        anchor = float(np.min(f["low"][lo:i + 1])) if d == 1 else float(np.max(f["high"][lo:i + 1]))
        raw = (ep - anchor) * d
    return float(np.clip(raw, ep * p.sl_min_pct / 100.0, ep * p.sl_max_pct / 100.0))


def _execution_features(df: pd.DataFrame, features: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    """Real candle prices/ATR for executable fills and dynamic market stops."""
    result = dict(features)
    for key in ("open", "high", "low", "close"):
        result[key] = df[key].to_numpy(dtype=float)
    result["atr"] = _rma(_true_range(df["high"], df["low"], df["close"]), 14).to_numpy()
    return result


def simulate(df: pd.DataFrame, timeframe: str = "", params: Optional[S1Params] = None,
             features: Optional[Dict[str, np.ndarray]] = None) -> S1Run:
    p = params or S1Params()
    pine = p.entry_mode == "pine"
    f = features if features is not None else compute_features(df, timeframe, p)
    n = len(f["close"])
    o, h, l, c = f["open"], f["high"], f["low"], f["close"]
    execution = f if pine else _execution_features(df, f)
    xo, xh, xl, xc = (execution[key] for key in ("open", "high", "low", "close"))
    atr, adx, rsi = f["atr"], f["adx"], f["rsi"]
    ema9, ema13 = f["ema9"], f["ema13"]
    macd, sig = f["macd"], f["macd_sig"]
    acc_bull, acc_bear = f["macd_acc_bull"], f["macd_acc_bear"]
    bull_s, bear_s = f["bull_score"], f["bear_score"]
    body_pct, high_vol = f["body_pct"], f["high_vol"]
    vol_ok = f["vol_above"] | f["vol_spike"]
    htf_bull = np.ones(n, bool) if not p.use_htf else (f["ema9_15"] > f["ema21_15"]) & (f["ema9_15_slope"] > 0)
    htf_bear = np.ones(n, bool) if not p.use_htf else (f["ema9_15"] < f["ema21_15"]) & (f["ema9_15_slope"] < 0)
    close_min, last_of_session = f["close_min"], f["last_of_session"]
    cutoff_min, exit_min = _hhmm(p.entry_cutoff), _hhmm(p.session_exit)

    positions = np.zeros(n, dtype=int)
    stops = np.full(n, np.nan)
    trades: List[S1Trade] = []
    trade: Optional[S1Trade] = None
    tsl = peak = trough = 0.0
    t1 = t2 = locked = False
    conf_cnt = 0
    last_sig_bar = 0
    last_sig_state = 0
    consec_loss = 0
    pause_until = 0

    for i in range(n):
        # Section 9: the gates read the state the bar opened with.
        ready = (trade is None and i >= p.warmup_bars and not np.isnan(atr[i]) and not np.isnan(adx[i])
                 and (i - last_sig_bar) >= p.signal_cooldown and i > pause_until
                 and not (p.session_rules and (close_min[i] > cutoff_min or last_of_session[i])))
        if ready:
            dyn_min = p.min_score + (16.0 if consec_loss >= 3 else 12.0 if consec_loss == 2 else 8.0 if consec_loss == 1 else 0.0)
            body_ok = abs(c[i] - o[i]) >= atr[i] * 0.15
            gates = adx[i] >= p.min_adx and body_ok and vol_ok[i]
            buy = bool(gates and htf_bull[i] and bull_s[i] >= dyn_min and (bull_s[i] - bear_s[i]) >= p.conflict_margin
                       and bear_s[i] < 45.0 and body_pct[i] > 0.5 and last_sig_state <= 0)
            sell = bool(gates and not buy and htf_bear[i] and bear_s[i] >= dyn_min and (bear_s[i] - bull_s[i]) >= p.conflict_margin
                        and bull_s[i] < 45.0 and body_pct[i] < 0.5 and last_sig_state >= 0)
            if buy or sell:
                # Section 10: the Pine books the signal bar's OPEN; "close" mode its close.
                d = 1 if buy else -1
                ep = o[i] if pine else xc[i]
                if pine:
                    sl1, sl2, targets, dist = _pine_levels(p, ep, d)
                else:
                    dist = _stop_distance(p, execution, i, ep, d)
                    sl1 = ep - d * dist
                    sl2_pct = max(p.sl2_pct, dist / ep * 100.0 + 0.05)
                    sl2 = ep * (1.0 - d * sl2_pct / 100.0)
                    targets = tuple(ep + d * dist * r for r in (p.t1_r, p.t2_r, p.t3_r))
                trade = S1Trade(d, i, float(ep), float(sl1), float(sl2), targets, _setup_reason(f, i, buy),
                                float(bull_s[i]), float(bear_s[i]), signal_close=float(c[i]),
                                sl_pct=dist / ep * 100.0)
                last_sig_bar, last_sig_state = i, d
                tsl, peak, trough = sl1, ep, ep
                t1 = t2 = locked = False
                conf_cnt = 0
                if not pine:
                    positions[i], stops[i] = d, tsl
                    continue
        if trade is None:
            continue

        d, ep, sl1, sl2 = trade.direction, trade.entry_price, trade.sl1, trade.sl2
        tp1, tp2, tp3 = trade.targets
        risk = abs(ep - sl1)
        # Section 11: targets count only on bars that did not touch SL1.
        if d == 1:
            peak = max(peak, xh[i])
            if xl[i] > sl1:
                t1 = t1 or xh[i] >= tp1
                t2 = t2 or xh[i] >= tp2
        else:
            trough = min(trough, xl[i])
            if xh[i] < sl1:
                t1 = t1 or xl[i] <= tp1
                t2 = t2 or xl[i] <= tp2
        # Section 12: lock at T1, then trail from trail_start_r.
        if p.use_trail and risk > 0:
            if p.lock_at_t1 and t1 and not locked:
                tsl = max(tsl, ep + risk * 0.4) if d == 1 else min(tsl, ep - risk * 0.4)
                locked = True
            if (xc[i] - ep) * d / risk >= p.trail_start_r:
                loose = p.trail_mult * 1.5 if (high_vol[i] and not t2) else p.trail_mult * 1.3 if t2 else p.trail_mult
                if d == 1:
                    tsl = max(tsl, peak - execution["atr"][i] * loose)
                else:
                    tsl = min(tsl, trough + execution["atr"][i] * loose)
        # Section 13: momentum-exit counter.
        r1 = rsi[i - 1] if i > 0 else np.nan
        if d == 1:
            met = int(macd[i] < sig[i] and acc_bear[i]) + int(c[i] < ema13[i]) + int(rsi[i] < 45.0 and rsi[i] < r1)
            conf_cnt = conf_cnt + 1 if met >= (3 if t1 else 2) else (max(0, conf_cnt - 1) if c[i] > ema9[i] else conf_cnt)
            momentum = conf_cnt >= p.exit_conf_bars and not (h[i] > tp2 and not t2)
        else:
            met = int(macd[i] > sig[i] and acc_bull[i]) + int(c[i] > ema13[i]) + int(rsi[i] > 55.0 and rsi[i] > r1)
            conf_cnt = conf_cnt + 1 if met >= (3 if t1 else 2) else (max(0, conf_cnt - 1) if c[i] < ema9[i] else conf_cnt)
            momentum = conf_cnt >= p.exit_conf_bars and not (l[i] < tp2 and not t2)

        reason, price = "", float("nan")
        if (pine or p.use_sl2) and ((d == 1 and xo[i] <= sl2) or (d == -1 and xo[i] >= sl2)):
            reason, price = "SL2 GAP (Max Loss)", (sl2 if pine else xo[i])
        elif (d == 1 and xl[i] <= sl1) or (d == -1 and xh[i] >= sl1):
            reason = "Hard SL1"
            price = sl1 if pine else (min(xo[i], sl1) if d == 1 else max(xo[i], sl1))
        elif p.use_trail and ((d == 1 and tsl > sl1 and xl[i] < tsl) or (d == -1 and tsl < sl1 and xh[i] > tsl)):
            reason, price = ("TSL (Profit Locked)" if locked else "TSL"), xc[i]
        elif momentum:
            reason, price = "Momentum Exit", xc[i]
        elif p.session_rules and (close_min[i] >= exit_min or last_of_session[i]):
            reason, price = "Session End", xc[i]

        if reason:
            trade.exit_bar, trade.exit_price, trade.exit_reason = i, float(price), reason
            trades.append(trade)
            if trade.pnl_points > 0:
                consec_loss = 0
            else:
                consec_loss += 1
                if consec_loss >= p.max_consec_loss:
                    pause_until = i + p.circuit_pause
            trade = None
            last_sig_state = 0
            continue
        positions[i], stops[i] = d, tsl

    if trade is not None:
        trades.append(trade)
    return S1Run(f, trades, positions, stops)


def get_s1_signals(
    df: pd.DataFrame,
    instrument: str = "",
    timeframe: str = "",
    params: Optional[S1Params] = None,
    df_5min: Optional[pd.DataFrame] = None,
    features: Optional[Dict[str, np.ndarray]] = None,
    df_15min: Optional[pd.DataFrame] = None,
) -> Tuple[List[UTBotSignal], np.ndarray, np.ndarray]:
    """Entry signals, per-bar position (+1/-1/0) and per-bar S1 stop (NaN when flat).

    Each signal carries executable levels measured from the raw market CLOSE. Separate
    s1_chart_* fields preserve the indicator's synthetic prices, plus the exit the S1 state
    machine gave that trade once it has happened (s1_exit_*), so a live position can
    follow the chart's EXIT label.
    """
    p = params or params_from_settings(instrument=instrument)
    f = features if features is not None else compute_features(df, timeframe, p, df_5min=df_5min, df_15min=df_15min)
    run = simulate(df, timeframe, p, features=f)
    execution = _execution_features(df, f) if p.sl_mode != "fixed" else f
    index = df.index
    signals: List[UTBotSignal] = []
    for t in run.trades:
        i = t.entry_bar
        ts = index[i]
        # Chart OHLC can be synthetic. Orders and their risk levels use market prices.
        close = float(df["close"].iloc[i])
        dist = _stop_distance(p, execution, i, close, t.direction)
        fixed_pct = {"5min": p.sl1_pct_5min, "15min": p.sl1_pct_15min}.get(str(timeframe or ""), 0.0)
        if fixed_pct and fixed_pct > 0:
            dist = close * float(fixed_pct) / 100.0
        sl1 = close - t.direction * dist
        sig = UTBotSignal(
            timestamp=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
            signal_type="BUY" if t.direction == 1 else "SELL",
            price=close,
            trailing_stop=float(sl1),
            stop_distance=float(dist),
            atr_value=float(np.nan_to_num(f["atr"][i])),
            adx_value=float(np.nan_to_num(f["adx"][i])),
            position_state=t.direction,
            bar_index=i,
            instrument=instrument,
            timeframe=timeframe,
            # Shared grading inspects the actual signal bar for wick rejection.
            # Preserve market OHLC separately from the transformed chart evidence.
            raw_candle={key: float(df[key].iloc[i]) for key in ("open", "high", "low", "close")},
            source_engine="s1",
        )
        exit_ts = index[t.exit_bar] if t.exit_bar >= 0 else None
        extras = {
            "source_engines": ["s1"],
            "s1_score": t.bull_score if t.direction == 1 else t.bear_score,
            "s1_bull_score": t.bull_score,
            "s1_bear_score": t.bear_score,
            "s1_setup": t.setup,
            "s1_sl_pct": float(dist / close * 100.0),
            "s1_sl1": float(sl1),
            "s1_targets": [float(close + t.direction * dist * r) for r in (p.t1_r, p.t2_r, p.t3_r)],
            "s1_chart_entry": float(t.entry_price),
            "s1_chart_signal_close": float(t.signal_close),
            "s1_chart_sl1": float(t.sl1),
            "s1_chart_sl2": float(t.sl2),
            "s1_chart_targets": [float(v) for v in t.targets],
            "s1_exit_reason": t.exit_reason,
            "s1_exit_time": exit_ts.to_pydatetime() if hasattr(exit_ts, "to_pydatetime") else exit_ts,
            "s1_exit_price": float(t.exit_price) if t.exit_bar >= 0 else None,
            # How much the market was moving on the signal bar (S1 lane weak-target rule).
            "s1_rel_volume": float(np.nan_to_num(f["rel_vol"][i], nan=1.0)),
            "s1_atr_expansion": float(np.nan_to_num(f["atr_exp"][i], nan=1.0)),
        }
        for k, v in extras.items():
            setattr(sig, k, v)
        signals.append(sig)
    return signals, run.positions, run.stops
