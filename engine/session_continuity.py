"""Persist gaps in advisory monitoring without reconstructing nonexistent fills."""

from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timedelta
from functools import wraps
import threading
from typing import Any


def session_locked(method):
    """Share the existing reentrant ledger lock with the independent monitor."""
    @wraps(method)
    def locked(host, *args, **kwargs):
        guard = getattr(host, "_session_breach_lock", None)
        if guard is None:
            guard = host._session_breach_lock = threading.RLock()
        with guard:
            return method(host, *args, **kwargs)
    return locked


def _clock(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value)) if not isinstance(value, datetime) else value
        if stamp.tzinfo is not None:
            from zoneinfo import ZoneInfo
            stamp = stamp.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
        return stamp
    except (TypeError, ValueError):
        return None


def snapshot_continuity(candidate: Any) -> dict:
    result = {}
    for name in ("monitoring_continuity", "exit_observation", "accepted_decision_record"):
        value = getattr(candidate, name, None)
        if isinstance(value, dict):
            try:
                result[name] = json.loads(json.dumps(value, allow_nan=False))
            except (TypeError, ValueError):
                continue
    for name in ("exit_price_basis", "exit_quote_source", "exit_quote_verified",
                 "exit_observed_at", "analysis_observed_at", "entry_quote_observed_at"):
        value = getattr(candidate, name, None)
        if value is not None:
            result[name] = value.isoformat() if isinstance(value, datetime) else value
    return result


def restore_continuity(candidate: Any, payload: dict) -> None:
    for name in ("monitoring_continuity", "exit_observation", "accepted_decision_record"):
        value = payload.get(name)
        if isinstance(value, dict):
            try:
                setattr(candidate, name, json.loads(json.dumps(value, allow_nan=False)))
            except (TypeError, ValueError):
                continue
    for name in ("exit_price_basis", "exit_quote_source"):
        if isinstance(payload.get(name), str):
            setattr(candidate, name, payload[name])
    if isinstance(payload.get("exit_quote_verified"), bool):
        candidate.exit_quote_verified = payload["exit_quote_verified"]
    for name in ("exit_observed_at", "analysis_observed_at", "entry_quote_observed_at"):
        stamp = _clock(payload.get(name))
        if stamp is not None:
            setattr(candidate, name, stamp)


def _assess_history(history, verified: bool, start: datetime, end: datetime,
                    stop: float, direction: str) -> dict:
    result = {"history_coverage": "UNVERIFIED" if not verified else "INCOMPLETE",
              "possible_stop_breach": None, "first_possible_stop_breach_at": ""}
    if history is None or not verified or stop <= 0:
        return result
    # A partial first candle may have touched the stop before monitoring ceased.
    first = start.replace(second=0, microsecond=0)
    if first < start:
        first += timedelta(minutes=1)
    last = end.replace(second=0, microsecond=0) - timedelta(minutes=1)
    expected = set()
    cursor = first
    while cursor <= last and len(expected) < 1440:
        expected.add(cursor)
        cursor += timedelta(minutes=1)
    seen, breached = set(), []
    try:
        for stamp, row in history.iterrows():
            clock = _clock(stamp)
            if clock not in expected or clock.date() != end.date():
                continue
            low, high = float(row["low"]), float(row["high"])
            if not (math.isfinite(low) and math.isfinite(high) and 0 < low <= high):
                continue
            seen.add(clock)
            if (direction == "LONG" and low <= stop) or (direction == "SHORT" and high >= stop):
                breached.append(clock)
    except (AttributeError, KeyError, TypeError, ValueError):
        return result
    if expected and expected == seen:
        entire_minutes = (start.second == end.second == 0 and start.microsecond == end.microsecond == 0)
        result["history_coverage"] = ("COMPLETE_CLOSED_INDEX_BARS" if entire_minutes
                                      else "COMPLETE_INTERIOR_CLOSED_INDEX_BARS")
        if entire_minutes:
            result["possible_stop_breach"] = False
    if breached:
        result["possible_stop_breach"] = True
        result["first_possible_stop_breach_at"] = min(breached).isoformat()
    return result


