#!/usr/bin/env python3
"""Audit session trade ledger consistency from production SQLite (not legacy JSON).

Modern contract (2026-07 audit repair):
  - Primary source: data_store/utbot_data.db trades table
  - Applies same shadow-free + exact-dedup hygiene as dashboard/TM
  - Optional: still accepts a JSON candidates path for offline dumps
  - Reports unmatched open, duplicate ids, shadow book size, PnL totals

Usage:
  python scripts/audit_session_ledger.py
  python scripts/audit_session_ledger.py --date 2026-07-09
  python scripts/audit_session_ledger.py --db data_store/utbot_data.db
  python scripts/audit_session_ledger.py --path path/to/export.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Allow running from repo root without install
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.trade_row_adapter import (  # noqa: E402
    accounting_pnl,
    hygiene_closed_rows,
    is_futures_shadow_row,
)


DEFAULT_DB = "data_store/utbot_data.db"


def _row_to_dict(cursor: sqlite3.Cursor, row: sqlite3.Row) -> Dict[str, Any]:
    keys = [d[0] for d in cursor.description]
    return {k: row[i] for i, k in enumerate(keys)}


def load_trades_from_sqlite(
    db_path: Path,
    session_date: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not db_path.exists():
        return []
    # Audit must never create, migrate, journal, or otherwise mutate its source.
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # Discover columns
    cols = {r[1] for r in cur.execute("PRAGMA table_info(trades)").fetchall()}
    if not cols:
        conn.close()
        return []

    sql = "SELECT * FROM trades WHERE 1=1"
    params: List[Any] = []
    if status:
        sql += " AND UPPER(COALESCE(status,'')) = ?"
        params.append(status.upper())
    if session_date:
        # Match entry calendar day in IST-ish string forms
        sql += (
            " AND ("
            " substr(COALESCE(entry_time,''), 1, 10) = ?"
            " OR date(entry_timestamp, 'unixepoch', 'localtime') = ?"
            " OR substr(COALESCE(exit_time,''), 1, 10) = ?"
            ")"
        )
        params.extend([session_date, session_date, session_date])
    sql += " ORDER BY COALESCE(entry_timestamp, 0), id"
    try:
        rows = cur.execute(sql, params).fetchall()
    except sqlite3.Error:
        # Older schemas without entry_timestamp
        sql2 = "SELECT * FROM trades"
        rows = cur.execute(sql2).fetchall()
        if session_date:
            filtered = []
            for r in rows:
                d = dict(r)
                blob = " ".join(
                    str(d.get(k) or "")
                    for k in ("entry_time", "exit_time", "entry_timestamp")
                )
                if session_date in blob:
                    filtered.append(r)
            rows = filtered
    out = [dict(r) for r in rows]
    conn.close()
    return out


def load_rows_from_json(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("candidates", "trades", "closed", "rows"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
    return []


def _price(row: Dict[str, Any], *keys: str) -> float:
    for key in keys:
        try:
            value = float(row.get(key) or 0.0)
            if value > 0:
                return value
        except Exception:
            continue
    return 0.0


def audit_trade_rows(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(rows or [])
    closed = [
        r
        for r in rows
        if str(r.get("status") or "").upper() == "CLOSED"
        or float(r.get("exit_price") or 0.0) > 0
    ]
    open_rows = [
        r
        for r in rows
        if str(r.get("status") or "").upper() in {"OPEN", "PENDING", ""}
        and float(r.get("exit_price") or 0.0) <= 0
    ]

    headline, hygiene = hygiene_closed_rows(closed)
    shadow_n = int((hygiene.get("futures_shadow") or {}).get("rows") or 0)

    # ID uniqueness
    ids = [str(r.get("id") or "") for r in rows if r.get("id")]
    dup_ids = sorted({i for i in ids if ids.count(i) > 1})

    # Stop/target clamp mismatches on closed exits
    clamp_mismatches = []
    for row in closed:
        reason = str(row.get("exit_reason") or row.get("status") or "").upper()
        exit_px = _price(row, "exit_price", "current_price", "price")
        if "TARGET" in reason:
            expected = _price(row, "target", "spot_target")
        elif "STOP" in reason or "SL" in reason or "TRAILING" in reason:
            expected = _price(row, "trailing_stop", "current_stop", "stop", "spot_stop")
        else:
            continue
        direction = str(row.get("direction") or "LONG").upper()
        crossed_as_expected = False
        if expected > 0 and exit_px > 0:
            if "TARGET" in reason:
                crossed_as_expected = (
                    exit_px >= expected - 0.05
                    if direction == "LONG"
                    else exit_px <= expected + 0.05
                )
            elif "STOP" in reason or "SL" in reason or "TRAILING" in reason:
                crossed_as_expected = (
                    exit_px <= expected + 0.05
                    if direction == "LONG"
                    else exit_px >= expected - 0.05
                )
        if expected > 0 and exit_px > 0 and not crossed_as_expected:
            clamp_mismatches.append(
                {
                    "id": row.get("id"),
                    "instrument": row.get("instrument"),
                    "timeframe": row.get("timeframe"),
                    "reason": reason,
                    "exit_price": exit_px,
                    "expected": expected,
                }
            )

    by_instrument: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"closed": 0, "open": 0, "pnl": 0.0, "shadow": 0}
    )
    for row in headline:
        inst = str(row.get("instrument") or "--").split()[0]
        by_instrument[inst]["closed"] += 1
        by_instrument[inst]["pnl"] += accounting_pnl(row)
    for row in open_rows:
        inst = str(row.get("instrument") or "--").split()[0]
        by_instrument[inst]["open"] += 1
    for row in closed:
        if is_futures_shadow_row(row):
            inst = str(row.get("instrument") or "--").split()[0]
            by_instrument[inst]["shadow"] += 1

    headline_pnl = sum(accounting_pnl(r) for r in headline)
    raw_closed_pnl = sum(accounting_pnl(r) for r in closed)

    return {
        "rows_total": len(rows),
        "open": len(open_rows),
        "closed_raw": len(closed),
        "closed_headline": len(headline),
        "shadow_rows": shadow_n,
        "duplicate_ids": dup_ids[:50],
        "duplicate_id_count": len(dup_ids),
        "headline_pnl": round(headline_pnl, 2),
        "raw_closed_pnl": round(raw_closed_pnl, 2),
        "pnl_inflation_if_raw": round(raw_closed_pnl - headline_pnl, 2),
        "stop_target_clamp_mismatches": len(clamp_mismatches),
        "clamp_mismatch_rows": clamp_mismatches[:20],
        "hygiene": hygiene,
        "by_instrument": {
            k: {**v, "pnl": round(v["pnl"], 2)}
            for k, v in sorted(by_instrument.items())
        },
        "ok": bool(
            len(dup_ids) == 0
            and len(clamp_mismatches) == 0
            and (
                abs(raw_closed_pnl - headline_pnl) < 0.01
                or shadow_n > 0
                or int((hygiene.get("engine_replay") or {}).get("excess_rows") or 0) > 0
            )
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit session trade ledger (SQLite-first)")
    parser.add_argument("--date", default="", help="Session date YYYY-MM-DD (optional filter)")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite path")
    parser.add_argument("--path", default="", help="Optional JSON export instead of SQLite")
    parser.add_argument("--status", default="", help="Optional status filter e.g. CLOSED")
    parser.add_argument("--json-out", default="", help="Write full report JSON")
    args = parser.parse_args()

    session_date = args.date or None
    if args.path:
        source_path = Path(args.path)
        source = str(source_path)
        source_exists = source_path.exists()
        rows = load_rows_from_json(source_path)
        source_kind = "json"
    else:
        source_path = Path(args.db)
        source = str(source_path)
        source_exists = source_path.exists()
        rows = load_trades_from_sqlite(
            source_path,
            session_date=session_date,
            status=args.status or None,
        )
        source_kind = "sqlite"

    result = {
        "source": source,
        "source_kind": source_kind,
        "source_exists": source_exists,
        "date_filter": session_date,
        **audit_trade_rows(rows),
    }
    # Cleaner "ok" signal: hygiene applied, no bad dups
    result["ok"] = bool(result["source_exists"] and result["ok"])
    if not source_exists:
        result["error"] = "source_not_found"
    print(json.dumps(result, indent=2))

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"Wrote {out}", file=sys.stderr)

    raise SystemExit(0 if result["ok"] else (2 if not source_exists else 1))


if __name__ == "__main__":
    main()
