"""UT SMART EXIT tuning study (2026-09-25): the ported engine on every stored session.

Engine: engine/ut_smart_engine.py -- the same numba code the live engine runs.
Data:   data_store/candles/{index}_{1min,5min,15min}.csv, session bars [09:15, 15:15).

Execution the way the live lane trades it, per signal timeframe:
    entry    open of the bar after the signal bar (the signal is confirmed on its close);
             no entry from a signal bar closing after 15:10 (1m/5m) or 14:50 (15m)
    exits    the engine's own exits (opposite UT signal, smart exit, hard exit), executed
             at the next bar's open; the 15:15 squareoff at the last session bar's close
    variant  "stop": also exit intrabar when price trades through the UT trailing stop
             known at the bar's open (the Pine script only reacts on the close)
Units: index points per trade, and rupees per lot as an ATM option (delta 0.5) after a
flat Rs.80 plus a half-spread each side (NIFTY 0.5, BANKNIFTY 1.0, SENSEX 1.0, MIDCPNIFTY
0.25 premium points).

Walk-forward: sessions split in date order into two halves; a parameter set is chosen on
the first half and judged on the second. Descriptive, one sample of ~44 sessions.
"""
from __future__ import annotations

import itertools
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from numba import njit

from engine import ut_smart_engine as u

INDICES = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
TFS = {"1min": 1, "5min": 5, "15min": 15}
LOT = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20, "MIDCPNIFTY": 120}
HALF_SPREAD = {"NIFTY": 0.5, "BANKNIFTY": 1.0, "SENSEX": 1.0, "MIDCPNIFTY": 0.25}
FLAT_COST = 80.0
DELTA = 0.5
REASONS = {1: "SMART_EXIT", 2: "SMART_HARD_EXIT", 3: "UT_REVERSE", 4: "EOD", 5: "TRAIL_STOP"}


def cost_rs(index):
    return FLAT_COST + 2 * HALF_SPREAD[index] * LOT[index]


