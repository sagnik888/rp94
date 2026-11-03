"""Why a position was closed, in one persisted record (guard audit 2026-10-06, step 4).

Exit reasons used to be a single label. Whether a HOLD had suppressed an earlier
proposal, which policy owned the position, what threshold the lock was standing on and
how old the trend evidence was could only be guessed from logs. build() collects what
the exit pass already computed -- it performs no new reads and no network calls -- and
the record is persisted with the closed trade (Trade.exit_decision_record) or the
session EXIT row (s1_lane.SESSION_STATE_FIELDS).

Status vocabulary for ``guards`` entries follows the audit: PROPOSED, CLAMPED, HIT,
ARMED_NOT_HIT, SUPPRESSED_BY_HOLD, CANCELLED, ERROR_NOT_APPLIED.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

SCHEMA = "exit_decision.v1"
_POLICY_BY_OWNER = {
    "S1": "s1_native",
    "UTS": "uts_native",
    "D1": "s12_d1_native",
    "D2": "s12_d2_native",
    "GENERIC": "generic_exit_stack",
}


def _iso(value) -> str:
    try:
        return value.isoformat()
    except Exception:
        return str(value or "")


def _number(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def build(
    trade_like: Any,
    *,
    reason: str,
    mark: float,
    decided_at,
    path: str,
    detail: str = "",
) -> Dict[str, Any]:
    """The record for one exit. Never raises: diagnostics must not block an exit."""
    try:
        from engine import s1_lane

        owner = s1_lane.frozen_exit_owner(trade_like) or ""
    except Exception:
        owner = ""
    record: Dict[str, Any] = {
        "schema": SCHEMA,
        "path": str(path or ""),
        "owner": owner or "LEGACY_UNSTAMPED",
        "policy": _POLICY_BY_OWNER.get(owner, "unknown"),
        "controlling_reason": str(reason or ""),
        "detail": str(detail or "")[:500],
        "mark": _number(mark),
        "decided_at": _iso(decided_at),
        "peak_pnl": _number(getattr(trade_like, "peak_pnl", None)),
        "peak_pnl_basis": str(getattr(trade_like, "peak_pnl_basis", "") or "UNSPECIFIED"),
        "max_drawdown": _number(getattr(trade_like, "max_drawdown", None)),
        "max_drawdown_basis": str(getattr(trade_like, "max_drawdown_basis", "") or "UNSPECIFIED"),
    }
    try:
        trace = list(getattr(trade_like, "_profit_lock_trace", None) or [])
    except Exception:
        trace = []
    if trace:
        record["profit_lock"] = [dict(item) for item in trace][-8:]
        record["suppressed_by_hold"] = [
            dict(item) for item in trace if item.get("state") == "SUPPRESSED_BY_HOLD"
        ]
        stamp = str(trace[-1].get("time") or "")
        record["profit_lock_evaluated_at"] = stamp
        record["profit_lock_current"] = bool(stamp) and stamp == record["decided_at"]
    trend = getattr(trade_like, "s1_trend_evidence", None)
    if trend:
        record["trend_evidence"] = str(trend)
    defer = getattr(trade_like, "_last_trend_profit_lock_defer_reason", None)
    if defer:
        record["last_trend_deferral_verdict"] = str(defer)[:300]
    return record
