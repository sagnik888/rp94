"""Offline S1 lane study (2026-09-25): every stored session, 4 indices x 1m/5m/15m.

Signals come from the live S1 engine exactly as _process_s1 builds them (session bars only,
Heikin Ashi, live SL). Each signal is then managed on 1-minute bars with the S1 lane rules
as shipped on 2026-09-25, in INDEX space, results in R (R = the S1 index stop distance):

  entry      first 1m open after the signal candle closes; S1 cutoffs (15m 14:50, 5m/1m 15:10)
  1m         only with a volume burst on the signal bar (S1 vol_spike proxy for the system's)
  stop       S1 SL1; locked at entry + 0.7R once 2R (T1) prints
  target     1R, 0.7R when the signal bar is weak or a later closed bar goes quiet (trend
             not with the trade); not a hard exit while the S1 trend is with the trade
             (runner), which is closed at 30R
  exits      S1 EXIT label (after its bar closes), trend turned against before 1R, profit
             lock after 1R with a 25/10/0 % giveback by trend, a trail ~1R behind the peak
             (proxy for the premium guard trail), 15:15
  cost       0.07R per trade (Rs.80 against a ~Rs.1,100 NIFTY 1R), reported separately

Limits: no option premiums (index R stands in for premium R), no regime-detector label (the
"clear trend regime is never weak" exemption is not applied), stop/target touched in the
same 1m bar counts as the stop. Descriptive only - one sample, not a fit.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from config.settings import Settings
from engine import s1_lane
from engine.s1_engine import compute_features, drop_off_session_bars, get_s1_signals, params_from_settings

INDICES = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
CONTEXT = {}
TFS = {"1min": 1, "5min": 5, "15min": 15}
COST_R = 0.07
SETTINGS = Settings()


def read(symbol, tf):
    df = pd.read_csv(ROOT / f"data_store/candles/{symbol}_{tf}.csv", index_col="timestamp", parse_dates=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def trend_of(bull, bear, d):
    gap = (bull - bear) * d
    margin = float(SETTINGS.s1_lane_trend_margin)
    return 1 if gap >= margin else -1 if -gap >= margin else 0


def manage(sig, d, tf_min, tf_df, f, pos_of, m1, variant):
    """One S1 trade on 1m bars. Returns dict or None (no entry)."""
    ts = pd.Timestamp(sig.timestamp)
    close_at = ts + pd.Timedelta(minutes=tf_min)
    cutoff = time(14, 50) if tf_min >= 15 else time(15, 10)
    if close_at.time() > cutoff:
        return None
    day = m1[m1.index.normalize() == ts.normalize()]
    path = day[day.index >= close_at]
    path = path[path.index.time < time(15, 15)]
    if path.empty:
        return None
    entry = float(path["open"].iloc[0])
    dist = float(sig.stop_distance) * float(variant.get("stop_mult", 1.0))
    if dist <= 0:
        return None
    weak_entry = s1_lane.weak_move(sig.adx_value, getattr(sig, "s1_rel_volume", None),
                                   getattr(sig, "s1_atr_expansion", None), SETTINGS)
    target_r = variant.get("fixed_target") or (0.7 if (weak_entry and variant.get("weak", True)) else 1.0)
    stop = entry - d * dist
    locked = False
    peak_r = 0.0
    trend = 0
    special = False
    chart_exit = pd.Timestamp(sig.s1_exit_time) if getattr(sig, "s1_exit_time", None) is not None else None
    chart_exit_at = chart_exit + pd.Timedelta(minutes=tf_min) if chart_exit is not None and chart_exit > ts else None
    sig_pos = pos_of.get(ts)
    last_tf_seen = sig_pos
    for t, bar in path.iterrows():
        # closed signal-timeframe bars after the signal bar update trend / quietness
        tf_close_pos = tf_df.index.searchsorted(t - pd.Timedelta(minutes=tf_min), side="right") - 1
        if sig_pos is not None and tf_close_pos > last_tf_seen and tf_close_pos >= 0:
            last_tf_seen = tf_close_pos
            j = tf_close_pos
            if tf_df.index[j] > ts:
                trend = trend_of(f["bull_score"][j], f["bear_score"][j], d) if variant.get("trend", True) else 0
                special = (f["bull_score"][j] - f["bear_score"][j]) * d >= float(SETTINGS.s1_lane_special_trend_margin)
                if (variant.get("weak", True) and not variant.get("fixed_target") and trend <= 0
                        and s1_lane.weak_move(f["adx"][j], f["rel_vol"][j], f["atr_exp"][j], SETTINGS)):
                    target_r = min(target_r, 0.7)
                # trend turned against before 1R
                if variant.get("trend_cut", True) and trend < 0 and peak_r < 1.0:
                    px = float(tf_df["close"].iloc[j])
                    return dict(r=(px - entry) * d / dist, reason="TREND_AGAINST", peak=peak_r)
        if chart_exit_at is not None and t >= chart_exit_at and variant.get("chart_exit", True):
            return dict(r=(float(bar["open"]) - entry) * d / dist, reason="S1_EXIT", peak=peak_r)
        hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
        adverse = lo if d == 1 else hi
        favour = hi if d == 1 else lo
        # stop first (conservative)
        if (adverse - stop) * d <= 0:
            return dict(r=(stop - entry) * d / dist, reason="T1_LOCK" if locked else "SL1", peak=peak_r)
        peak_r = max(peak_r, (favour - entry) * d / dist)
        if not locked and peak_r >= float(SETTINGS.s1_t1_r):
            locked = True
            stop = entry + d * dist * float(SETTINGS.s1_lane_t1_lock_r)
        runner = trend > 0 and variant.get("runner", True)
        cap = float(SETTINGS.s1_lane_runner_max_r) if (special or variant.get("always_30r")) else float(SETTINGS.s1_t3_r)
        if runner and peak_r >= cap:
            return dict(r=cap, reason="RUNNER_MAX", peak=peak_r)
        if not runner and peak_r >= target_r:
            return dict(r=target_r, reason="TARGET", peak=peak_r)
        gain = (cl - entry) * d / dist
        if variant.get("lock", True) and peak_r >= float(variant.get("arm_r", SETTINGS.s1_lane_lock_arm_r)):
            give = 25.0 if trend > 0 else 0.0 if trend < 0 else 10.0
            if gain < peak_r * (1 - give / 100.0):
                return dict(r=gain, reason="PROFIT_LOCK", peak=peak_r)
        if variant.get("trail", True) and peak_r > 0:
            trail_r = 1.5 if trend > 0 else 0.5 if trend < 0 else 1.0
            if gain <= peak_r - trail_r and peak_r >= trail_r:
                return dict(r=gain, reason="TRAIL", peak=peak_r)
    last = float(path["close"].iloc[-1])
    return dict(r=(last - entry) * d / dist, reason="EOD", peak=peak_r)


def day_character(m5):
    out = {}
    for day, g in m5.groupby(m5.index.normalize()):
        g = g[(g.index.time >= time(9, 15)) & (g.index.time < time(15, 30))]
        if len(g) < 20:
            continue
        rng = g["high"].max() - g["low"].min()
        net = g["close"].iloc[-1] - g["open"].iloc[0]
        eff = abs(net) / rng if rng > 0 else 0.0
        kind = "TREND_UP" if eff >= 0.5 and net > 0 else "TREND_DOWN" if eff >= 0.5 else "RANGE"
        out[day] = dict(kind=kind, range_pct=rng / g["open"].iloc[0] * 100.0)
    return out


VARIANTS = {
    "shipped": {},
    "no_runner": {"runner": False},
    "fixed_1r": {"fixed_target": 1.0},
    "fixed_1_5r": {"fixed_target": 1.5},
    "no_trend_cut": {"trend_cut": False},
    "no_weak_0_7r": {"weak": False},
    "no_chart_exit": {"chart_exit": False},
    "no_profit_lock": {"lock": False},
    "no_trail": {"trail": False},
    "lock_arm_0_5r": {"arm_r": 0.5},
    "stop_x1_3": {"stop_mult": 1.3},
    "stop_x0_8": {"stop_mult": 0.8},
    "no_runner_no_chart_exit": {"runner": False, "chart_exit": False},
    "lock_0_5r_no_runner": {"arm_r": 0.5, "runner": False},
    "lock_0_5r_runner": {"arm_r": 0.5},
    "lock_0_7r": {"arm_r": 0.7},
    "lock_1r_old": {"arm_r": 1.0},
    "runner_always_30r": {"always_30r": True},
}


def main():
    rows = []
    signal_counts = defaultdict(int)
    for symbol in INDICES:
        frames = {tf: read(symbol, tf) for tf in TFS}
        p = params_from_settings(SETTINGS, symbol)
        m1 = drop_off_session_bars(frames["1min"], p.session_end)
        days = day_character(drop_off_session_bars(frames["5min"], p.session_end))
        vol_cuts = np.percentile([v["range_pct"] for v in days.values()], [33.3, 66.7]) if days else [0, 0]
        d5 = drop_off_session_bars(frames["5min"], p.session_end)
        d15 = drop_off_session_bars(frames["15min"], p.session_end)
        # Real-time context at entry: session open and running VWAP on 1m bars.
        m1_day = m1.index.normalize()
        day_open = m1.groupby(m1_day)["open"].transform("first")
        typical = (m1["high"] + m1["low"] + m1["close"]) / 3.0
        vol = m1["volume"].where(m1["volume"] > 0, 1.0)
        vwap = (typical * vol).groupby(m1_day).cumsum() / vol.groupby(m1_day).cumsum()
        path_len = m1["close"].diff().abs().where(m1_day == pd.Series(m1_day, index=m1.index).shift(), 0.0).fillna(0.0)
        cum_path = path_len.groupby(m1_day).cumsum()
        day_hi = m1["high"].groupby(m1_day).cummax()
        day_lo = m1["low"].groupby(m1_day).cummin()
        daily_rng = (m1["high"].groupby(m1_day).max() - m1["low"].groupby(m1_day).min())
        prior_avg_rng = daily_rng.shift(1).rolling(10, min_periods=3).mean()
        f15 = compute_features(d15, "15min", p, df_5min=d5, df_15min=d15)
        CONTEXT[symbol] = dict(m1=m1, day_open=day_open)
        for tf, tf_min in TFS.items():
            df = drop_off_session_bars(frames[tf], p.session_end)
            f = compute_features(df, tf, p, df_5min=d5, df_15min=d15)
            signals = get_s1_signals(df, symbol, tf, p, features=f)[0]
            pos_of = {ts: i for i, ts in enumerate(df.index)}
            for sig in signals:
                d = 1 if sig.signal_type == "BUY" else -1
                signal_counts[(symbol, tf, sig.signal_type)] += 1
                burst = bool(getattr(sig, "s1_rel_volume", 0.0) >= 1.5)
                if tf == "1min" and not burst:
                    continue
                day = pd.Timestamp(sig.timestamp).normalize()
                info = days.get(day, {"kind": "UNKNOWN", "range_pct": np.nan})
                vol = ("LOW" if info["range_pct"] <= vol_cuts[0] else "HIGH" if info["range_pct"] > vol_cuts[1]
                       else "MID") if np.isfinite(info["range_pct"]) else "UNKNOWN"
                at = pd.Timestamp(sig.timestamp) + pd.Timedelta(minutes=tf_min)
                k = m1.index.searchsorted(at, side="left") - 1
                if k >= 0 and m1.index[k].normalize() == day:
                    px = float(m1["close"].iloc[k])
                    open_align = (px - float(day_open.iloc[k])) * d > 0
                    vwap_align = (px - float(vwap.iloc[k])) * d > 0
                    net = abs(px - float(day_open.iloc[k]))
                    er = net / float(cum_path.iloc[k]) if float(cum_path.iloc[k]) > 0 else 0.0
                    avg_r = prior_avg_rng.get(day, np.nan)
                    rng_ratio = float(day_hi.iloc[k] - day_lo.iloc[k]) / avg_r if avg_r and np.isfinite(avg_r) else np.nan
                    j15 = d15.index.searchsorted(at - pd.Timedelta(minutes=15), side="right") - 1
                    gap15 = float((f15["bull_score"][j15] - f15["bear_score"][j15]) * d) if j15 >= 0 else np.nan
                    ip = pos_of.get(pd.Timestamp(sig.timestamp))
                    adx_slope = float(f["adx"][ip] - f["adx"][ip - 3]) if ip is not None and ip >= 3 else np.nan
                else:
                    open_align = vwap_align = None
                    er = rng_ratio = gap15 = adx_slope = np.nan
                base = dict(symbol=symbol, tf=tf, side=sig.signal_type, day=str(day.date()),
                            open_align=open_align, vwap_align=vwap_align, er_so_far=er, range_ratio=rng_ratio,
                            htf15_gap=gap15, adx_slope=adx_slope, entry_at=str(at),
                            adx=float(sig.adx_value or 0.0), rel_vol=float(getattr(sig, "s1_rel_volume", 0.0) or 0.0),
                            atr_exp=float(getattr(sig, "s1_atr_expansion", 0.0) or 0.0),
                            entry_hhmm=at.strftime("%H:%M"),
                            day_kind=info["kind"], vol=vol, score=float(sig.s1_score or 0.0),
                            with_day=(info["kind"] == "TREND_UP" and d == 1) or (info["kind"] == "TREND_DOWN" and d == -1),
                            against_day=(info["kind"] == "TREND_UP" and d == -1) or (info["kind"] == "TREND_DOWN" and d == 1))
                for name, variant in VARIANTS.items():
                    res = manage(sig, d, tf_min, df, f, pos_of, m1, variant)
                    if res is not None:
                        rows.append(dict(base, variant=name, **res))
    out = pd.DataFrame(rows)

    def breadth(row):
        at = pd.Timestamp(row["entry_at"])
        d = 1 if row["side"] == "BUY" else -1
        n = 0
        for sym, ctx in CONTEXT.items():
            m1, day_open = ctx["m1"], ctx["day_open"]
            k = m1.index.searchsorted(at, side="left") - 1
            if k >= 0 and m1.index[k].normalize() == at.normalize():
                n += int((float(m1["close"].iloc[k]) - float(day_open.iloc[k])) * d > 0)
        return n
    if len(out):
        keys = out[["entry_at", "side"]].drop_duplicates()
        keys["breadth"] = keys.apply(breadth, axis=1)
        out = out.merge(keys, on=["entry_at", "side"], how="left")
    out.to_csv(ROOT / "reports/s1_lane_workflow_study_20260925_trades.csv", index=False)

    def summ(g):
        return dict(trades=int(len(g)), win_pct=round(float((g["r"] > 0).mean() * 100), 1),
                    avg_r=round(float(g["r"].mean()), 3), net_r=round(float((g["r"] - COST_R).sum()), 2),
                    net_avg_r=round(float((g["r"] - COST_R).mean()), 3))

    shipped = out[out.variant == "shipped"]
    report = {
        "sessions": int(shipped["day"].nunique()),
        "signals_raw": {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in sorted(signal_counts.items())},
        "variants": {name: summ(g) for name, g in out.groupby("variant")},
        "shipped_by_side": {k: summ(g) for k, g in shipped.groupby("side")},
        "shipped_by_tf": {k: summ(g) for k, g in shipped.groupby("tf")},
        "shipped_by_index": {k: summ(g) for k, g in shipped.groupby("symbol")},
        "shipped_by_day_kind": {k: summ(g) for k, g in shipped.groupby("day_kind")},
        "shipped_by_side_and_day_kind": {f"{a}|{b}": summ(g) for (a, b), g in shipped.groupby(["side", "day_kind"])},
        "shipped_with_vs_against_day": {"with": summ(shipped[shipped.with_day]),
                                        "against": summ(shipped[shipped.against_day]),
                                        "range_day": summ(shipped[shipped.day_kind == "RANGE"])},
        "shipped_by_volatility": {k: summ(g) for k, g in shipped.groupby("vol")},
        "shipped_exit_reasons": shipped["reason"].value_counts().to_dict(),
        "shipped_score_buckets": {str(k): summ(g) for k, g in shipped.groupby(pd.cut(shipped["score"], [0, 70, 80, 90, 101]), observed=True)},
        "runner_peaks": {"trades_reaching_3r": int((shipped["peak"] >= 3).sum()),
                         "trades_reaching_5r": int((shipped["peak"] >= 5).sum()),
                         "trades_reaching_10r": int((shipped["peak"] >= 10).sum()),
                         "max_peak_r": round(float(shipped["peak"].max()), 2) if len(shipped) else 0.0},
    }
    (ROOT / "reports/s1_lane_workflow_study_20260925.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