def observe_continuity(candidate: Any, *, now: datetime, quote_usable: bool,
                       history=None, history_verified: bool = False,
                       gap_seconds: float = 30.0) -> tuple[dict, bool, bool]:
    """Return state, persistence need, and one warning for a new monitoring gap.

    This is advisory evidence. It never changes prices, native protection, orders,
    or lifecycle status. Fresh quotes after recovery cannot erase a missed interval.
    """
    now = _clock(now)
    state = copy.deepcopy(getattr(candidate, "monitoring_continuity", {}) or {})
    last = _clock(state.get("last_check_at"))
    entry = _clock(getattr(candidate, "entry_timestamp", None))
    threshold = max(1.0, float(gap_seconds))
    alert = False
    unknown_start = last is None and entry is not None and (now - entry).total_seconds() > threshold
    gap = last is not None and 0 < (now - last).total_seconds() > threshold
    if gap or unknown_start:
        start = last or entry
        try:
            stop = float(state.get("observed_underlying_stop") or
                         getattr(candidate, "underlying_stop", 0.0) or
                         getattr(candidate, "spot_stop", 0.0) or 0.0)
        except (TypeError, ValueError):
            stop = 0.0
        if not math.isfinite(stop):
            stop = 0.0
        evidence = (_assess_history(history, history_verified, start, now, stop,
                                   str(getattr(candidate, "direction", "")).upper())
                    if not unknown_start else {"history_coverage": "UNKNOWN_START",
                                              "possible_stop_breach": None,
                                              "first_possible_stop_breach_at": ""})
        evidence.update({"from": start.isoformat(), "to": now.isoformat(),
                         "seconds": round((now - start).total_seconds(), 3),
                         "stop_at_gap_start": stop, "historical_fill_inferred": False,
                         "gap_kind": "MONITOR_UNAVAILABLE"})
        state["gaps"] = (list(state.get("gaps", []) or []) + [evidence])[-8:]
        state.update(evidence)
        state["status"] = "RECOVERY_REQUIRED"
        alert = True
    quote_start = _clock(state.get("quote_unusable_since"))
    quote_recovered = bool(quote_usable and quote_start is not None and state.get("quote_gap_open"))
    if not quote_usable and quote_start is None:
        quote_start = _clock(state.get("last_quote_check_at")) or now
        state["quote_unusable_since"] = quote_start.isoformat()
        state["quote_gap_stop"] = state.get("observed_underlying_stop", 0.0)
    if quote_start is not None and (now - quote_start).total_seconds() > threshold:
        try:
            quote_stop = float(state.get("quote_gap_stop") or state.get("observed_underlying_stop") or
                               getattr(candidate, "underlying_stop", 0.0) or 0.0)
        except (TypeError, ValueError):
            quote_stop = 0.0
        if not math.isfinite(quote_stop):
            quote_stop = 0.0
        evidence = _assess_history(history, history_verified, quote_start, now, quote_stop,
                                   str(getattr(candidate, "direction", "")).upper())
        evidence.update({"from": quote_start.isoformat(), "to": now.isoformat(),
                         "seconds": round((now - quote_start).total_seconds(), 3),
                         "stop_at_gap_start": quote_stop, "historical_fill_inferred": False,
                         "gap_kind": "QUOTE_UNAVAILABLE"})
        gaps = list(state.get("gaps", []) or [])
        if state.get("quote_gap_open"):
            for index in range(len(gaps) - 1, -1, -1):
                if gaps[index].get("gap_kind") == "QUOTE_UNAVAILABLE":
                    gaps[index] = evidence
                    break
        else:
            gaps.append(evidence)
            alert = True
        state["gaps"] = gaps[-8:]
        state.update(evidence)
        state.update(status="RECOVERY_REQUIRED", quote_gap_open=not quote_usable,
                     quote_gap_stop=quote_stop)
    if quote_usable:
        state.pop("quote_unusable_since", None)
        state["quote_gap_open"] = False
    state.setdefault("status", "CONNECTED")
    state.update({"schema_version": "session_monitor.v1", "last_check_at": now.isoformat(),
                  "current_quote_usable": bool(quote_usable), "historical_fill_inferred": False})
    if quote_usable:
        state["last_quote_check_at"] = now.isoformat()
    try:
        observed_stop = float(getattr(candidate, "underlying_stop", 0.0) or
                              getattr(candidate, "spot_stop", 0.0) or 0.0)
        if quote_usable and math.isfinite(observed_stop):
            state["observed_underlying_stop"] = observed_stop
    except (TypeError, ValueError):
        pass
    persisted = _clock(state.get("last_persisted_check_at"))
    changed = alert or quote_recovered or persisted is None or (now - persisted).total_seconds() >= 10.0
    if changed:
        state["last_persisted_check_at"] = now.isoformat()
    candidate.monitoring_continuity = state
    return state, changed, alert
