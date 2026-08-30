"""Tick-lifecycle exit backtester (2026-07-24).

Replays each session trade's REAL 1-minute option-premium path (from
data_store/candles/<contract>_1min.csv) from entry to session close through the
exit logic, and A/B-compares:

  * BASELINE  — "take profit at target, else stop"  (no ride)
  * RUNNER    — the trend-runner: engage early on a proven big move, ride the
                current runtime-configured trailing lock  (Phase 1 + Phase 2)

Both are simulated on the SAME real premium bars, so the delta is purely the
exit-logic change. Trades are taken from the rtracker ACCEPTED events; the regime
used is the RAW (un-demoted) regime + ADX, i.e. what the ADX-override regime fix
now preserves. Options are long-premium, so "premium up == gain" for CE and PE.

Usage:  python scripts/backtest_exit_lifecycle.py [YYYY-MM-DD]
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config.settings import get_settings  # noqa: E402
from engine.trade_accounting import estimate_trade_charges  # noqa: E402
from scanner import Scanner  # noqa: E402  (real helpers, for fidelity)

ROOT = Path(__file__).resolve().parents[1]
CANDLES = ROOT / "data_store" / "candles"
LOT_SIZE = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20, "MIDCPNIFTY": 120}
MARKET_TIMEZONE = "Asia/Kolkata"


def _market_timestamp(value) -> pd.Timestamp:
    """Return one archived timestamp as a timezone-naive IST wall clock.

    Session archives contain a historical mix of naïve local timestamps,
    ``+05:30`` offsets, and UTC ``Z`` timestamps.  The live candle cache uses
    naïve IST, so that is the comparison invariant for this replay too.  Aware
    values must be converted before dropping their timezone; merely stripping
    ``Z`` would move the represented market time by 5h30m.
    """
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return pd.NaT
    if pd.isna(ts):
        return pd.NaT
    if ts.tzinfo is not None:
        ts = ts.tz_convert(MARKET_TIMEZONE).tz_localize(None)
    return ts


def _market_timestamp_series(values: pd.Series) -> pd.Series:
    """Normalize even a single column containing mixed timestamp formats."""
    return pd.Series(
        (_market_timestamp(value) for value in values),
        index=values.index,
        dtype="datetime64[ns]",
    )


def _sc() -> Scanner:
    return Scanner.__new__(Scanner)


def _runner_execution_parameters() -> tuple[float, float]:
    """Return the exact production unlock ratio and base runner lock."""
    settings = get_settings()
    unlock = min(
        1.0,
        max(
            0.50,
            float(getattr(settings, "dynamic_rr_runner_unlock_ratio", 0.85) or 0.85),
        ),
    )
    lock = min(
        0.98,
        max(
            0.25,
            float(getattr(settings, "dynamic_rr_runner_lock_pct", 0.85) or 0.85),
        ),
    )
    return unlock, lock


def option_replay_pnl(
    entry_price: float,
    exit_price: float,
    quantity: int,
    instrument: str,
    settings=None,
) -> tuple[float, float, float]:
    """Return gross, shared modeled round-trip costs/slippage, and net P&L."""
    accounting_settings = settings if settings is not None else get_settings()
    gross = round(
        (float(exit_price) - float(entry_price)) * max(0, int(quantity)), 2
    )
    costs = float(
        estimate_trade_charges(
            entry_price,
            exit_price,
            quantity,
            "OPT",
            accounting_settings,
            instrument=instrument,
        )
    )
    return gross, costs, round(gross - costs, 2)


def _candle_sources(day: str) -> list[str]:
    """Prefer the immutable session archive (scripts/archive_session_data.py) over the
    live cache: the live cache only holds last-fetched contracts, so a traded strike's
    bars can be overwritten. Falls back to the live cache for un-archived days."""
    arch = ROOT / "data_store" / "session_archive" / day / "candles"
    if arch.is_dir():
        files = glob.glob(str(arch / "*CE_1min.csv")) + glob.glob(str(arch / "*PE_1min.csv"))
        if files:
            return files
    return glob.glob(str(CANDLES / "*CE_1min.csv")) + glob.glob(str(CANDLES / "*PE_1min.csv"))


def load_today_contracts(day: str) -> dict[str, pd.DataFrame]:
    """{basename: df(day-only, indexed by minute)} for option candles ending on `day`."""
    out: dict[str, pd.DataFrame] = {}
    for f in _candle_sources(day):
        try:
            if os.path.getsize(f) < 200:
                continue
            df = pd.read_csv(f)
            if "timestamp" not in df or not len(df):
                continue
            df["timestamp"] = _market_timestamp_series(df["timestamp"])
            df = df[df["timestamp"].notna()].copy()
            day_df = df[df["timestamp"].dt.strftime("%Y-%m-%d") == day].copy()
            if len(day_df) < 5:
                continue
            day_df = day_df.sort_values("timestamp").reset_index(drop=True)
            out[os.path.basename(f)] = day_df
        except Exception:
            continue
    return out


def _instrument_of(basename: str) -> str:
    m = re.match(r"([A-Z]+?)\d", basename)
    return m.group(1) if m else ""


def _type_of(basename: str) -> str:
    b = basename.upper()
    if "CE_1MIN" in b:
        return "CE"
    if "PE_1MIN" in b:
        return "PE"
    return ""


def load_trades(day: str) -> list[dict]:
    """Entry decisions (rtracker ACCEPTED) with premium entry/stop/target + raw regime."""
    path = ROOT / "data_store" / "rtracker" / "events" / f"{day}.jsonl"
    trades = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if str(r.get("decision", "")).upper() != "ACCEPTED":
            continue
        reg = r.get("regime")
        raw_regime = (reg.get("raw_regime") if isinstance(reg, dict) else "") or ""
        adx = float(reg.get("adx") if isinstance(reg, dict) else 0.0) or 0.0
        entry_ts = _market_timestamp(r.get("signal_timestamp") or r.get("entry_timestamp"))
        if pd.isna(entry_ts):
            continue
        trades.append({
            "instrument": str(r.get("instrument") or "").split()[0],
            "direction": str(r.get("direction") or "").upper(),
            "timeframe": r.get("timeframe"),
            "entry": float(r.get("price") or 0.0),
            "stop": float(r.get("stop") or 0.0),
            "target": float(r.get("target") or 0.0),
            "entry_ts": entry_ts,
            "raw_regime": str(raw_regime).upper(),
            "adx": adx,
            "option_type": "CE" if str(r.get("direction") or "").upper() == "LONG" else "PE",
            "trading_symbol": str((r.get("contract") or {}).get("trading_symbol") or "")
            if isinstance(r.get("contract"), dict) else "",
        })
    return trades


def match_contract(trade: dict, contracts: dict[str, pd.DataFrame]) -> tuple[str, pd.DataFrame] | None:
    """Exact match on the recorded traded contract when available (rtracker now stores
    contract identity); otherwise fall back to premium-proximity guessing."""
    sym = str(trade.get("trading_symbol") or "").strip()
    if sym:
        for name, df in contracts.items():
            if name.startswith(sym + "_"):
                return name, df
    best = None
    best_err = 1e9
    ts = trade["entry_ts"]
    for name, df in contracts.items():
        if _instrument_of(name) != trade["instrument"] or _type_of(name) != trade["option_type"]:
            continue
        at = df[df["timestamp"] <= ts]
        if not len(at):
            continue
        prem = float(at.iloc[-1]["close"])
        err = abs(prem - trade["entry"]) / max(1.0, trade["entry"])
        if err < best_err:
            best_err, best = err, (name, df)
    # require a reasonable premium match (<8%)
    if best is not None and best_err <= 0.08:
        return best
    return None


def _bars_from_entry(df: pd.DataFrame, entry_ts) -> pd.DataFrame:
    return df[df["timestamp"] >= entry_ts].reset_index(drop=True)


def sim_old(trade: dict, bars: pd.DataFrame, qty: int) -> tuple[float, str]:
    """Approximate the OLD exit logic on the real premium path: stop / target PLUS the
    peak-fade give-back guard that actually cut winners (peak >= arm and price fades to
    <= giveback_ratio of the peak gain). This is the fair baseline to compare the runner
    against (the naive target/stop floor omits the give-back and is too pessimistic)."""
    entry, stop, target = trade["entry"], trade["stop"], trade["target"]
    arm_pts = max(0.40 * max(1e-9, entry - stop), 500.0 / max(1, qty))  # ~0.40R or Rs500
    gb_ratio = 0.50  # profit_giveback_exit_ratio default
    peak = entry
    for _, b in bars.iterrows():
        hi, lo = float(b["high"]), float(b["low"])
        if stop > 0 and lo <= stop:
            return stop, "STOP"
        # give-back guard: once a real peak exists, a fade to <=50% of it exits
        peak_gain = peak - entry
        if peak_gain >= arm_pts and lo <= entry + peak_gain * gb_ratio:
            return entry + peak_gain * gb_ratio, "GIVEBACK"
        if target > 0 and hi >= target:
            return target, "TARGET"
        peak = max(peak, hi)
    return float(bars.iloc[-1]["close"]) if len(bars) else entry, "CLOSE"


def sim_runner(trade: dict, bars: pd.DataFrame, sc: Scanner, qty: int) -> tuple[float, str]:
    """Trend-runner: engage early on a proven big move (real _big_move_expected),
    then trail the volatility-adjusted lock (real _dynamic_runner_lock_pct)."""
    entry, stop, target = trade["entry"], trade["stop"], trade["target"]
    regime, risk = trade["raw_regime"], max(1e-9, entry - stop)
    tl = type("T", (), {"inst_type": "OPT", "option_type": trade["option_type"],
                        "direction": trade["direction"], "adx_value": trade["adx"],
                        "entry_price": entry, "initial_stop": stop, "trailing_stop": stop})()
    runner_unlock_ratio, base_lock_pct = _runner_execution_parameters()
    lock_pct = sc._dynamic_runner_lock_pct(base_lock_pct, regime)
    target_dist = max(0.0, target - entry)
    arm_pts = max(0.40 * risk, 500.0 / max(1, qty))
    gb_ratio = 0.50
    peak = entry
    runner = False
    for _, b in bars.iterrows():
        hi, lo = float(b["high"]), float(b["low"])
        peak_gain = peak - entry
        # Intra-bar rule (SAME as OLD): the adverse extreme (low) is tested first.
        if runner:
            # Runner engaged: give-back guard is suppressed; the trailing lock governs.
            eff_stop = entry + peak_gain * lock_pct
            if lo <= eff_stop:
                return max(eff_stop, stop), "RUNNER_LOCK"
        else:
            # Below the big-move threshold the trade keeps the SAME protection as OLD
            # (stop + give-back) — only a proven big move suppresses the give-back.
            if stop > 0 and lo <= stop:
                return stop, "STOP"
            if peak_gain >= arm_pts and lo <= entry + peak_gain * gb_ratio:
                return entry + peak_gain * gb_ratio, "GIVEBACK"
        # This bar's high extends the peak and may engage the runner.
        peak = max(peak, hi)
        peak_gain = peak - entry
        gain_r = peak_gain / risk
        if not runner:
            reached_t1 = target_dist > 0 and peak_gain >= target_dist * runner_unlock_ratio
            big_ok, _ = sc._big_move_expected(tl, gain_r=gain_r, regime=regime)
            if reached_t1 or big_ok:
                runner = True
            elif target > 0 and hi >= target:
                return target, "TARGET"
    return float(bars.iloc[-1]["close"]) if len(bars) else entry, "CLOSE"


def _atm_opposite(trade: dict, contracts: dict[str, pd.DataFrame], ts) -> tuple[str, pd.DataFrame] | None:
    """Fresh-ATM opposite-side contract at time `ts`: the opposite CE/PE for the same
    instrument whose premium is closest to at-the-money (proxied by the most actively
    traded / mid-priced available strike with bars at ts)."""
    want = "PE" if trade["option_type"] == "CE" else "CE"
    best = None
    best_vol = -1.0
    for name, df in contracts.items():
        if _instrument_of(name) != trade["instrument"] or _type_of(name) != want:
            continue
        at = df[df["timestamp"] <= ts]
        if not len(at):
            continue
        row = at.iloc[-1]
        vol = float(row.get("volume", 0) or 0)
        if vol > best_vol:
            best_vol, best = vol, (name, df)
    return best


def sim_runner_hedged(trade: dict, bars: pd.DataFrame, sc: Scanner, qty: int,
                      contracts: dict[str, pd.DataFrame]) -> tuple[float, str, float, int]:
    """Runner + pullback hedge. Returns (exit_premium, why, hedge_pnl_rs, hedge_count).

    The parent lifecycle is identical to sim_runner; the hedge is an ADDITIONAL
    fresh-ATM opposite leg armed on an early pullback and released on resume. The
    hedge always closes with the parent (never a naked leg)."""
    from config.settings import get_settings

    entry, stop, target = trade["entry"], trade["stop"], trade["target"]
    regime, risk = trade["raw_regime"], max(1e-9, entry - stop)
    tl = type("T", (), {"inst_type": "OPT", "option_type": trade["option_type"],
                        "direction": trade["direction"], "adx_value": trade["adx"],
                        "entry_price": entry, "initial_stop": stop, "trailing_stop": stop})()
    runner_unlock_ratio, base_lock_pct = _runner_execution_parameters()
    lock_pct = sc._dynamic_runner_lock_pct(base_lock_pct, regime)
    target_dist = max(0.0, target - entry)
    arm_pts = max(0.40 * risk, 500.0 / max(1, qty))
    gb_ratio = 0.50
    hedge_frac = float(getattr(get_settings(), "pullback_hedge_size_fraction", 0.5) or 0.5)

    peak = entry
    runner = False
    hedge = None          # {"df","entry_px","ts","low"}
    hedge_pnl = 0.0
    hedge_count = 0
    pullback_low = None

    def _close_hedge(ts) -> float:
        nonlocal hedge
        if hedge is None:
            return 0.0
        at = hedge["df"][hedge["df"]["timestamp"] <= ts]
        px = float(at.iloc[-1]["close"]) if len(at) else hedge["entry_px"]
        pnl = (px - hedge["entry_px"]) * max(1, int(qty * hedge_frac))
        hedge = None
        return pnl

    for _, b in bars.iterrows():
        ts, hi, lo = b["timestamp"], float(b["high"]), float(b["low"])
        peak_gain = peak - entry

        # ---- parent stop / lock (identical rules to sim_runner) ----
        if runner:
            eff_stop = entry + peak_gain * lock_pct
            if lo <= eff_stop:
                hedge_pnl += _close_hedge(ts)          # hedge never outlives parent
                return max(eff_stop, stop), "RUNNER_LOCK", hedge_pnl, hedge_count
        else:
            if stop > 0 and lo <= stop:
                hedge_pnl += _close_hedge(ts)
                return stop, "STOP", hedge_pnl, hedge_count
            if peak_gain >= arm_pts and lo <= entry + peak_gain * gb_ratio:
                hedge_pnl += _close_hedge(ts)
                return entry + peak_gain * gb_ratio, "GIVEBACK", hedge_pnl, hedge_count

        # ---- hedge lifecycle ----
        cur = float(b["close"])
        if hedge is not None:
            pullback_low = min(pullback_low if pullback_low is not None else cur, lo)
            mins = (ts - hedge["ts"]).total_seconds() / 60.0
            rel, _ = sc._pullback_hedge_release(
                current_premium=cur, peak_premium=peak,
                pullback_low=pullback_low, minutes_held=mins,
            )
            if rel:
                hedge_pnl += _close_hedge(ts)
                pullback_low = None
        elif runner:
            arm, _ = sc._pullback_hedge_arm(
                tl, current_premium=cur, peak_premium=peak,
                gain_r=peak_gain / risk, regime=regime, hedges_used=hedge_count,
                runner_stop=entry + peak_gain * lock_pct,
            )
            if arm:
                opp = _atm_opposite(trade, contracts, ts)
                if opp is not None:
                    _, odf = opp
                    at = odf[odf["timestamp"] <= ts]
                    if len(at):
                        hedge = {"df": odf, "entry_px": float(at.iloc[-1]["close"]), "ts": ts}
                        pullback_low = lo
                        hedge_count += 1

        # ---- peak / runner engagement ----
        peak = max(peak, hi)
        peak_gain = peak - entry
        if not runner:
            reached_t1 = target_dist > 0 and peak_gain >= target_dist * runner_unlock_ratio
            big_ok, _ = sc._big_move_expected(tl, gain_r=peak_gain / risk, regime=regime)
            if reached_t1 or big_ok:
                runner = True
            elif target > 0 and hi >= target:
                hedge_pnl += _close_hedge(ts)
                return target, "TARGET", hedge_pnl, hedge_count

    last_ts = bars.iloc[-1]["timestamp"] if len(bars) else None
    hedge_pnl += _close_hedge(last_ts)
    return (float(bars.iloc[-1]["close"]) if len(bars) else entry), "CLOSE", hedge_pnl, hedge_count


def run_day(day: str, *, quiet: bool = False) -> dict:
    """Backtest one session; returns totals (used by --all aggregate mode)."""
    contracts = load_today_contracts(day)
    trades = load_trades(day)
    sc = _sc()
    settings = get_settings()
    tot_base_gross = tot_run_gross = tot_hedged_gross = 0.0
    tot_base_costs = tot_run_costs = 0.0
    matched = 0
    hedges = 0
    for t in trades:
        m = match_contract(t, contracts)
        if m is None:
            continue
        _, df = m
        bars = _bars_from_entry(df, t["entry_ts"])
        if len(bars) < 2:
            continue
        matched += 1
        qty = LOT_SIZE.get(t["instrument"], 1)
        bx, _ = sim_old(t, bars, qty)
        rx, _ = sim_runner(t, bars, sc, qty)
        base_gross, base_costs, _ = option_replay_pnl(
            t["entry"], bx, qty, t["instrument"], settings
        )
        run_gross, run_costs, _ = option_replay_pnl(
            t["entry"], rx, qty, t["instrument"], settings
        )
        tot_base_gross += base_gross
        tot_base_costs += base_costs
        tot_run_gross += run_gross
        tot_run_costs += run_costs
        hx, _, hpnl, hcount = sim_runner_hedged(t, bars, sc, qty, contracts)
        tot_hedged_gross += (hx - t["entry"]) * qty + hpnl
        hedges += hcount
    tot_base = tot_base_gross - tot_base_costs
    tot_run = tot_run_gross - tot_run_costs
    res = {
        "date": day,
        "trades": len(trades),
        "matched": matched,
        "old": tot_base,
        "runner": tot_run,
        "old_gross": tot_base_gross,
        "runner_gross": tot_run_gross,
        "old_costs": tot_base_costs,
        "runner_costs": tot_run_costs,
        # Hedge legs lack archived executable bid/ask fills, so this remains an
        # explicitly gross exploratory number and never drives certification.
        "hedged_gross": tot_hedged_gross,
        "hedges": hedges,
        "delta": tot_run - tot_base,
        "gross_delta": tot_run_gross - tot_base_gross,
        "hedge_delta_gross": tot_hedged_gross - tot_run_gross,
    }
    if not quiet:
        print(f"{day}  trades={len(trades):3} matched={matched:3}  "
              f"OLDnet={tot_base:9,.0f}  RUNnet={tot_run:9,.0f}  NDELTA={res['delta']:+9,.0f}"
              f"  | HEDGEDgross={tot_hedged_gross:9,.0f} (n={hedges:2})")
    return res


def run_all(min_r_label: str = "") -> None:
    """Aggregate every archived/available session — the multi-day validation view."""
    days = sorted({Path(p).stem for p in glob.glob(str(ROOT / "data_store" / "rtracker" / "events" / "2026-*.jsonl"))})
    print(f"Multi-session validation {min_r_label}({len(days)} sessions)\n")
    rows = [run_day(d) for d in days]
    rows = [r for r in rows if r["matched"] > 0]
    if not rows:
        print("no replayable sessions")
        return
    tb = sum(r["old"] for r in rows)
    tr = sum(r["runner"] for r in rows)
    tbg = sum(r["old_gross"] for r in rows)
    trg = sum(r["runner_gross"] for r in rows)
    tbc = sum(r["old_costs"] for r in rows)
    trc = sum(r["runner_costs"] for r in rows)
    thg = sum(r["hedged_gross"] for r in rows)
    nh = sum(r["hedges"] for r in rows)
    wins = sum(1 for r in rows if r["delta"] > 0.005)
    losses = sum(1 for r in rows if r["delta"] < -0.005)
    print("\n" + "=" * 78)
    print(f"SESSIONS replayed: {len(rows)}   better: {wins}   worse: {losses}   "
          f"neutral: {len(rows) - wins - losses}")
    print(f"OLD    gross: Rs {tbg:,.0f}   costs/slippage: Rs {tbc:,.0f}   NET: Rs {tb:,.0f}")
    print(f"RUNNER gross: Rs {trg:,.0f}   costs/slippage: Rs {trc:,.0f}   NET: Rs {tr:,.0f}")
    print(f"NET DELTA (runner-old): Rs {tr - tb:+,.0f}   (avg {(tr - tb) / len(rows):+,.0f}/session)")
    hwin = sum(1 for r in rows if r["hedge_delta_gross"] > 0.005)
    hlose = sum(1 for r in rows if r["hedge_delta_gross"] < -0.005)
    print(f"HEDGED exploratory GROSS: Rs {thg:,.0f}   ({nh} hedges armed; better {hwin} / worse {hlose} sessions)")
    print(f"HEDGE gross delta: Rs {thg - trg:+,.0f}   (not certification-grade without executable hedge costs)")


def main() -> None:
    args = [a for a in sys.argv[1:]]
    day = "2026-07-24"
    min_r = None
    for a in args:
        if a.startswith("--min-r="):
            min_r = float(a.split("=", 1)[1])
        elif re.match(r"\d{4}-\d{2}-\d{2}", a):
            day = a
    label = ""
    if min_r is not None:
        from config.settings import get_settings
        get_settings().trend_runner_min_peak_r_multiple = min_r
        label = f"[min_r={min_r}] "
        print(f"[override] trend_runner_min_peak_r_multiple = {min_r}")
    if "--all" in args:
        run_all(label)
        return
    contracts = load_today_contracts(day)
    trades = load_trades(day)
    sc = _sc()
    print(f"Session {day}: {len(trades)} accepted trades, {len(contracts)} option contracts with intraday data\n")
    rows = []
    settings = get_settings()
    tot_base = tot_run = 0.0
    matched = 0
    hedges = 0
    for t in trades:
        m = match_contract(t, contracts)
        if m is None:
            rows.append((t, None, None, None, None, "NO_CONTRACT_MATCH"))
            continue
        name, df = m
        bars = _bars_from_entry(df, t["entry_ts"])
        if len(bars) < 2:
            rows.append((t, name, None, None, None, "NO_BARS"))
            continue
        matched += 1
        qty = LOT_SIZE.get(t["instrument"], 1)
        bx, br = sim_old(t, bars, qty)
        rx, rr = sim_runner(t, bars, sc, qty)
        _, _, base_pnl = option_replay_pnl(
            t["entry"], bx, qty, t["instrument"], settings
        )
        _, _, run_pnl = option_replay_pnl(
            t["entry"], rx, qty, t["instrument"], settings
        )
        tot_base += base_pnl
        tot_run += run_pnl
        rows.append((t, name, base_pnl, br, run_pnl, rr))

    print(f"{'inst':11}{'tf':6}{'reg/adx':16}{'entry':>8}{'OLD':>9}{'why':>10}{'RUNNER':>10}{'why':>13}{'delta':>9}")
    for t, name, bp, br, rp, rr in rows:
        if bp is None:
            print(f"{t['instrument']:11}{str(t['timeframe']):6}{'':16}{t['entry']:8.1f}   ---- {rr}")
            continue
        d = rp - bp
        print(f"{t['instrument']:11}{str(t['timeframe']):6}{(t['raw_regime'][:10]+'/'+str(int(t['adx']))):16}"
              f"{t['entry']:8.1f}{bp:9.0f}{br:>10}{rp:10.0f}{rr:>13}{d:9.0f}")
    print("\n" + "=" * 78)
    print(f"MATCHED {matched}/{len(trades)} trades (real premium paths)")
    print(f"OLD    (stop/target + give-back guard) total: Rs {tot_base:,.0f}")
    print(f"RUNNER (let-winners-run, Phase 1+2)    total: Rs {tot_run:,.0f}")
    print(f"DELTA  (runner - old):                       Rs {tot_run - tot_base:,.0f}")


if __name__ == "__main__":
    main()
