"""Why do UT SMART EXIT's 5m and 1m trades lose, and what would fix them? (2026-09-26)

Data: 1m index candles from data_store/session_archive (47 sessions, 2026-07-20..09-25;
the live CSVs were cut to 22 sessions on 09-25) merged with the live CSV; 5m/15m are
resampled from the same 1m bars so every frame sees identical prices. Session bars only.
Engine: engine/ut_smart_engine.py (shipped params unless a variant says otherwise).
Execution/costs as reports/ut_smart_exit_study_20260925.py: entry at the next bar's open,
the engine's exits at the next open, 15:15 squareoff, cutoffs 15m 12:45 / 5m 15:05 /
1m 15:09 (signal-bar start), per lot at delta 0.5 after Rs.80 + half spread each side.
Every variant is reported per half (sessions split in date order) -- a fix must hold in both.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "reports"))

import numpy as np
import pandas as pd

import ut_smart_exit_study_20260925 as st
from config.settings import Settings
from engine import ut_smart_engine as u
from engine.ut_bot_core import UTBotEngine

INDICES = st.INDICES
CUTOFF = {"1min": 15 * 60 + 9, "5min": 15 * 60 + 5, "15min": 12 * 60 + 45}   # last signal-bar start
TF_MIN = {"1min": 1, "5min": 5, "15min": 15}


def load_1m(index):
    parts = []
    for day in sorted((ROOT / "data_store/session_archive").glob("*/candles")):
        f = day / f"{index}_1min.csv"
        if f.exists():
            try:
                parts.append(pd.read_csv(f, index_col=0, parse_dates=True))
            except Exception:
                pass
    parts.append(pd.read_csv(ROOT / f"data_store/candles/{index}_1min.csv", index_col=0, parse_dates=True))
    df = pd.concat(parts)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    df = df[~df.index.duplicated(keep="last")].sort_index()[["open", "high", "low", "close", "volume"]]
    return u.drop_off_session_bars(df.apply(pd.to_numeric, errors="coerce").dropna(subset=["open", "close"]))


def resample(m1, minutes):
    if minutes == 1:
        return m1
    day = m1.index.normalize()
    slot = (m1.index - day - pd.Timedelta(hours=9, minutes=15)) // pd.Timedelta(minutes=minutes)
    key = day + pd.Timedelta(hours=9, minutes=15) + slot * pd.Timedelta(minutes=minutes)
    g = m1.groupby(key)
    return pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                         "close": g["close"].last(), "volume": g["volume"].sum()})


class Frame:
    def __init__(self, index, tf, df):
        self.index, self.tf, self.df = index, tf, df
        idx = df.index
        self.day = idx.to_numpy().astype("datetime64[D]").astype(np.int64)
        self.last = np.r_[self.day[1:] != self.day[:-1], True]
        self.start_min = np.asarray(idx.hour * 60 + idx.minute)
        self.entry_ok = self.start_min <= CUTOFF[tf]

    def run(self, p, extra_ok=None):
        ind = u.indicators(self.df, p)
        out = u.run(ind, p)
        ok = self.entry_ok if extra_ok is None else (self.entry_ok & extra_ok(ind, out))
        rows = st._execute(ind["open"], ind["high"], ind["low"], ind["close"], ind["trail"], out["signal"],
                           out["state"], out["exit"], self.day, self.last, ok, False)
        return frame_rows(self, rows), ind, out


def frame_rows(fr, rows):
    cols = ["dir", "entry_i", "exit_i", "entry", "exit", "risk", "reason", "mfe"]
    t = pd.DataFrame(rows, columns=cols) if len(rows) else pd.DataFrame(columns=cols)
    if t.empty:
        return t
    t["pts"] = t["dir"] * (t["exit"] - t["entry"])
    t["rs"] = t["pts"] * st.DELTA * st.LOT[fr.index] - st.cost_rs(fr.index)
    t["cost_pts"] = st.cost_rs(fr.index) / (st.DELTA * st.LOT[fr.index])
    t["bars"] = t["exit_i"] - t["entry_i"]
    t["day"] = fr.day[t["entry_i"].astype(int)]
    t["start_min"] = fr.start_min[t["entry_i"].astype(int)]
    t["index"] = fr.index
    return t


def htf_state(fr_low, fr_high, p):
    """The higher frame's CLOSED UT SMART state at each lower-frame bar (no lookahead)."""
    ind = u.indicators(fr_high.df, p)
    out = u.run(ind, p)
    # a higher bar's state is known once it closes: shift its stamp to its close time
    stamps = fr_high.df.index + pd.Timedelta(minutes=TF_MIN[fr_high.tf])
    s = pd.Series(out["state"], index=stamps)
    low_close = fr_low.df.index + pd.Timedelta(minutes=TF_MIN[fr_low.tf])
    aligned = s.reindex(s.index.union(low_close)).ffill().reindex(low_close)
    return np.nan_to_num(aligned.to_numpy()).astype(int)


def summarize(t, split):
    if t.empty:
        return dict(n=0)
    h1, h2 = t[t["day"] < split], t[t["day"] >= split]
    return dict(n=len(t), win=round((t["rs"] > 0).mean() * 100), gross=round(t["pts"].mean(), 2),
                cost=round(t["cost_pts"].mean(), 2), rs_trade=round(t["rs"].mean()), total=round(t["rs"].sum()),
                H1=round(h1["rs"].sum()), H2=round(h2["rs"].sum()))


