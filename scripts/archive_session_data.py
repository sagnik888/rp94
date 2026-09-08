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
ARCHIVE_SCHEMA_VERSION = 3
CONTRACT_OWNERSHIP_VERSION = "shadow-outcome-session-date.v1"


def event_ledger_identity(day: str) -> dict:
    """Stable identity of the event ledger used to build an archive manifest."""
    try:
        stat = (EVENTS / f"{day}.jsonl").stat()
        return {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        return {"exists": False, "size": 0, "mtime_ns": 0}


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


def contract_evidence_events(events: list[dict], day: str | None = None) -> list[dict]:
    """Events allowed to demand contract candles for ``day``.

    Shadow outcomes can be finalized after a restart and are then appended to the next
    session's JSONL file. Their embedded ``session_date`` still names the signal day.
    Keep those rows in ``trades.json`` for forensics, but do not make a prior-session
    contract (possibly already expired) part of the next session's candle coverage.
    Missing or malformed ownership remains fail-closed and is included.
    """
    if not day:
        return list(events)
    scoped: list[dict] = []
    for event in events:
        if str(event.get("decision") or "").upper() == "SHADOW_OUTCOME":
            shadow = event.get("shadow_outcome")
            source_day = (
                str(shadow.get("session_date") or "").strip()
                if isinstance(shadow, dict)
                else ""
            )
            if source_day and source_day != day:
                continue
        scoped.append(event)
    return scoped


def traded_symbols(events: list[dict], day: str | None = None) -> set[str]:
    """Contract symbols owned by the archived session, if present."""
    syms: set[str] = set()
    for e in contract_evidence_events(events, day):
        c = e.get("contract")
        if isinstance(c, dict):
            s = str(c.get("trading_symbol") or "").strip()
            if s:
                syms.add(s)
    return syms


def contract_tokens(events: list[dict], day: str | None = None) -> dict[str, str]:
    """{trading_symbol: symbol_token}, as the live session itself recorded it.

    The back-fill needs a broker token per contract, and asking the data provider for one
    does not work: main._contract_token_for probes get_token_for_symbol / resolve_token /
    symbol_token / contract_master and MarketDataProvider exposes none of them (it has
    get_option_token, which takes name/strike/option_type rather than a tradingsymbol).
    So every fetch returned "" and the back-fill filled nothing.

    The event stream already carries the exact token each trade was placed with, which is
    better than re-deriving one: it is the identity the session actually used, and it
    needs no expiry or strike parsing. Every traded contract in the 2026-08-11..13
    sessions has one.
    """
    out: dict[str, str] = {}
    for e in contract_evidence_events(events, day):
        c = e.get("contract")
        if not isinstance(c, dict):
            continue
        symbol = str(c.get("trading_symbol") or "").strip()
        token = str(c.get("symbol_token") or "").strip()
        if symbol and token:
            out.setdefault(symbol, token)
    return out


def missing_contract_symbols(day: str, events: list[dict]) -> list[str]:
    """Traded contracts whose 1-minute premium is NOT in the live candle cache.

    The archiver can only ever copy what the cache already holds, and the cache holds
    only what the live session happened to fetch. For OPTION contracts that turned out
    to be nothing at all: as of 2026-08-14 the cache had no CE/PE file newer than
    2026-07-31, so every session after that archived index candles and no premium, and
    became permanently unreplayable.

    This lists what is missing so an authenticated caller can fetch it BEFORE the
    archive snapshot runs. The archiver itself is a child process with no broker
    session, which is why it cannot do the fetching.
    """
    missing: list[str] = []
    for sym in sorted(traded_symbols(events, day)):
        if not sym.upper().endswith(("CE", "PE")):
            continue
        path = CANDLES / f"{sym}_1min.csv"
        if day_slice(path, day) is None:
            missing.append(sym)
    return missing


def backfill_contract_history(day: str, fetcher, events: list[dict] | None = None,
                              *, quiet: bool = False) -> list[str]:
    """Fetch each traded contract's 1-minute premium into the live candle cache.

    `fetcher(symbol) -> bool` is supplied by the caller, which owns the authenticated
    data provider. Returns the symbols that landed. Never raises: a session that cannot
    be back-filled must still archive whatever it does have.
    """
    events = events if events is not None else load_events(day)
    wanted = missing_contract_symbols(day, events)
    if not wanted:
        return []
    filled: list[str] = []
    for sym in wanted:
        try:
            if fetcher(sym):
                filled.append(sym)
        except Exception as exc:
            _log(f"[backfill] {sym} failed: {exc}", quiet)
    _log(f"[backfill] {len(filled)}/{len(wanted)} contract histories fetched for {day}", quiet)
    return filled


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
    """Return replay-compatible 1-minute premium files for the requested session.

    The exit-lifecycle replay reads only ``*CE_1min.csv``/``*PE_1min.csv``. A 5-minute
    cache file for the right contract therefore cannot satisfy archive coverage.
    """
    files: list[Path] = []
    if symbols:
        for sym in symbols:
            path = CANDLES / f"{sym}_1min.csv"
            if path.exists():
                files.append(path)
        # Exact identity is available, so an empty result means the traded contracts
        # are missing. Falling back here would archive unrelated contracts and could
        # make a file-count coverage check look complete.
        return sorted(set(files))
    files = [Path(p) for p in
             glob.glob(str(CANDLES / "*CE_1min.csv"))
             + glob.glob(str(CANDLES / "*PE_1min.csv"))]
    return sorted(set(files))


def archived_contract_symbols(filenames: list[str], symbols: set[str]) -> set[str]:
    """Exact traded identities represented by replay-compatible 1-minute files."""
    return {
        symbol
        for symbol in symbols
        if f"{symbol}_1min.csv" in filenames
    }


def _prior_repair_status(out_dir: Path) -> dict:
    """Read prior retry metadata without making an archive attempt depend on it."""
    try:
        prior = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        status = prior.get("repair_status") if isinstance(prior, dict) else None
        return dict(status) if isinstance(status, dict) else {}
    except Exception:
        return {}


def _safe_nonnegative_int(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return max(0, int(default))


def archive_day(day: str, *, force: bool = False, quiet: bool = False) -> dict:
    if market_hours_now() and not force:
        _log(f"[skip] market hours — refusing heavy archive IO (use --force to override)", quiet)
        return {"date": day, "skipped": "market_hours"}

    # Outcomes can settle shortly after the first post-close archive. Read against a
    # stable file identity so the manifest never claims to cover a ledger revision it
    # did not actually parse. A later append changes the identity and re-queues the day.
    source_event_identity = event_ledger_identity(day)
    ledger_stable = False
    for _ in range(3):
        before = event_ledger_identity(day)
        events = load_events(day)
        after = event_ledger_identity(day)
        source_event_identity = after
        if before == after:
            ledger_stable = True
            break
    if not events:
        _log(f"[skip] no rtracker events for {day}", quiet)
        return {"date": day, "skipped": "no_events"}

    out_dir = ARCHIVE / day
    (out_dir / "candles").mkdir(parents=True, exist_ok=True)
    prior_repair = _prior_repair_status(out_dir)

    (out_dir / "trades.json").write_text(
        json.dumps(events, separators=(",", ":")), encoding="utf-8"
    )

    contract_events = contract_evidence_events(events, day)
    symbols = traded_symbols(contract_events)
    copied_contracts: set[str] = set()
    for path in candidate_contract_files(day, symbols):
        sub = day_slice(path, day)
        if sub is None:
            continue
        dest = out_dir / "candles" / path.name
        sub.to_csv(dest, index=False)
        copied_contracts.add(path.name)

    # A rerun may happen days later because outcomes were appended after the original
    # archive. The live cache is rolling and may no longer contain that session. Treat
    # already-frozen, day-validated files as authoritative instead of downgrading or
    # overwriting good historical coverage with whatever happens to be cached today.
    if symbols:
        contract_names = {f"{symbol}_1min.csv" for symbol in symbols}
    else:
        contract_names = copied_contracts | {
            path.name
            for path in (out_dir / "candles").glob("*_1min.csv")
            if path.stem.upper().endswith(("CE_1MIN", "PE_1MIN"))
        }
    archived = sorted(
        name
        for name in contract_names
        if (out_dir / "candles" / name).exists()
        and day_slice(out_dir / "candles" / name, day) is not None
    )

    # Index spot series (needed for regime/context replay)
    spot = []
    for idx in INDICES:
        for tf in ("1min", "5min", "15min"):
            p = CANDLES / f"{idx}_{tf}.csv"
            dest = out_dir / "candles" / p.name
            if p.exists():
                sub = day_slice(p, day)
                if sub is not None:
                    sub.to_csv(dest, index=False)
            if dest.exists() and day_slice(dest, day) is not None:
                spot.append(p.name)

    decisions = {}
    for e in events:
        decisions[str(e.get("decision") or "?")] = decisions.get(str(e.get("decision") or "?"), 0) + 1

    archived_symbols = archived_contract_symbols(archived, symbols)
    missing_symbols = sorted(symbols - archived_symbols)
    skipped = sum(
        1
        for symbol in missing_symbols
        if (CANDLES / f"{symbol}_1min.csv").exists()
    )

    manifest = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "contract_ownership_version": CONTRACT_OWNERSHIP_VERSION,
        "source_event_identity": source_event_identity,
        "date": day,
        "archived_at": datetime.now().isoformat(timespec="seconds"),
        "events": len(events),
        "decisions": decisions,
        "cross_session_contract_events_excluded": len(events) - len(contract_events),
        "traded_symbols_recorded": sorted(symbols),
        "contract_files_archived": len(archived),
        "contract_symbols_archived": sorted(archived_symbols),
        "missing_contract_symbols": missing_symbols,
        "spot_files_archived": len(spot),
        "contract_files_without_day_bars": skipped,
        "has_contract_identity": bool(symbols),
        "coverage_note": (
            "exact contract identity present"
            if symbols else
            "legacy session: no contract identity in events — replay must premium-match"
        ),
    }
    coverage_errors = []
    if not ledger_stable:
        coverage_errors.append("event ledger changed repeatedly while archive was reading it")
    if len(spot) < len(INDICES) * 3:
        coverage_errors.append(
            f"spot coverage {len(spot)}/{len(INDICES) * 3} index/timeframe files"
        )
    if missing_symbols:
        coverage_errors.append(
            f"contract symbol coverage {len(archived_symbols)}/{len(symbols)}; "
            f"missing {', '.join(missing_symbols)}"
        )
    if skipped:
        coverage_errors.append(f"{skipped} contract file(s) had no bars for {day}")
    manifest["complete"] = not coverage_errors
    manifest["coverage_errors"] = coverage_errors

    # The child has no authenticated provider, so it cannot decide whether a missing
    # contract can be repaired.  It does make the gap machine-visible even in --quiet
    # mode; the parent scheduler adds fetch outcomes, retry counts and any terminal
    # classification after it has attempted the authenticated backfill.
    prior_rows = prior_repair.get("missing_contracts") or {}
    prior_rows = prior_rows if isinstance(prior_rows, dict) else {}
    missing_rows = {
        symbol: dict(prior_rows.get(symbol) or {
            "state": "retryable",
            "reason": "no_cached_1min_bars",
        })
        for symbol in missing_symbols
    }
    manifest["repair_status"] = {
        "state": "complete" if manifest["complete"] else "retryable",
        "classification": "none" if manifest["complete"] else "retryable",
        "attempts": (
            0
            if manifest["complete"]
            else _safe_nonnegative_int(prior_repair.get("attempts"))
        ),
        "archive_failures": (
            0
            if manifest["complete"]
            else _safe_nonnegative_int(prior_repair.get("archive_failures"))
        ),
        "missing_contracts": missing_rows,
        "next_action": (
            "none"
            if manifest["complete"]
            else "backfill exact 1-minute contract histories, then rerun archive"
        ),
    }
    manifest_path = out_dir / "manifest.json"
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(temporary, manifest_path)
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
    failures = []
    for day in days:
        result = archive_day(day, force=force, quiet=quiet)
        if result.get("skipped") or result.get("complete") is False:
            failures.append({"date": day, "result": result})
    if failures:
        # Quiet controls routine success chatter, never evidence of an incomplete
        # archive.  The scheduler captures stderr and turns this into a visible repair
        # or terminal-classification log instead of silently retrying forever.
        for failure in failures:
            print(
                "[archive-result] "
                + json.dumps({"status": "incomplete", **failure}, default=str),
                file=sys.stderr,
            )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
