"""Stop-out re-entry backtest (2026-07-25).

For every STOP-type exit in the archived sessions, ask the REAL gate
(Scanner._stop_out_reentry_allowed) whether a second attempt is allowed. When it is,
simulate that re-entry on the REAL 1-minute option-premium path: enter a fresh-ATM
contract `--delay` minutes after the stop, in the SAME direction, with the same stop
percentage as the original trade, and run it through the same exit logic the live
system uses (sim_runner from backtest_exit_lifecycle).

Exits carry ADX but not regime, so the live regime at re-entry time is proxied by the
nearest ACCEPTED event for that instrument (which records raw_regime).

Usage: python scripts/backtest_stop_out_reentry.py [--delay=5] [--window=30] [--adx=25]
"""
from __future__ import annotations

import glob
import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scanner as scanner_module  # noqa: E402
from config.settings import get_settings as get_runtime_settings  # noqa: E402
from scanner import Scanner  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("bt", ROOT / "scripts" / "backtest_exit_lifecycle.py")
bt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bt)

STOP_REASONS = {"STOP_HIT", "STOP_LOSS", "TRAILING_STOP", "EARLY_LOSS_CUT", "LOSS_THREAT_EXIT"}


def _ts(value):
    ts = bt._market_timestamp(value)
    return None if pd.isna(ts) else ts.to_pydatetime()


def _option_replay_pnl(
    entry_price: float,
    exit_price: float,
    quantity: int,
    instrument: str,
    settings=None,
) -> tuple[float, float, float]:
    """Return gross, modeled round-trip costs, and net replay P&L.

    There are no archived executable bid/ask fills for these hypothetical
    re-entries.  Use the existing shared accounting model rather than inventing
    another spread assumption; it combines configured costs with the same
    static/calibrated option-slippage policy used elsewhere in the system.
    """
    accounting_settings = settings if settings is not None else get_runtime_settings()
    return bt.option_replay_pnl(
        entry_price, exit_price, quantity, instrument, accounting_settings
    )


def _fresh_atm(instrument: str, opt_type: str, contracts: dict, when) -> tuple[str, pd.DataFrame] | None:
    """Most actively traded contract of the wanted side with bars at `when`."""
    best, best_vol = None, -1.0
    for name, df in contracts.items():
        if bt._instrument_of(name) != instrument or bt._type_of(name) != opt_type:
            continue
        at = df[df["timestamp"] <= when]
        if not len(at):
            continue
        vol = float(at.iloc[-1].get("volume", 0) or 0)
        if vol > best_vol:
            best_vol, best = vol, (name, df)
    return best


