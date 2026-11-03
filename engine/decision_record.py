"""Canonical, serializable record for one scanner admission decision."""

from __future__ import annotations

import hashlib
import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from enum import Enum
from typing import Any, Iterable, Mapping


class DecisionStatus(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    NOT_APPLICABLE = "NOT_APPLICABLE"


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def candidate_summary(candidate: Any) -> dict[str, Any]:
    """Return stable fields needed to explain a competing candidate."""
    return {
        "instrument": str(getattr(candidate, "instrument", "") or ""),
        "timeframe": str(getattr(candidate, "timeframe", "") or ""),
        "direction": str(getattr(candidate, "direction", "") or ""),
        "grade": str(getattr(candidate, "grade", "") or ""),
        "confidence": round(float(getattr(candidate, "confidence", 0.0) or 0.0), 6),
        "signal_timestamp": _iso(getattr(candidate, "signal_timestamp", None)),
    }


def policy_owner(candidate: Any) -> tuple[str, str]:
    """Return authoritative policy owner and generic-quality applicability."""
    lane = str(getattr(candidate, "source_lane", "") or "").strip().upper()
    engine = str(getattr(candidate, "source_engine", "") or "").strip().upper()
    if lane in {"UTS", "UT_SMART", "UT SMART", "UT_SMART_EXIT"} or "UT_SMART" in engine:
        return "UT_SMART_LANE", DecisionStatus.NOT_APPLICABLE.value
    if lane == "S1" or engine == "S1":
        return "S1_LANE", DecisionStatus.NOT_APPLICABLE.value
    verdict = getattr(candidate, "quality_verdict", {}) or {}
    if isinstance(verdict, Mapping) and verdict:
        allowed = bool(verdict.get("allowed", False))
        return "QUALITY_POLICY", (
            DecisionStatus.ALLOW.value if allowed else DecisionStatus.BLOCK.value
        )
    return "SCANNER_ENTRY_COORDINATOR", DecisionStatus.NOT_APPLICABLE.value


def decision_id(candidate: Any, owner: str) -> str:
    identity = "|".join(
        (
            str(getattr(candidate, "instrument", "") or ""),
            str(getattr(candidate, "timeframe", "") or ""),
            str(getattr(candidate, "direction", "") or ""),
            _iso(getattr(candidate, "signal_timestamp", None)),
            str(owner or ""),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def data_age_seconds(candidate: Any, decided_at: datetime) -> float | None:
    observed = getattr(candidate, "signal_timestamp", None)
    if observed is None:
        return None
    try:
        market_tz = ZoneInfo("Asia/Kolkata")
        observed = (observed.replace(tzinfo=market_tz) if observed.tzinfo is None
                    else observed.astimezone(market_tz))
        decided_at = (decided_at.replace(tzinfo=market_tz) if decided_at.tzinfo is None
                      else decided_at.astimezone(market_tz))
        return round(max(0.0, float((decided_at - observed).total_seconds())), 3)
    except Exception:
        return None


def decision_clocks(candidate: Any, decided_at: datetime) -> dict[str, Any]:
    """Separate source-bar identity, availability, observation and acceptance."""
    source = getattr(candidate, "source_signal_timestamp", None) or getattr(candidate, "signal_timestamp", None)
    available = getattr(candidate, "signal_available_at", None)
    confirmed = (getattr(candidate, "entered_intracandle", None) is False or
                 str(getattr(candidate, "signal_fire_mode", "")).upper() == "CANDLE_CLOSE")
    if available is None and confirmed and isinstance(source, datetime):
        minutes = {"1min": 1, "5min": 5, "15min": 15, "60min": 60}.get(str(getattr(candidate, "timeframe", "")))
        if minutes is not None:
            available = source + timedelta(minutes=minutes)
    accepted = (getattr(candidate, "decision_record", {}) or {}).get("accepted_at", "")
    delay = None
    if confirmed and isinstance(available, datetime):
        try:
            clock = datetime.fromisoformat(accepted) if accepted else decided_at
            market_tz = ZoneInfo("Asia/Kolkata")
            comparable_available = (available.replace(tzinfo=market_tz) if available.tzinfo is None
                                    else available.astimezone(market_tz))
            comparable_clock = (clock.replace(tzinfo=market_tz) if clock.tzinfo is None
                                else clock.astimezone(market_tz))
            delay = round(max(0.0, (comparable_clock - comparable_available).total_seconds()), 3)
        except (TypeError, ValueError):
            pass
    return {"source_signal_timestamp": _iso(source), "signal_available_at": _iso(available),
            "post_close_delay_seconds": delay,
            "analysis_observed_at": _iso(getattr(candidate, "analysis_observed_at", None)),
            "entry_quote_observed_at": _iso(getattr(candidate, "entry_quote_observed_at", None)),
            "accepted_at": accepted}


def build_decision_record(
    candidate: Any,
    *,
    rank: int,
    decided_at: datetime,
    alternatives: Iterable[Any] = (),
) -> dict[str, Any]:
    owner, quality_status = policy_owner(candidate)
    return {
        "schema_version": "scanner_decision.v1",
        "decision_id": decision_id(candidate, owner),
        "policy_owner": owner,
        "status": DecisionStatus.NOT_APPLICABLE.value,
        "quality_policy_status": quality_status,
        "rank": max(1, int(rank or 1)),
        "rejected_alternatives": [],
        "competing_candidates": [candidate_summary(item) for item in alternatives],
        "data_age_seconds": data_age_seconds(candidate, decided_at),
        "reason": "Awaiting execution gates",
        "source": "global_candidate_ranking",
        "input_timestamp": _iso(getattr(candidate, "signal_timestamp", None)),
        "decided_at": _iso(decided_at),
        **decision_clocks(candidate, decided_at),
    }


def ensure_decision_record(
    candidate: Any,
    *,
    rank: int = 1,
    decided_at: datetime | None = None,
    alternatives: Iterable[Any] = (),
) -> dict[str, Any]:
    """Guarantee one canonical record without replacing an existing decision."""
    current = dict(getattr(candidate, "decision_record", {}) or {})
    required = {
        "schema_version",
        "decision_id",
        "policy_owner",
        "status",
        "rank",
        "reason",
        "source",
        "decided_at",
    }
    if required.issubset(current):
        for key, value in decision_clocks(candidate, decided_at or datetime.now().astimezone()).items():
            if key not in current:
                current[key] = value
        setattr(candidate, "decision_record", current)
        return current
    record = build_decision_record(
        candidate,
        rank=rank,
        decided_at=decided_at or datetime.now().astimezone(),
        alternatives=alternatives,
    )
    record.update({key: value for key, value in current.items() if value not in (None, "")})
    setattr(candidate, "decision_record", record)
    return record


def update_decision_record(
    candidate: Any,
    *,
    status: str,
    reason: str,
    source: str,
    decided_at: datetime,
) -> dict[str, Any]:
    record = dict(getattr(candidate, "decision_record", {}) or {})
    owner, quality_status = policy_owner(candidate)
    if not record:
        record = build_decision_record(candidate, rank=1, decided_at=decided_at)
    normalized = str(status or "").strip().upper()
    if normalized in {"ACCEPTED", "ALLOW", "ALLOWED"}:
        normalized = DecisionStatus.ALLOW.value
    elif normalized in {"REJECTED", "BLOCK", "BLOCKED", "REFUSED"}:
        normalized = DecisionStatus.BLOCK.value
    elif normalized not in {item.value for item in DecisionStatus}:
        normalized = DecisionStatus.NOT_APPLICABLE.value
    record.update(
        {
            "policy_owner": owner,
            "quality_policy_status": quality_status,
            "status": normalized,
            "reason": str(reason or record.get("reason") or ""),
            "source": str(source or record.get("source") or "scanner"),
            "decided_at": _iso(decided_at),
            "data_age_seconds": data_age_seconds(candidate, decided_at),
        }
    )
    if str(status or "").strip().upper() == "ACCEPTED":
        record.setdefault("accepted_at", "")
        if not record["accepted_at"]:
            record["accepted_at"] = _iso(decided_at)
    setattr(candidate, "decision_record", record)
    record.update(decision_clocks(candidate, decided_at))
    if str(status or "").strip().upper() == "ACCEPTED" and not getattr(candidate, "accepted_decision_record", None):
        setattr(candidate, "accepted_decision_record", copy.deepcopy(record))
    return record
