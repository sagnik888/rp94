"""UT SMART EXIT: closed-candle vs intracandle (forming-bar) signals (study 2026-09-26).

The live system has a signal-fire setting (INTRACANDLE / CANDLE_CLOSE). The user's Pine
script has the same switch (confirmClose, default ON). This replays the 15m forming candle
minute by minute from 1m candles, the way the live scanner sees it, and trades it the way
the live lane would:

  intrabar signal  the UT flip computed on the PARTIAL candle (open, high/low so far, the
                   1m close; ATR and the trailing stop updated with that partial bar),
                   exactly Pine's realtime evaluation with confirmClose = false
  stabilization    the flip must still be there `stab` minutes later; entry at the next
                   1m open (the live wait is 30-60s)
  repaint          if the candle closes without the flip, the trade exits at the next open
  reversal         an intrabar opposite flip closes the open trade (the lane's reversal)
  exits            the chart's smart / hard exit and the closed-candle reversal, at the next
                   open; 15:15 squareoff; entry cutoff on the signal bar's start (12:45)

Variants: CLOSE (baseline, same data), INTRA (entries and reversals intrabar), INTRA_FLAT
(intrabar entries only when flat; an open trade reverses only on a closed candle), SPEC (the
shipped rules, 2026-09-26 user: INTRA_FLAT plus a repaint check during the candle -- the trade
exits at the next 1m open as soon as the forming flip is gone, not only at the close).
Costs as the tuning study: per lot at delta 0.5 after Rs.80 plus a half spread each side.
15m bars are built from the 1m store so both modes see identical prices.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from config.settings import Settings
from engine import ut_smart_engine as u

INDICES = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
LOT = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20, "MIDCPNIFTY": 120}
HALF_SPREAD = {"NIFTY": 0.5, "BANKNIFTY": 1.0, "SENSEX": 1.0, "MIDCPNIFTY": 0.25}
CUTOFF_START = 12 * 60 + 45


def rupees(index, pts):
    return pts * 0.5 * LOT[index] - (80.0 + 2 * HALF_SPREAD[index] * LOT[index])


def load(index):
    m1 = pd.read_csv(ROOT / f"data_store/candles/{index}_1min.csv", index_col=0, parse_dates=True)
    m1 = m1[~m1.index.duplicated(keep="last")].sort_index()[["open", "high", "low", "close"]].astype(float)
    m1 = u.drop_off_session_bars(m1)
    day = m1.index.normalize()
    slot = ((m1.index - day - pd.Timedelta(hours=9, minutes=15)) // pd.Timedelta(minutes=15))
    m1 = m1.assign(bar=day + pd.Timedelta(hours=9, minutes=15) + slot * pd.Timedelta(minutes=15))
    b15 = m1.groupby("bar").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    return m1, b15


def intrabar_signals(m1, b15, ind, p):
    """{bar i: [(minute timestamp, signal)]} for the forming candle, minute by minute."""
    out = {}
    alpha = 1.0 / p.atr_period
    groups = {k: g for k, g in m1.groupby("bar")}
    closes = b15["close"].to_numpy()
    for i in range(1, len(b15)):
        prev_trail, prev_atr, prev_src = ind["trail"][i - 1], ind["atr"][i - 1], ind["src"][i - 1]
        if not (np.isfinite(prev_trail) and np.isfinite(prev_atr)):
            continue
        g = groups[b15.index[i]]
        o = float(g["open"].iloc[0])
        hi = g["high"].cummax().to_numpy()
        lo = g["low"].cummin().to_numpy()
        cl = g["close"].to_numpy()
        src = (o + hi + lo + cl) / 4.0 if p.use_heikin_ashi else cl
        tr = np.maximum(hi - lo, np.maximum(np.abs(hi - closes[i - 1]), np.abs(lo - closes[i - 1])))
        atr = alpha * tr + (1 - alpha) * prev_atr
        nloss = p.key_value * atr
        trail = np.where((src > prev_trail) & (prev_src > prev_trail), np.maximum(prev_trail, src - nloss),
                np.where((src < prev_trail) & (prev_src < prev_trail), np.minimum(prev_trail, src + nloss),
                np.where(src > prev_trail, src - nloss, src + nloss)))
        buy = (src > trail) & (prev_src <= prev_trail)
        sell = (src < trail) & (prev_src >= prev_trail)
        sig = np.where(buy, 1, np.where(sell, -1, 0))
        out[i] = list(zip(g.index, sig))
    return out


def simulate(index, m1, b15, p, mode, stab=1):
    ind = u.indicators(b15, p)
    run = u.run(ind, p)
    closed_sig, closed_state, closed_exit = run["signal"], run["state"], run["exit"]
    intra = intrabar_signals(m1, b15, ind, p) if mode != "CLOSE" else {}
    opens_1m = m1["open"]
    bars = b15.index
    day_of = bars.normalize()
    trades = []
    pos, entry_px, entry_t, kind = 0, 0.0, None, ""
    pending_bar = None                      # bar index of an unconfirmed intrabar entry

    def close_pos(px, t, why):
        nonlocal pos, pending_bar
        trades.append(dict(index=index, dir=pos, entry=entry_px, exit=px, entry_t=entry_t, exit_t=t,
                           kind=kind, why=why, pts=pos * (px - entry_px)))
        pos = 0
        pending_bar = None

    def open_pos(d, px, t, how):
        nonlocal pos, entry_px, entry_t, kind
        pos, entry_px, entry_t, kind = d, px, t, how

    for i in range(1, len(bars)):
        start_min = bars[i].hour * 60 + bars[i].minute
        last_of_day = i == len(bars) - 1 or day_of[i + 1] != day_of[i]
        next_open = float(b15["open"].iloc[i + 1]) if not last_of_day else float(b15["close"].iloc[i])
        next_t = bars[i + 1] if not last_of_day else bars[i] + pd.Timedelta(minutes=15)
        # ---- inside the forming candle
        if i in intra and start_min < CUTOFF_START:
            mins = intra[i]
            for k in range(len(mins) - 1 - stab):
                window = [s for _, s in mins[k:k + stab + 1]]
                d = window[0]
                if d == 0 or any(s != d for s in window) or pos == d:
                    continue
                if pos == -d and mode in ("INTRA_FLAT", "SPEC"):
                    continue
                t_fill = mins[k + stab + 1][0]
                px = float(opens_1m.loc[t_fill])
                if pos == -d:
                    close_pos(px, t_fill, "INTRA_REVERSE")
                open_pos(d, px, t_fill, "intrabar")
                pending_bar = i
                if mode == "SPEC":
                    # repaint check through the rest of the candle
                    for kk in range(k + stab + 1, len(mins) - 1):
                        if mins[kk][1] != d:
                            t_out = mins[kk + 1][0]
                            close_pos(float(opens_1m.loc[t_out]), t_out, "REPAINT_INTRABAR")
                            break
                    if pos == 0:
                        break
        # ---- at the candle close
        c = int(closed_sig[i])
        if pending_bar == i and c != pos:
            close_pos(next_open, next_t, "REPAINT")
        if c != 0:
            if pos == -c:
                close_pos(next_open, next_t, "UT_REVERSE")
            if pos == 0 and start_min < CUTOFF_START and not last_of_day:
                open_pos(c, next_open, next_t, "close")
            elif pos == c and pending_bar == i:
                pending_bar = None                   # confirmed
        elif closed_exit[i] != 0 and pos != 0 and closed_state[i - 1] == pos:
            close_pos(next_open, next_t, "SMART_EXIT")
        if last_of_day and pos != 0:
            close_pos(float(b15["close"].iloc[i]), next_t, "EOD")
    return pd.DataFrame(trades)


def main():
    p = u.params_from_settings(Settings())
    rows = []
    for index in INDICES:
        m1, b15 = load(index)
        for mode, stab in (("CLOSE", 0), ("INTRA", 0), ("INTRA", 1), ("INTRA", 2), ("INTRA_FLAT", 1), ("SPEC", 1)):
            t = simulate(index, m1, b15, p, mode, stab)
            if t.empty:
                continue
            t["rs"] = [rupees(index, x) for x in t["pts"]]
            t["mode"] = f"{mode}/stab{stab}"
            rows.append(t)
    a = pd.concat(rows)
    days = np.sort(a["entry_t"].dt.normalize().unique())
    split = days[len(days) // 2]
    a["half"] = np.where(a["entry_t"].dt.normalize() < split, "H1", "H2")
    pd.set_option("display.width", 200)
    print("sessions", len(days), "split", pd.Timestamp(split).date())
    summary = a.groupby("mode").agg(trades=("rs", "size"), win=("rs", lambda s: round((s > 0).mean() * 100, 1)),
                                    rs_trade=("rs", "mean"), total=("rs", "sum"))
    halves = a.pivot_table(index="mode", columns="half", values="rs", aggfunc="sum")
    print(summary.join(halves).round(0).to_string())
    print("\nexit reasons by mode:")
    print(a.pivot_table(index="mode", columns="why", values="rs", aggfunc="size", fill_value=0).to_string())
    print("\nP&L by exit reason (INTRA/stab1):")
    x = a[a["mode"] == "INTRA/stab1"]
    print(x.groupby("why")["rs"].agg(["size", "mean", "sum"]).round(0).to_string())
    print("\nentry kind (INTRA/stab1):")
    print(x.groupby("kind")["rs"].agg(["size", "mean", "sum"]).round(0).to_string())
    print("\nby index:")
    print(a.pivot_table(index="mode", columns="index", values="rs", aggfunc="sum").round(0).to_string())
    return a


if __name__ == "__main__":
    main()