def main() -> None:
    args = sys.argv[1:]
    delay = 5.0
    window, adx_floor = 30.0, 25.0
    for a in args:
        if a.startswith("--delay="):
            delay = float(a.split("=")[1])
        elif a.startswith("--window="):
            window = float(a.split("=")[1])
        elif a.startswith("--adx="):
            adx_floor = float(a.split("=")[1])

    accounting_settings = get_runtime_settings()
    scanner_module.get_settings = lambda: SimpleNamespace(
        stop_out_reentry_enabled=True,
        stop_out_reentry_window_minutes=window,
        stop_out_reentry_max_per_index=1,
        stop_out_reentry_min_adx=adx_floor,
        # runner settings used by sim_runner
        trend_runner_let_winners_run_enabled=True,
        trend_runner_min_peak_r_multiple=0.4,
        trend_runner_big_move_min_adx=25.0,
        trend_runner_dynamic_lock_enabled=True,
        trend_runner_lock_pct_high_vol=0.85,
        pullback_hedge_enabled=False,
    )

    print(f"Stop-out re-entry backtest (delay={delay:.0f}m window={window:.0f}m adx>={adx_floor:.0f})\n")
    days = sorted({Path(p).stem for p in glob.glob(str(ROOT / "data_store" / "rtracker" / "events" / "2026-*.jsonl"))})
    total_gross_pnl = total_costs = total_pnl = 0.0
    taken = wins = 0
    skipped_no_data = 0
    per_day: dict[str, float] = {}

    for day in days:
        events = [json.loads(l) for l in open(ROOT / "data_store" / "rtracker" / "events" / f"{day}.jsonl",
                                              encoding="utf-8") if l.strip()]
        stops = [e for e in events
                 if str(e.get("decision", "")).upper() == "EXIT"
                 and str(e.get("exit_reason", "")).upper() in STOP_REASONS]
        if not stops:
            continue
        accepted = [e for e in events if str(e.get("decision", "")).upper() == "ACCEPTED"]
        contracts = bt.load_today_contracts(day)
        used: dict[str, int] = {}
        day_pnl = 0.0
        day_taken = 0

        for e in stops:
            inst = str(e.get("instrument", "")).split()[0]
            direction = str(e.get("direction", "")).upper()
            xt = _ts(e.get("exit_timestamp") or e.get("timestamp"))
            if not xt:
                continue
            adx = float((e.get("regime") or {}).get("adx") or 0.0)

            # live-regime proxy: nearest ACCEPTED for this instrument
            raw, best_gap = "", 1e9
            for a in accepted:
                if str(a.get("instrument", "")).split()[0] != inst:
                    continue
                at = _ts(a.get("signal_timestamp") or a.get("timestamp"))
                if not at:
                    continue
                gap = abs((at - xt).total_seconds()) / 60.0
                if gap < best_gap:
                    best_gap, raw = gap, str((a.get("regime") or {}).get("raw_regime") or "").upper()
            if best_gap > 60:
                raw = ""

            sc = Scanner.__new__(Scanner)
            sc.trades = SimpleNamespace(closed_trades=[SimpleNamespace(
                instrument=inst, direction=direction,
                exit_reason=str(e.get("exit_reason", "")).upper(), exit_time=xt,
                pnl=float(e.get("pnl") or 0.0))])
            sc.latest_regimes = {inst: raw}
            sc._stop_out_reentries = dict(used)
            ok, _ = sc._stop_out_reentry_allowed(inst, direction, xt + timedelta(minutes=delay),
                                                 adx_value=adx)
            if not ok:
                continue

            opt_type = "CE" if direction == "LONG" else "PE"
            when = xt + timedelta(minutes=delay)
            match = _fresh_atm(inst, opt_type, contracts, when)
            if match is None:
                skipped_no_data += 1
                continue
            _, df = match
            bars = df[df["timestamp"] >= when].reset_index(drop=True)
            if len(bars) < 2:
                skipped_no_data += 1
                continue

            entry = float(bars.iloc[0]["close"])
            orig_price = float(e.get("price") or 0.0)
            orig_stop = float(e.get("stop") or 0.0)
            stop_pct = ((orig_price - orig_stop) / orig_price) if orig_price > 0 and orig_stop > 0 else 0.15
            stop_pct = min(0.5, max(0.03, stop_pct))
            trade = {
                "instrument": inst, "direction": direction, "option_type": opt_type,
                "entry": entry, "stop": entry * (1 - stop_pct),
                "target": entry * (1 + stop_pct * 1.5),
                "raw_regime": raw, "adx": adx,
            }
            qty = bt.LOT_SIZE.get(inst, 1)
            px, why = bt.sim_runner(trade, bars, bt._sc(), qty)
            gross_pnl, costs, net_pnl = _option_replay_pnl(
                entry, px, qty, inst, accounting_settings,
            )
            day_pnl += net_pnl
            total_gross_pnl += gross_pnl
            total_costs += costs
            total_pnl += net_pnl
            taken += 1
            day_taken += 1
            wins += 1 if net_pnl > 0 else 0
            used[inst] = used.get(inst, 0) + 1
            print(f"  {day} {inst:11}{direction:6}{raw:15} entry={entry:7.1f} exit={px:7.1f} "
                  f"{why:12} gross={gross_pnl:+9,.0f} costs={costs:7,.0f} net={net_pnl:+9,.0f}")
        if day_taken:
            per_day[day] = day_pnl

    print("\n" + "=" * 78)
    print(f"RE-ENTRIES taken: {taken}   wins: {wins}   losses: {taken - wins}"
          f"   (skipped, no contract data: {skipped_no_data})")
    if taken:
        print(f"WIN RATE: {wins / taken:.0%}")
        print(f"GROSS P&L: Rs {total_gross_pnl:+,.0f}   "
              f"MODELED COSTS/SLIPPAGE: Rs {total_costs:,.0f}")
        print(f"TOTAL NET P&L of stop-out re-entries: Rs {total_pnl:+,.0f}   "
              f"(avg {total_pnl / taken:+,.0f}/trade)")
        pos = sum(1 for v in per_day.values() if v > 0)
        neg = sum(1 for v in per_day.values() if v < 0)
        print(f"SESSIONS with re-entries: {len(per_day)}   better: {pos}   worse: {neg}")


if __name__ == "__main__":
    main()