def read(index, tf):
    df = pd.read_csv(ROOT / f"data_store/candles/{index}_{tf}.csv", index_col=0, parse_dates=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return u.drop_off_session_bars(df)


@njit(cache=True)
def _execute(op, hi, lo, cl, trail, signal, state, exit_code, day, last_of_day, entry_ok, intrabar_stop):
    """Live execution of the engine's signals. One row per trade:
    dir, entry_i, exit_i, entry_px, exit_px, risk_pts, reason, mfe_pts."""
    n = len(cl)
    out = np.zeros((n, 8))
    k = 0
    pos = 0
    entry_px = 0.0
    entry_i = -1
    risk = 0.0
    best = 0.0
    pend_exit = 0          # reason code to execute at the next open
    pend_entry = 0
    pend_risk = 0.0
    for i in range(n):
        # 1. orders queued at the previous close fill at this open (same session only)
        if i > 0 and day[i] == day[i - 1]:
            if pend_exit != 0 and pos != 0:
                px = op[i]
                out[k, 0] = pos; out[k, 1] = entry_i; out[k, 2] = i; out[k, 3] = entry_px
                out[k, 4] = px; out[k, 5] = risk; out[k, 6] = pend_exit; out[k, 7] = best
                k += 1
                pos = 0
            if pend_entry != 0 and pos == 0:
                pos = pend_entry
                entry_px = op[i]
                entry_i = i
                risk = pend_risk
                best = 0.0
        pend_exit = 0
        pend_entry = 0
        # 2. intrabar trail stop (variant): the trail known at this bar's open
        if pos != 0 and intrabar_stop and i > 0 and not np.isnan(trail[i - 1]):
            t = trail[i - 1]
            hit = (pos == 1 and lo[i] <= t) or (pos == -1 and hi[i] >= t)
            if hit:
                px = min(op[i], t) if pos == 1 else max(op[i], t)
                fav = (hi[i] - entry_px) if pos == 1 else (entry_px - lo[i])
                best = max(best, fav)
                out[k, 0] = pos; out[k, 1] = entry_i; out[k, 2] = i; out[k, 3] = entry_px
                out[k, 4] = px; out[k, 5] = risk; out[k, 6] = 5; out[k, 7] = best
                k += 1
                pos = 0
        if pos != 0:
            fav = (hi[i] - entry_px) if pos == 1 else (entry_px - lo[i])
            best = max(best, fav)
        # 3. the engine's decisions at this close
        if signal[i] != 0:
            if pos == -signal[i]:
                pend_exit = 3
            if entry_ok[i] and not (pos == signal[i]):
                pend_entry = signal[i]
                pend_risk = abs(cl[i] - trail[i])
        elif exit_code[i] != 0 and pos != 0 and i > 0 and state[i - 1] == pos:
            pend_exit = exit_code[i]
        # 4. squareoff at the session's last bar
        if last_of_day[i]:
            if pos != 0:
                out[k, 0] = pos; out[k, 1] = entry_i; out[k, 2] = i; out[k, 3] = entry_px
                out[k, 4] = cl[i]; out[k, 5] = risk; out[k, 6] = 4; out[k, 7] = best
                k += 1
                pos = 0
            pend_exit = 0
            pend_entry = 0
    return out[:k]


class Series:
    """One index x timeframe: raw frame plus the arrays every run needs."""

    def __init__(self, index, tf):
        self.index, self.tf = index, tf
        self.df = read(index, tf)
        idx = self.df.index
        # Calendar day number. Not index.asi8 // 86_400e9: pandas 3 parses these CSV stamps
        # at microsecond resolution, which put all 44 sessions into one "day".
        self.day = idx.to_numpy().astype("datetime64[D]").astype(np.int64)
        self.last = np.r_[self.day[1:] != self.day[:-1], True]
        close_min = idx.hour * 60 + idx.minute + TFS[tf]
        cutoff = 14 * 60 + 50 if TFS[tf] >= 15 else 15 * 60 + 10
        self.entry_ok = np.asarray(close_min <= cutoff)
        self.days = np.unique(self.day)
        self._ind = {}

    def ind(self, p):
        key = (p.key_value, p.atr_period, p.use_heikin_ashi)
        if key not in self._ind:
            self._ind[key] = u.indicators(self.df, p)
        return self._ind[key]

    def run(self, p, intrabar_stop=False):
        ind = self.ind(p)
        out = u.run(ind, p)
        rows = _execute(ind["open"], ind["high"], ind["low"], ind["close"], ind["trail"], out["signal"],
                        out["state"], out["exit"], self.day, self.last, self.entry_ok, intrabar_stop)
        return rows


def frame(series, rows):
    if len(rows) == 0:
        return pd.DataFrame(columns=["dir", "entry_i", "exit_i", "entry", "exit", "risk", "reason", "mfe"])
    t = pd.DataFrame(rows, columns=["dir", "entry_i", "exit_i", "entry", "exit", "risk", "reason", "mfe"])
    t["pts"] = t["dir"] * (t["exit"] - t["entry"])
    t["rs"] = t["pts"] * DELTA * LOT[series.index] - cost_rs(series.index)
    t["r"] = np.where(t["risk"] > 0, t["pts"] / t["risk"], np.nan)
    t["day"] = series.day[t["entry_i"].astype(int)]
    t["time"] = series.df.index[t["entry_i"].astype(int)]
    return t


def summary(t):
    if t.empty:
        return {"n": 0}
    return {"n": int(len(t)), "win": round(float((t["rs"] > 0).mean() * 100), 1),
            "pts": round(float(t["pts"].mean()), 2), "rs": round(float(t["rs"].mean()), 1),
            "rs_sum": round(float(t["rs"].sum())), "r": round(float(t["r"].mean()), 3)}


def grid():
    for key, atr, ha in itertools.product((1.0, 1.5, 2.0, 2.5, 3.0), (10, 14, 21), (False, True)):
        base = u.UTSParams(key_value=key, atr_period=atr, use_heikin_ashi=ha)
        yield replace(base, smart_exit=False)
        for arm, ret, hard, conf in itertools.product((0.5, 0.8, 1.2), (0.3, 0.45, 0.7), (0.8, 1.1, 1.6), (1, 2, 3)):
            yield replace(base, arm_atr=arm, retrace_atr=ret, hard_retrace_atr=hard, confirmations=conf)


def main(tfs=("5min", "15min", "1min")):
    report = {}
    for tf in tfs:
        series = [Series(ix, tf) for ix in INDICES]
        days = np.unique(np.concatenate([s.days for s in series]))
        split = days[len(days) // 2]
        results = []
        for p in grid():
            for stop in (False, True):
                a = b = 0.0
                na = nb = 0
                per = {}
                for s in series:
                    rows = s.run(p, stop)
                    if len(rows):
                        rs = rows[:, 0] * (rows[:, 4] - rows[:, 3]) * DELTA * LOT[s.index] - cost_rs(s.index)
                        first = s.day[rows[:, 1].astype(np.int64)] < split
                    else:
                        rs = np.zeros(0); first = np.zeros(0, bool)
                    fa, fb = float(rs[first].sum()), float(rs[~first].sum())
                    a += fa; b += fb
                    na += int(first.sum()); nb += int((~first).sum())
                    per[s.index] = (round(fa), round(fb))
                results.append({"p": p, "stop": stop, "train": a, "test": b, "n_train": na, "n_test": nb, "per": per})
        res = pd.DataFrame(results)
        both = res[(res["train"] > 0) & (res["test"] > 0)]
        best = res.sort_values("train", ascending=False).iloc[0]
        default = res[(res["p"] == u.UTSParams()) & (~res["stop"])].iloc[0]
        top_train = res.sort_values("train", ascending=False).head(20)
        report[tf] = {
            "sessions": int(len(days)), "split_day": str(np.datetime64(int(split), "D")),
            "combos": int(len(res)), "positive_both_halves": int(len(both)),
            "default": {"train": round(default["train"]), "test": round(default["test"]),
                        "n": (int(default["n_train"]), int(default["n_test"])), "per_index": default["per"]},
            "train_best": {"params": best["p"].__dict__, "stop": bool(best["stop"]),
                           "train": round(best["train"]), "test": round(best["test"]),
                           "n": (int(best["n_train"]), int(best["n_test"])), "per_index": best["per"]},
            "top20_by_train_test_sum": round(float(top_train["test"].sum())),
            "top20_by_train_test_positive": int((top_train["test"] > 0).sum()),
            "best_both": [
                {"params": r["p"].__dict__, "stop": bool(r["stop"]), "train": round(r["train"]),
                 "test": round(r["test"]), "per_index": r["per"]}
                for _, r in both.assign(m=both[["train", "test"]].min(axis=1)).sort_values("m", ascending=False).head(8).iterrows()
            ],
        }
        print(tf, json.dumps(report[tf], indent=1, default=str), flush=True)
    return report


if __name__ == "__main__":
    tfs = tuple(sys.argv[1:]) or ("5min", "15min", "1min")
    main(tfs)
