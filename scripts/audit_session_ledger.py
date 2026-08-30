#!/usr/bin/env python3
"""Audit session trade ledger consistency from production SQLite (not legacy JSON).

Modern contract (2026-07 audit repair):
  - Primary source: data_store/utbot_data.db trades table
  - Certifies session/live rows separately from mutable engine replay fixtures
  - Applies shadow-free + replay-only dedup hygiene as dashboard/TM
  - Optional: still accepts a JSON candidates path for offline dumps
  - Reports unmatched open, duplicate ids, shadow book size, and scoped PnL totals

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
    is_engine_replay_row,
    is_futures_shadow_row,
    is_simulated_history_row,
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


_TARGET_LEVEL_REASONS = frozenset({
    "TARGET",
    "TARGET_HIT",
    "PROFIT_TARGET",
})
_STOP_LEVEL_REASONS = frozenset({
    "STOP_HIT",
    "STOP_LOSS",
    "SL_HIT",
    "TRAILING_STOP",
    "PROTECTIVE_STOP",
})


def _canonical_exit_reason(row: Dict[str, Any]) -> str:
    raw = str(row.get("exit_reason") or row.get("status") or "").upper().strip()
    return "_".join(raw.replace("-", " ").replace("_", " ").split())


def _exit_level(row: Dict[str, Any]) -> Tuple[str, float]:
    """Return the explicit price-level kind/value for a deterministic exit.

    Not every reason containing the word ``STOP`` is a price stop.  In particular,
    BREAKEVEN_STOP is a P&L/high-water-mark rule: it closes at the current quote and
    does not mutate ``current_stop``.  Treating it as an exchange stop produced the
    two false certification failures in the 2026-08-19 session.

    Price domains also matter.  An option exit price must never be compared with
    ``spot_stop``/``spot_target``; those fields contain the underlying index level.
    """
    reason = _canonical_exit_reason(row)
    is_option = str(row.get("inst_type") or "").upper() == "OPT"
    if reason in _TARGET_LEVEL_REASONS:
        keys = (
            ("current_target", "target", "premium_target")
            if is_option
            else ("current_target", "target", "underlying_target", "spot_target")
        )
        return "TARGET", _price(row, *keys)
    if reason in _STOP_LEVEL_REASONS:
        keys = (
            (
                "protective_trigger",
                "current_stop",
                "trailing_stop",
                "premium_stop",
                "stop",
            )
            if is_option
            else (
                "protective_trigger",
                "current_stop",
                "trailing_stop",
                "stop",
                "underlying_stop",
                "spot_stop",
            )
        )
        return "STOP", _price(row, *keys)
    return "", 0.0


def _is_closed(row: Dict[str, Any]) -> bool:
    if str(row.get("status") or "").upper() == "CLOSED":
        return True
    try:
        return float(row.get("exit_price") or 0.0) > 0
    except (TypeError, ValueError):
        return False


def audit_trade_rows(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(rows or [])
    closed = [r for r in rows if _is_closed(r)]
    shadow_rows = [r for r in rows if is_futures_shadow_row(r)]
    replay_rows = [
        r for r in rows
        if is_engine_replay_row(r) and not is_futures_shadow_row(r)
    ]
    session_rows = [
        r for r in rows
        if not is_engine_replay_row(r) and not is_futures_shadow_row(r)
    ]
    replay_closed = [r for r in replay_rows if _is_closed(r)]
    replay_open = [r for r in replay_rows if not _is_closed(r)]
    session_closed = [r for r in session_rows if _is_closed(r)]
    session_open = [
        r for r in session_rows
        if str(r.get("status") or "").upper() in {"OPEN", "PENDING", ""}
        and not _is_closed(r)
    ]
    session_other = [
        r for r in session_rows
        if not _is_closed(r)
        and str(r.get("status") or "").upper() not in {"OPEN", "PENDING", ""}
    ]

    headline, hygiene = hygiene_closed_rows(closed)
    replay_headline, _ = hygiene_closed_rows(replay_closed)
    shadow_n = int((hygiene.get("futures_shadow") or {}).get("rows") or 0)

    # ID uniqueness
    ids = [str(r.get("id") or "") for r in rows if r.get("id")]
    id_counts: Dict[str, int] = defaultdict(int)
    for trade_id in ids:
        id_counts[trade_id] += 1
    dup_ids = sorted(trade_id for trade_id, count in id_counts.items() if count > 1)

    # Prefix and provenance are independent compatibility signals. A simulated id
    # explicitly marked LIVE/RECOVERY is ambiguous and must be repaired rather than
    # silently assigned to either accounting scope.
    replay_provenance_conflicts = []
    for row in rows:
        provenance = str(row.get("data_provenance") or "").upper().strip()
        if is_simulated_history_row(row) and provenance in {"LIVE", "RECOVERY"}:
            replay_provenance_conflicts.append({
                "id": row.get("id"),
                "data_provenance": provenance,
            })

    # Stop/target level mismatches on closed exits. Broker fills are execution facts,
    # not clamps: without the trigger-tick event a fill alone cannot prove whether the
    # level fired, so report those as unverifiable instead of manufacturing a failure.
    clamp_mismatches = []
    live_level_exits_unverifiable = []
    for row in closed:
        reason = _canonical_exit_reason(row)
        level_kind, expected = _exit_level(row)
        if not level_kind:
            continue
        exit_px = _price(row, "exit_price", "current_price", "price")
        if expected <= 0 or exit_px <= 0:
            continue

        provenance = str(row.get("data_provenance") or "").upper().strip()
        if provenance in {"LIVE", "RECOVERY"}:
            live_level_exits_unverifiable.append({
                "id": row.get("id"),
                "instrument": row.get("instrument"),
                "reason": reason,
                "exit_price": exit_px,
                "recorded_level": expected,
            })
            continue

        # CE and PE are both bought contracts in this system. SHORT describes the
        # underlying thesis for a PE, not a short option position.
        direction = (
            "LONG"
            if str(row.get("inst_type") or "").upper() == "OPT"
            else str(row.get("direction") or "LONG").upper()
        )
        crossed_as_expected = False
        if level_kind == "TARGET":
            crossed_as_expected = (
                exit_px >= expected - 0.05
                if direction == "LONG"
                else exit_px <= expected + 0.05
            )
        elif level_kind == "STOP":
            crossed_as_expected = (
                exit_px <= expected + 0.05
                if direction == "LONG"
                else exit_px >= expected - 0.05
            )
        if not crossed_as_expected:
            clamp_mismatches.append(
                {
                    "id": row.get("id"),
                    "instrument": row.get("instrument"),
                    "timeframe": row.get("timeframe"),
                    "reason": reason,
                    "level_kind": level_kind,
                    "exit_price": exit_px,
                    "expected": expected,
                }
            )

    by_instrument: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"closed": 0, "open": 0, "pnl": 0.0, "shadow": 0}
    )
    for row in session_closed:
        inst = str(row.get("instrument") or "--").split()[0]
        by_instrument[inst]["closed"] += 1
        by_instrument[inst]["pnl"] += accounting_pnl(row)
    for row in session_open:
        inst = str(row.get("instrument") or "--").split()[0]
        by_instrument[inst]["open"] += 1
    for row in shadow_rows:
        if is_futures_shadow_row(row):
            inst = str(row.get("instrument") or "--").split()[0]
            by_instrument[inst]["shadow"] += 1

    headline_pnl = sum(accounting_pnl(r) for r in headline)
    raw_closed_pnl = sum(accounting_pnl(r) for r in closed)
    session_pnl = sum(accounting_pnl(r) for r in session_closed)
    replay_raw_pnl = sum(accounting_pnl(r) for r in replay_closed)
    replay_headline_pnl = sum(accounting_pnl(r) for r in replay_headline)
    shadow_accounting_pnl = sum(accounting_pnl(r) for r in shadow_rows if _is_closed(r))
    replay_variant_pnl = replay_raw_pnl - replay_headline_pnl
    accounting_reconciled = abs(
        raw_closed_pnl
        - headline_pnl
        - shadow_accounting_pnl
        - replay_variant_pnl
    ) < 0.01

    failures = []
    if dup_ids:
        failures.append("duplicate_trade_ids")
    if clamp_mismatches:
        failures.append("deterministic_exit_level_mismatches")
    if replay_open:
        failures.append("open_engine_replay_rows")
    if replay_provenance_conflicts:
        failures.append("replay_provenance_conflicts")
    if not accounting_reconciled:
        failures.append("accounting_hygiene_does_not_reconcile")

    return {
        "certification_scope": "session_ledger",
        "certification_failures": failures,
        "rows_total": len(rows),
        "open": len(session_open),
        "closed_raw": len(closed),
        "closed_headline": len(headline),
        "shadow_rows": shadow_n,
        "duplicate_ids": dup_ids[:50],
        "duplicate_id_count": len(dup_ids),
        "headline_pnl": round(headline_pnl, 2),
        "raw_closed_pnl": round(raw_closed_pnl, 2),
        # Compatibility aggregate: raw storage includes both replay variants and
        # intentionally segregated FUT_SHADOW rows.
        "pnl_inflation_if_raw": round(raw_closed_pnl - headline_pnl, 2),
        "stop_target_clamp_mismatches": len(clamp_mismatches),
        "clamp_mismatch_rows": clamp_mismatches[:20],
        "live_level_exits_unverifiable": len(live_level_exits_unverifiable),
        "live_level_exit_rows": live_level_exits_unverifiable[:20],
        "replay_provenance_conflicts": replay_provenance_conflicts[:20],
        "accounting_hygiene_reconciled": accounting_reconciled,
        "session_ledger": {
            "rows": len(session_rows),
            "open": len(session_open),
            "closed": len(session_closed),
            "other_status": len(session_other),
            "pnl": round(session_pnl, 2),
        },
        "historical_replay": {
            "rows": len(replay_rows),
            "open": len(replay_open),
            "closed_raw": len(replay_closed),
            "closed_distinct": len(replay_headline),
            "stream_variant_rows": len(replay_closed) - len(replay_headline),
            "raw_pnl": round(replay_raw_pnl, 2),
            "distinct_outcome_pnl": round(replay_headline_pnl, 2),
            "raw_pnl_difference": round(replay_variant_pnl, 2),
            "certification_blocking": bool(
                replay_open or replay_provenance_conflicts
            ),
        },
        # The legacy flat P&L fields above describe this combined storage view.
        # Keep the label explicit for new callers so they do not mistake replay
        # experiments for certified session results.
        "combined_storage_accounting": {
            "closed_raw": len(closed),
            "closed_headline": len(headline),
            "raw_pnl": round(raw_closed_pnl, 2),
            "headline_pnl": round(headline_pnl, 2),
            "excluded_pnl": round(raw_closed_pnl - headline_pnl, 2),
        },
        "hygiene": hygiene,
        "by_instrument": {
            k: {**v, "pnl": round(v["pnl"], 2)}
            for k, v in sorted(by_instrument.items())
        },
        "ok": not failures,
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
