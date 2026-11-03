"""UT SMART EXIT: would the S1-style 1m burst trades pay? (study 2026-09-26)

S1 trades 1m only on a volume burst (the system's 1m rule, every engine). UT SMART EXIT
is 15m-only by default (uts_lane_timeframes), so its 1m burst path never runs. This
replays UT SMART 1m flips from the real engine, admits only those that pass the system's
1m burst test at the signal bar, and trades them with the lane's own exits.

Burst test (engine/signal_quality_policy.volume_spike_entry_confirmed, 1m, high-burst-only):
strong context ADX(14) >= 25, volume >= 2.0x its 20-bar mean, bar range >= 1.5 ATR,
close location >= 0.65 in the signal direction. The intelligence-score leg is not
reproducible offline and is left out (ADX alone admits the strong context).
Execution/costs as reports/ut_smart_exit_study_20260925.py (next 1m open, 15:15 squareoff,
per lot at delta 0.5 after Rs.80 + spread). Parameter sets: the shipped (15m-tuned) set
and the script defaults.
"""
from __future__ import annotations

import sys
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


def burst_mask(df, ind, signal, adx_min=25.0, vol_min=2.0, range_atr=1.5, close_loc=0.65):
    vol = df["volume"].to_numpy(dtype=float)
    base = pd.Series(vol).rolling(20, min_periods=10).mean().shift(1).to_numpy()
    ratio = np.where(base > 0, vol / base, 0.0)
    hi, lo, cl = ind["high"], ind["low"], ind["close"]
    rng = np.maximum(hi - lo, 1e-9)
    rng_atr = np.where(ind["atr"] > 0, (hi - lo) / ind["atr"], 0.0)
    loc_up = (cl - lo) / rng
    loc_dn = (hi - cl) / rng
    adx = UTBotEngine.calculate_adx(df, 14)[2].to_numpy(dtype=float)
    loc = np.where(signal > 0, loc_up, loc_dn)
    return (np.nan_to_num(adx) >= adx_min) & (ratio >= vol_min) & (rng_atr >= range_atr) & (loc >= close_loc)


def main():
    rows = []
    for label, p in (("shipped", u.params_from_settings(Settings())), ("pine_default", u.UTSParams())):
        for ix in st.INDICES:
            s = st.Series(ix, "1min")
            s.df = pd.read_csv(ROOT / f"data_store/candles/{ix}_1min.csv", index_col=0, parse_dates=True)
            s.df = u.drop_off_session_bars(s.df[~s.df.index.duplicated(keep="last")].sort_index()[["open", "high", "low", "close", "volume"]].astype(float))
            ind = u.indicators(s.df, p)
            out = u.run(ind, p)
            burst = burst_mask(s.df, ind, out["signal"])
            for mode, ok in (("all_1m", s.entry_ok), ("burst_1m", s.entry_ok & burst)):
                r = st._execute(ind["open"], ind["high"], ind["low"], ind["close"], ind["trail"], out["signal"],
                                out["state"], out["exit"], s.day, s.last, ok, False)
                t = st.frame(s, r)
                t["params"], t["mode"], t["index"] = label, mode, ix
                rows.append(t)
    a = pd.concat(rows)
    days = np.sort(np.unique(a["day"]))
    split = days[len(days) // 2]
    a["half"] = np.where(a["day"] < split, "H1", "H2")
    pd.set_option("display.width", 200)
    g = a.groupby(["params", "mode"])
    print(pd.DataFrame({"trades": g.size(), "win%": g["rs"].apply(lambda x: (x > 0).mean() * 100),
                        "rs/trade": g["rs"].mean(), "total": g["rs"].sum()}).round(0).to_string())
    print(a.pivot_table(index=["params", "mode"], columns="half", values="rs", aggfunc="sum").round(0).to_string())
    print(a.pivot_table(index=["params", "mode"], columns="index", values="rs", aggfunc="sum").round(0).to_string())
    print("gross points/trade:", a.groupby(["params", "mode"])["pts"].mean().round(2).to_dict())


if __name__ == "__main__":
    main()
