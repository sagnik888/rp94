"""Judge the early-entry build-up shadow (engine/buildup_shadow.py) on real outcomes.

    python scripts/evaluate_buildup_shadow.py                      # live log
    python scripts/evaluate_buildup_shadow.py --archive 2026-10-01 2026-10-09

The second form replays the detector over the archived futures quotes and option chains
(data_store/derivative_observations/<date>.jsonl), with the same settings the live
detector uses, so the rule can be judged before the live log has grown.

For each event it reports what the index did next in the event's direction (move at +5,
+15 and +30 minutes, and the best and worst excursions over 30 minutes), and whether, and
how much later, the system itself signalled the same index and direction
(utbot_data.db signal_decisions, any status).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STORE = ROOT / "data_store"
INDICES = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")


def archive_events(dates, out_path: Path):
    """Replay the detector over archived futures quotes and chains for these dates."""
    from engine.buildup_shadow import BuildupShadow, chain_pcr

    out_path.unlink(missing_ok=True)
    events = []
    for day in dates:
        path = STORE / "derivative_observations" / f"{day}.jsonl"
        if not path.exists():
            print(f"  {day}: no archive")
            continue
        stream = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if '"futures", "kite"' in line:
                    row = json.loads(line)
                    key, values = row["key"], row["row"]
                    stream.append((float(values[0]), "F", key[0], values))
                elif line.startswith('{"kind": "chain"'):
                    row = json.loads(line)
                    stream.append((float(row["at"]), "C", str(row.get("instrument", "")).upper(),
                                   pd.DataFrame(row.get("rows") or [])))
        stream.sort(key=lambda item: item[0])
        shadow = BuildupShadow(log_path=out_path)
        found = 0
        for at, kind, index, payload in stream:
            if index not in INDICES:
                continue
            if kind == "C":
                shadow.observe_chain(index, payload, at=at)
            else:
                # Archived futures rows: [exchange_ts, cumulative volume, OI, price].
                event = shadow.observe_futures(index, payload[0], payload[3], payload[1], payload[2])
                if event:
                    events.append(event)
                    found += 1
        print(f"  {day}: {found} events from {len(stream)} observations")
    return events


def load_candles(index):
    frame = pd.read_csv(STORE / "candles" / f"{index}_1min.csv", usecols=[0, 1, 2, 3, 4])
    stamp = frame.columns[0]
    frame[stamp] = pd.to_datetime(frame[stamp])
    return frame.set_index(stamp).sort_index()


def system_signals():
    db = sqlite3.connect(f"file:{STORE / 'utbot_data.db'}?mode=ro", uri=True)
    rows = db.execute("select timestamp, instrument, timeframe, direction, status from signal_decisions").fetchall()
    out = []
    for ts, instrument, timeframe, direction, status in rows:
        try:
            when = pd.Timestamp(ts)
            when = when.tz_convert("Asia/Kolkata").tz_localize(None) if when.tzinfo else when
        except Exception:
            continue
        side = "LONG" if str(direction).upper() in {"LONG", "BUY"} else "SHORT"
        out.append((when, str(instrument).upper(), side, timeframe, status))
    return pd.DataFrame(out, columns=["at", "index", "direction", "timeframe", "status"])


def evaluate(events):
    if not events:
        print("No events.")
        return None
    candles = {name: load_candles(name) for name in INDICES}
    signals = system_signals()
    rows = []
    for event in events:
        at = pd.Timestamp(event["at"])
        sign = 1 if event["direction"] == "LONG" else -1
        frame = candles[event["index"]]
        # Start at the first minute that opens AFTER the event: the event's own minute
        # opened up to 59s before it and would credit the detector with part of the move.
        after = frame.loc[at.ceil("min"):at + timedelta(minutes=30)]
        if len(after) < 25:
            continue
        start = float(after["open"].iloc[0])

        def move(minutes):
            part = frame.loc[:at + timedelta(minutes=minutes)]
            return (float(part["close"].iloc[-1]) - start) / start * 100 * sign

        high, low = float(after["high"].max()), float(after["low"].min())
        mfe = ((high - start) if sign > 0 else (start - low)) / start * 100
        mae = ((start - low) if sign > 0 else (high - start)) / start * 100
        later = signals[(signals["index"] == event["index"]) & (signals["direction"] == event["direction"])
                        & (signals["at"] >= at) & (signals["at"] <= at + timedelta(minutes=20))]
        lead = price_edge = None
        if not later.empty:
            first = later["at"].min()
            lead = (first - at).total_seconds() / 60
            then = frame.loc[:first]
            if not then.empty:
                price_edge = (float(then["close"].iloc[-1]) - start) / start * 100 * sign
        rows.append(dict(at=at, index=event["index"], direction=event["direction"], legs=event["legs_true"],
                         oi_type=event.get("oi_type"), peers=len(event.get("peers_aligned") or []),
                         m5=move(5), m15=move(15), m30=move(30), mfe=mfe, mae=mae,
                         system_lead_min=lead, system_later_by_pct=price_edge))
    table = pd.DataFrame(rows)
    if table.empty:
        print("No events with 30 minutes of candles after them.")
        return table
    print(f"\n{len(table)} events, {table['at'].dt.date.nunique()} sessions")
    print("Index move in the event's direction (% of index):")
    summary = table[["m5", "m15", "m30", "mfe", "mae"]].describe(percentiles=[.5]).loc[["mean", "50%"]]
    print(summary.round(3).to_string())
    print(f"hit rate (move > 0): +5m {(table.m5 > 0).mean():.2f}  +15m {(table.m15 > 0).mean():.2f}  "
          f"+30m {(table.m30 > 0).mean():.2f};  MFE > MAE: {(table.mfe > table.mae).mean():.2f}")
    for column in ("legs", "peers", "oi_type"):
        print(f"\nby {column}:")
        print(table.groupby(column).agg(n=("m15", "size"), m15=("m15", "mean"), hit15=("m15", lambda s: (s > 0).mean()),
                                        mfe=("mfe", "mean"), mae=("mae", "mean")).round(3).to_string())
    followed = table.dropna(subset=["system_lead_min"])
    print(f"\nsystem signalled the same way within 20 min after {len(followed)}/{len(table)} events; "
          f"median {followed['system_lead_min'].median():.1f} min later, the index already "
          f"{followed['system_later_by_pct'].median():+.3f}% further in the move")
    return table


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--archive", nargs=2, metavar=("FROM", "TO"), help="replay archived sessions FROM..TO")
    parser.add_argument("--events", help="evaluate this events JSONL instead of the live log")
    parser.add_argument("--csv", help="write the per-event table here")
    args = parser.parse_args()
    if args.archive:
        start, end = (datetime.strptime(d, "%Y-%m-%d").date() for d in args.archive)
        dates = [str(start + timedelta(days=i)) for i in range((end - start).days + 1)]
        events = archive_events(dates, STORE / "buildup_shadow_archive_replay.jsonl")
    else:
        path = Path(args.events) if args.events else STORE / "buildup_shadow.jsonl"
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    table = evaluate(events)
    if args.csv and table is not None:
        table.to_csv(args.csv, index=False)


if __name__ == "__main__":
    main()
