#!/usr/bin/env python3
"""Audit historical backtest windows via the live dashboard API.

Modern contract (2026-07 audit repair):
  - Authenticates with X-Dashboard-Token from /api/client_config
  - WebSocket max_size raised so large historical full_update frames work
  - Uses shadow-free summary fields when present (futures_shadow / engine_replay)
  - Works with default dashboard port from settings (override --base-url)

Usage:
  python scripts/audit_backtest_windows.py --days 1 5 --timeout 600
  python scripts/audit_backtest_windows.py --base-url http://127.0.0.1:8001
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

try:
    import websockets
except ImportError as exc:  # pragma: no cover
    raise SystemExit("websockets package required: pip install websockets") from exc


DEFAULT_BASE = "http://127.0.0.1:8001"
# Stay above default 1 MiB so first full_update can land; server also caps.
WS_MAX_SIZE = 4 * 1024 * 1024


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "utbot-audit-backtest-windows/2.0"})
    return s


def fetch_client_config(base: str, session: requests.Session) -> Dict[str, Any]:
    r = session.get(f"{base.rstrip('/')}/api/client_config", timeout=15)
    r.raise_for_status()
    return r.json()


def auth_headers(token: str) -> Dict[str, str]:
    return {"X-Dashboard-Token": token} if token else {}


def get_json(base: str, path: str, session: requests.Session, token: str) -> Dict[str, Any]:
    r = session.get(
        f"{base.rstrip('/')}{path}",
        headers=auth_headers(token),
        timeout=30,
    )
    if r.status_code == 401:
        raise RuntimeError(
            f"Unauthorized on {path} — need dashboard token. "
            "Ensure /api/client_config returns dashboard_token (loopback)."
        )
    r.raise_for_status()
    return r.json()


async def configure_days(base: str, token: str, days: int) -> None:
    token_q = quote(str(token or ""), safe="")
    host = base.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    uri = f"{host}/ws?token={token_q}"
    origin = base.rstrip("/")
    async with websockets.connect(
        uri,
        origin=origin,
        max_size=WS_MAX_SIZE,
        open_timeout=20,
    ) as websocket:
        # Drain optional full_update
        try:
            await asyncio.wait_for(websocket.recv(), timeout=5)
        except Exception:
            pass
        await websocket.send(json.dumps({"cmd": "configure", "backtest_days": int(days)}))
        deadline = time.time() + 45
        while time.time() < deadline:
            raw = await asyncio.wait_for(websocket.recv(), timeout=15)
            message = json.loads(raw)
            if message.get("type") == "config_ack":
                data = message.get("data") or {}
                if data.get("status") not in (None, "ok"):
                    raise RuntimeError(f"Configuration rejected for {days} days: {message}")
                return
        raise TimeoutError(f"No config_ack for backtest_days={days}")


def _summarize_closed(closed: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_type: Dict[str, Dict[str, Any]] = {}
    for row in closed:
        inst_type = str(row.get("inst_type") or "UNKNOWN").upper()
        bucket = by_type.setdefault(inst_type, {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0})
        pnl = float(row.get("pnl") or 0.0)
        bucket["count"] += 1
        bucket["pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1
    for value in by_type.values():
        n = int(value["count"] or 0)
        value["pnl"] = round(float(value["pnl"]), 2)
        value["win_rate"] = round(value["wins"] / n * 100.0, 2) if n else 0.0
    return by_type


def collect_result(
    base: str,
    session: requests.Session,
    token: str,
    days: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    deadline = time.time() + timeout_seconds
    stable_key = None
    stable_count = 0
    last_seen: Dict[str, Any] = {}

    while time.time() < deadline:
        state = get_json(base, "/api/state", session, token)
        diagnostics = state.get("diagnostics") or {}
        simulation = diagnostics.get("simulation") or state.get("simulation") or {}
        trades = get_json(base, "/api/trades?limit=2000&offset=0", session, token)
        closed = list(trades.get("closed") or [])
        summary = dict(trades.get("summary") or {})

        last_seen = {
            "days": days,
            "simulation_id": simulation.get("id") or state.get("simulation_id"),
            "diagnostic_days": simulation.get("backtest_days") or state.get("backtest_days"),
            "is_calculating": bool(state.get("is_calculating")),
            "scan_count": state.get("scan_count"),
            "closed_rows": len(closed),
            "closed_total": trades.get("closed_total") or len(closed),
            "summary": {
                "daily_pnl": summary.get("daily_pnl"),
                "fut_pnl": summary.get("fut_pnl"),
                "opt_pnl": summary.get("opt_pnl"),
                "total_trades": summary.get("total_trades"),
                "win_rate": summary.get("win_rate"),
                "profit_factor": summary.get("profit_factor"),
                "max_drawdown": summary.get("max_drawdown"),
                "futures_shadow": summary.get("futures_shadow"),
                "engine_replay": summary.get("engine_replay"),
            },
            "row_pnl": round(sum(float(row.get("pnl") or 0.0) for row in closed), 2),
            "by_type": _summarize_closed(closed),
            "rejects": diagnostics.get("rejects") or {},
            "scanner_freshness": diagnostics.get("scanner_freshness") or state.get("scanner_freshness"),
            "sources": diagnostics.get("sources") or {},
        }

        key = (
            last_seen["simulation_id"],
            last_seen["diagnostic_days"],
            last_seen["closed_rows"],
            round(float(summary.get("daily_pnl") or 0.0), 2),
        )
        ready = (
            int(last_seen.get("diagnostic_days") or 0) == int(days)
            and int(state.get("scan_count") or 0) >= 2
            and not bool(state.get("is_calculating"))
        )
        # Also accept stable calculating=false via recalculation idle
        recalc = (diagnostics.get("recalculation") or {}).get("status") or ""
        if recalc and str(recalc).lower() not in ("", "idle", "complete", "completed", "done"):
            ready = False

        if ready and key == stable_key:
            stable_count += 1
        elif ready:
            stable_key = key
            stable_count = 1
        else:
            stable_key = None
            stable_count = 0

        if stable_count >= 3:
            return last_seen
        time.sleep(2)

    raise TimeoutError(f"Backtest window {days} did not stabilize: {json.dumps(last_seen)[:500]}")


async def run_audit(args: argparse.Namespace) -> List[Dict[str, Any]]:
    base = str(args.base_url or DEFAULT_BASE).rstrip("/")
    session = _session()
    cfg = fetch_client_config(base, session)
    token = str(cfg.get("dashboard_token") or "")
    if not token:
        print(
            "WARNING: empty dashboard_token from /api/client_config "
            "(non-loopback?). Protected APIs may 401.",
            file=sys.stderr,
        )

    status = get_json(base, "/api/status", session, token)
    original_days = int(
        ((status.get("config") or {}).get("backtest_days"))
        or status.get("backtest_days")
        or 0
    )
    if original_days <= 0:
        state = get_json(base, "/api/state", session, token)
        original_days = int(
            ((state.get("config") or {}).get("backtest_days"))
            or state.get("backtest_days")
            or 0
        )
    results: List[Dict[str, Any]] = []
    last_requested: Optional[int] = None
    try:
        for days in args.days:
            last_requested = int(days)
            print(f"--- configure backtest_days={last_requested} ---", flush=True)
            await configure_days(base, token, last_requested)
            result = collect_result(base, session, token, last_requested, int(args.timeout))
            results.append(result)
            print(json.dumps(result, indent=2), flush=True)
        return results
    finally:
        if original_days > 0 and last_requested != original_days:
            print(f"--- restore backtest_days={original_days} ---", flush=True)
            await configure_days(base, token, original_days)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit historical backtest windows via dashboard API")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help="Dashboard base URL")
    parser.add_argument("--days", nargs="+", type=int, default=[1, 5, 14])
    parser.add_argument("--timeout", type=int, default=600, help="Seconds per window")
    parser.add_argument(
        "--output",
        default="",
        help="Optional JSON report path (default reports/backtest_window_audit_<ts>.json)",
    )
    args = parser.parse_args()

    results = asyncio.run(run_audit(args))
    out = args.output
    if not out:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out = f"reports/backtest_window_audit_{ts}.json"
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Wrote {path}", flush=True)


if __name__ == "__main__":
    main()
