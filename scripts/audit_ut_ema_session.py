"""Reconstruct retained index candles and audit UT/EMA session evidence offline.

This does not fetch quotes, mutate the trading database, or reconstruct lost ticks.
Candidate candles are retained broker history, not necessarily the live input used
by the old running process. The distinction is recorded in every report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.ut_ema_engine import UTEmaEngine


def reconstruct(frame: pd.DataFrame, timeframe: str):
    """Aggregate unique minute opens; count coverage instead of filling holes."""
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("A timestamp index is required")
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError("Minute candles must be ordered and unique")
    minutes = int(timeframe.removesuffix("min"))
    grouped = frame.resample(timeframe, origin="start_day", offset="9h15min")
    bars = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    counts = grouped.close.count().reindex(bars.index)
    # A missing minute cannot masquerade as a complete OHLC bar.
    return bars, counts.eq(minutes)


def audit(root: Path, day: str, instrument: str):
    source = root / "data_store" / "candidate_candles" / f"{instrument}_1min.csv"
    frame = pd.read_csv(source, parse_dates=["timestamp"]).set_index("timestamp")
    report = {"day": day, "instrument": instrument, "candle_source": str(source),
              "candle_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "evidence_basis": "retained broker minute history; original live tick path unavailable",
              "timeframes": {}, "accepted": [], "exact_tradingview_parity": "unproven"}
    day_frame = frame.loc[day]
    flat = day_frame.loc[day_frame[["open", "high", "low", "close"]].nunique(axis=1).eq(1)]
    report["flat_minute_count"] = len(flat)
    report["flat_minute_times"] = [str(t) for t in flat.index]
    output = {}
    for timeframe in ("5min", "15min"):
        bars, complete = reconstruct(frame, timeframe)
        selected = bars.loc[day]
        missing = [str(t) for t in selected.index if not complete.loc[t]]
        result = UTEmaEngine().process(bars, instrument, timeframe, realtime=False,
                                       as_of=f"{day} 16:00")
        events = [event for event in result["utema_events"] if event["bar_time"].startswith(day)]
        report["timeframes"][timeframe] = {"bars": len(selected), "incomplete_bars": missing,
            "historical_events": events,
            "native_entries": sum(event["entry"] for event in events),
            "native_exits": sum(not event["entry"] for event in events),
            "last_bar": {"timestamp": str(selected.index[-1]), **selected.iloc[-1].to_dict()},
            "replay_basis": "Pine historical OHLC fallback; not a recreation of live intrabar signals"}
        output[timeframe] = selected.assign(minute_coverage_complete=complete.reindex(selected.index))
    ledger = root / "data_store" / "rtracker" / "events" / f"{day}.jsonl"
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if (item.get("instrument") == instrument and item.get("source_lane") == "UTEMA"
                    and item.get("decision") == "ACCEPTED"):
                native = item.get("quality", {}).get("checks", {}).get("utema_state", {})
                report["accepted"].append({key: item.get(key) for key in
                    ("timestamp", "timeframe", "direction", "signal_timestamp", "lifecycle_id", "build")}
                    | {"native_state": native})
    for accepted in report["accepted"]:
        events = report["timeframes"].get(accepted["timeframe"], {}).get("historical_events", [])
        side = 1 if accepted["direction"] == "LONG" else -1
        accepted["has_historical_entry_on_same_bar"] = any(
            event["entry"] and event["side"] == side
            and event["bar_time"] == accepted["signal_timestamp"] for event in events)
    return report, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--day", default="2026-10-29")
    parser.add_argument("--instrument", default="NIFTY")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report, bars = audit(args.root, args.day, args.instrument)
    args.output.mkdir(parents=True, exist_ok=True)
    for timeframe, frame in bars.items():
        frame.to_csv(args.output / f"{args.instrument}_{timeframe}_reconstructed.csv")
    (args.output / "audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"accepted": len(report["accepted"]), "flat_minutes": report["flat_minute_count"],
        "timeframes": {key: {k: v for k, v in value.items() if k != "historical_events"}
                       for key, value in report["timeframes"].items()}}, indent=2))


if __name__ == "__main__":
    main()