def main():
    base = u.params_from_settings(Settings())
    frames = {}
    for ix in INDICES:
        m1 = load_1m(ix)
        frames[ix] = {tf: Frame(ix, tf, resample(m1, TF_MIN[tf])) for tf in TF_MIN}
    days = np.unique(np.concatenate([frames[ix]["1min"].day for ix in INDICES]))
    split = days[len(days) // 2]
    print(f"sessions {len(days)} ({np.datetime64(int(days[0]), 'D')}..{np.datetime64(int(days[-1]), 'D')}), "
          f"split {np.datetime64(int(split), 'D')}")
    results = []

    def collect(label, tf, per_index):
        t = pd.concat(per_index) if per_index else pd.DataFrame()
        results.append((label, tf, summarize(t, split)))
        return t

    # ---------------------------------------------------------------- 1. anatomy of the loss
    anatomy = {}
    for tf in ("15min", "5min", "1min"):
        per = [frames[ix][tf].run(base)[0] for ix in INDICES]
        t = collect("shipped", tf, per)
        anatomy[tf] = t
    print("\n== anatomy (shipped params)")
    for tf, t in anatomy.items():
        days_n = len(np.unique(t["day"]))
        quick = t[t["bars"] <= 2]
        print(f"{tf:>5}: {len(t)} trades ({len(t) / max(1, days_n) / 4:.1f}/index/day), gross {t['pts'].mean():+.2f} pts "
              f"vs cost {t['cost_pts'].mean():.2f} pts; median hold {t['bars'].median():.0f} bars; "
              f"{len(quick)} trades <=2 bars made {quick['rs'].sum():+.0f}")
        by_reason = t.groupby("reason").agg(n=("rs", "size"), pts=("pts", "mean"), rs=("rs", "sum")).round(1)
        by_reason.index = [st.REASONS.get(int(r), r) for r in by_reason.index]
        print("       exits:", by_reason.to_dict("index"))
        t = t.assign(slot=pd.cut(t["start_min"], [0, 600, 690, 780, 900], labels=["09:15-10:00", "10:00-11:30", "11:30-13:00", "13:00+"]))
        print("       by entry time:", t.groupby("slot", observed=True)["rs"].agg(["size", "sum"]).round(0).to_dict("index"))

    # ---------------------------------------------------------------- 2. candidate fixes
    def htf_filter(ix, tf, higher):
        states = {h: htf_state(frames[ix][tf], frames[ix][h], base) for h in higher}

        def f(ind, out):
            ok = np.ones(len(out["signal"]), bool)
            for h, s in states.items():
                ok &= (out["signal"] == s) | (out["signal"] == 0)
            return ok
        return f

    def adx_filter(tf_df, minimum):
        adx = UTBotEngine.calculate_adx(tf_df, 14)[2].to_numpy(dtype=float)
        return lambda ind, out: np.nan_to_num(adx) >= minimum

    for tf, higher in (("5min", ("15min",)), ("1min", ("5min", "15min"))):
        collect("htf aligned", tf, [frames[ix][tf].run(base, htf_filter(ix, tf, higher))[0] for ix in INDICES])
        for key in (3.0, 4.0, 5.0):
            p = replace(base, key_value=key)
            collect(f"key {key:g}", tf, [frames[ix][tf].run(p)[0] for ix in INDICES])
            collect(f"key {key:g} + htf aligned", tf,
                    [frames[ix][tf].run(p, htf_filter(ix, tf, higher))[0] for ix in INDICES])
        collect("ADX>=25", tf, [frames[ix][tf].run(base, (lambda ix=ix: (lambda ind, out, a=adx_filter(frames[ix][tf].df, 25): a(ind, out)))())[0]
                                 for ix in INDICES])
        collect("before 11:30", tf, [frames[ix][tf].run(base, lambda ind, out, fr=frames[ix][tf]: fr.start_min < 690)[0]
                                     for ix in INDICES])

    # 5m entry, 15m hold: enter on a 5m flip that agrees with the closed 15m state; stay in
    # until the 15m state turns against it or 15:15 (the 15m trend carries the trade).
    per = []
    for ix in INDICES:
        fr5, fr15 = frames[ix]["5min"], frames[ix]["15min"]
        ind = u.indicators(fr5.df, base)
        out = u.run(ind, base)
        s15 = htf_state(fr5, fr15, base)
        op, cl = ind["open"], ind["close"]
        rows, pos, entry_px, entry_i = [], 0, 0.0, -1
        for i in range(len(cl) - 1):
            same_day_next = fr5.day[i + 1] == fr5.day[i]
            if pos != 0 and (s15[i] != pos or not same_day_next):
                px = op[i + 1] if same_day_next else cl[i]
                rows.append([pos, entry_i, i, entry_px, px, 0.0, 3 if same_day_next else 4, 0.0])
                pos = 0
            sig = out["signal"][i]
            if pos == 0 and sig != 0 and sig == s15[i] and fr5.entry_ok[i] and same_day_next:
                pos, entry_px, entry_i = int(sig), op[i + 1], i + 1
        per.append(frame_rows(fr5, np.asarray(rows, float) if rows else np.zeros((0, 8))))
    collect("5m entry + 15m hold", "5min", per)

    print("\n== variants (per lot, after costs; a fix must be positive in BOTH halves)")
    table = pd.DataFrame([dict(tf=tf, variant=label, **res) for label, tf, res in results])
    pd.set_option("display.width", 220)
    print(table.to_string(index=False))


if __name__ == "__main__":
    main()
