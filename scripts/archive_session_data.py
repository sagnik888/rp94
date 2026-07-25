"""Session data archiver — builds the multi-day backtest validation set.

WHY: offline exit-lifecycle backtests need each session's REAL option-premium path.
The live candle cache (data_store/candles/*.csv) only holds whatever contracts were
last fetched, so a traded strike's intraday bars can be overwritten or never stored —
on 2026-07-24 only ~40% of trades could be matched to real premium data.

WHAT: snapshots, per session date, into data_store/session_archive/<date>/:
  * trades.json          — that day's rtracker decisions (incl. contract identity)
  * candles/<sym>.csv    — day-sliced 1-min bars for every contract the day traded
                           (plus the index spot series), copied out of the live cache
  * manifest.json        — coverage stats so gaps are visible rather than silent

SAFETY: read-only w.r.t. live trading state. Never runs heavy IO during market hours
unless --force is passed (mirrors the log-cleaner's market-hours guard).

Usage:
  python scripts/archive_session_data.py [YYYY-MM-DD] [--force] [--quiet]
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import date, datetime, time as dt_time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CANDLES = ROOT / "data_store" / "candles"
EVENTS = ROOT / "data_store" / "rtracker" / "events"
ARCHIVE = ROOT / "data_store" / "session_archive"
INDICES = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
MARKET_OPEN, MARKET_CLOSE = dt_time(9, 0), dt_time(15, 45)


def _log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg)


def market_hours_now() -> bool:
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE and date.today().weekday() < 5


def load_events(day: str) -> list[dict]:
    path = EVENTS / f"{day}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.open(encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def traded_symbols(events: list[dict]) -> set[str]:
    """Contract symbols the session actually traded/considered (new schema), if present."""
    syms: set[str] = set()
    for e in events:
        c = e.get("contract")
        if isinstance(c, dict):
            s = str(c.get("trading_symbol") or "").strip()
            if s:
                syms.add(s)
    return syms


def day_slice(csv_path: Path, day: str) -> pd.DataFrame | None:
    try:
        if csv_path.stat().st_size < 120:
            return None
        df = pd.read_csv(csv_path)
        if "timestamp" not in df.columns or not len(df):
            return None
        ts = pd.to_datetime(df["timestamp"], errors="coerce")
        sub = df[ts.dt.strftime("%Y-%m-%d") == day]
        return sub if len(sub) else None
    except Exception:
        return None


def candidate_contract_files(day: str, symbols: set[str]) -> list[Path]:
    """Prefer exact traded symbols; otherwise fall back to every option contract that
    has bars for the day (so older sessions without contract identity still archive)."""
    files: list[Path] = []
    if symbols:
        for sym in symbols:
            files.extend(Path(p) for p in glob.glob(str(CANDLES / f"{sym}_*.csv")))
    if not files:
        files = [Path(p) for p in
                 glob.glob(str(CANDLES / "*CE_*.csv")) + glob.glob(str(CANDLES / "*PE_*.csv"))]
    return sorted(set(files))


def archive_day(day: str, *, force: bool = False, quiet: bool = False) -> dict:
    if market_hours_now() and not force:
        _log(f"[skip] market hours — refusing heavy archive IO (use --force to override)", quiet)
        return {"date": day, "skipped": "market_hours"}

    events = load_events(day)
    if not events:
        _log(f"[skip] no rtracker events for {day}", quiet)
        return {"date": day, "skipped": "no_events"}

    out_dir = ARCHIVE / day
    (out_dir / "candles").mkdir(parents=True, exist_ok=True)

    (out_dir / "trades.json").write_text(
        json.dumps(events, separators=(",", ":")), encoding="utf-8"
    )

    symbols = traded_symbols(events)
    archived, skipped = [], 0
    for path in candidate_contract_files(day, symbols):
        sub = day_slice(path, day)
        if sub is None:
            skipped += 1
            continue
        dest = out_dir / "candles" / path.name
        sub.to_csv(dest, index=False)
        archived.append(path.name)

    # Index spot series (needed for regime/context replay)
    spot = []
    for idx in INDICES:
        for tf in ("1min", "5min", "15min"):
            p = CANDLES / f"{idx}_{tf}.csv"
            if not p.exists():
                continue
            sub = day_slice(p, day)
            if sub is None:
                continue
            sub.to_csv(out_dir / "candles" / p.name, index=False)
            spot.append(p.name)

    decisions = {}
    for e in events:
        decisions[str(e.get("decision") or "?")] = decisions.get(str(e.get("decision") or "?"), 0) + 1

    manifest = {
        "date": day,
        "archived_at": datetime.now().isoformat(timespec="seconds"),
        "events": len(events),
        "decisions": decisions,
        "traded_symbols_recorded": sorted(symbols),
        "contract_files_archived": len(archived),
        "spot_files_archived": len(spot),
        "contract_files_without_day_bars": skipped,
        "has_contract_identity": bool(symbols),
        "coverage_note": (
            "exact contract identity present"
            if symbols else
            "legacy session: no contract identity in events — replay must premium-match"
        ),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _log(
        f"[ok] {day}: {len(events)} events, {len(archived)} contract + {len(spot)} spot "
        f"candle files -> {out_dir}",
        quiet,
    )
    return manifest


def main() -> None:
    args = sys.argv[1:]
    force = "--force" in args
    quiet = "--quiet" in args
    days = [a for a in args if re.match(r"^\d{4}-\d{2}-\d{2}$", a)]
    if not days:
        days = [date.today().isoformat()]
    for day in days:
        archive_day(day, force=force, quiet=quiet)


if __name__ == "__main__":
    main()
