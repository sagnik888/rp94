"""One HOLD contract for the generic profit lock, shared by every execution path.

The manual session ledger (scanner._check_session_candidates_breaches_locked), the
broker exit cycle (engine/exit_cycle.py) and the historical replay probe
(SignalProcessor._historical_live_management_probe) each used to carry their own copy
of "ratchet the lock stop, maybe give it option breathing room, maybe defer it for a
trend, then let a retention backstop run". The copies drifted: the manual path kept a
deferred lock's tightened stop and let the backstop close the trade in the same pass,
and replay had no breathing room or trend deferral at all (guard audit 2026-10-06).

resolve() is the single decision. Each path keeps its own price-space conversion and
its own exit-price choice; what is shared is:

* option breathing room applied to the proposed stop (premium space),
* whether the lock floor was crossed,
* the trend deferral, which restores the stop the pass started from, and
* the two hold flags the later retention backstop must honour
  (Scanner._smart_profit_retention_eligible).

An already-committed stop is never loosened: only this pass's proposal is undone.
Every step is also recorded in ``trace`` so an exit can say which rule controlled it
and which proposals a HOLD suppressed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

DEFERRABLE_LOCK_REASONS = frozenset({"SMART_PROFIT_LOCK", "MAJOR_WIN_GUARD"})
POLICY_VERSION = "profit_lock_hold.v1"


@dataclass
class ProfitLockHold:
    lock_reason: str
    lock_stop: float
    effective_lock_stop: float
    stop: float
    hit: bool
    breathing_clamped: bool = False
    breathing_floor: float = 0.0
    breathing_stop: float = 0.0     # the clamped stop, kept even if a deferral restores
    trend_deferred: bool = False
    defer_reason: str = ""
    restored: bool = False
    trace: List[Dict[str, Any]] = field(default_factory=list)


def _iso(value) -> str:
    try:
        return value.isoformat()
    except Exception:
        return str(value or "")


def resolve(
    scanner,
    trade_like,
    *,
    lock_reason: str,
    lock_stop: float,
    proposed_stop: float,
    pre_lock_stop: float,
    mark: float,
    option_space: bool,
    current_pnl_rs: float,
    peak_pnl_rs: float,
    latest_time,
    regime: str = "",
    apply_breathing: bool = True,
    mark_verified: bool = True,
    non_option_hit: Any = False,
    non_option_defer_floor: float = 0.0,
    stop_viable: Optional[Callable[[float], bool]] = None,
    on_defer: Optional[Callable[[str, str], None]] = None,
) -> ProfitLockHold:
    """Resolve one pass of the profit lock for any execution path.

    ``proposed_stop`` is the path's stop after ratcheting in the lock (in premium space
    for an option, contract/underlying space otherwise); ``pre_lock_stop`` is the stop
    the pass started from, in the same space as ``stop`` is returned. Options: the lock
    is hit when a verified positive mark is at or below the (possibly clamped) lock
    floor. Non-options: ``non_option_hit`` is the caller's own stop test, either a bool
    or a callable evaluated on the resolved stop. ``stop_viable`` (optional) cancels the
    lock and keeps the pre-lock stop when the resolved stop would not bank a net gain.
    """
    lock_reason = str(lock_reason or "")
    lock_stop = float(lock_stop or 0.0)
    stop = float(proposed_stop or 0.0)
    effective = lock_stop
    result = ProfitLockHold(
        lock_reason=lock_reason, lock_stop=lock_stop, effective_lock_stop=effective,
        stop=stop, hit=False,
    )
    stamp = _iso(latest_time)
    if lock_reason or lock_stop > 0:
        result.trace.append({
            "guard": lock_reason or "PROFIT_LOCK", "state": "PROPOSED", "time": stamp,
            "threshold": lock_stop, "units": "premium" if option_space else "price",
            "policy": POLICY_VERSION,
        })

    if option_space and apply_breathing and stop > 0:
        try:
            adjusted, floor, clamped = scanner._apply_option_stop_breathing_room(
                trade_like,
                stop,
                latest_time=latest_time,
                regime=regime,
                profit_lock_reason=lock_reason,
            )
        except Exception as exc:
            # No breathing room is the protective answer: the tighter stop stands.
            adjusted, floor, clamped = stop, 0.0, False
            result.trace.append({
                "guard": "OPTION_BREATHING_ROOM", "state": "ERROR_NOT_APPLIED", "time": stamp,
                "detail": f"{type(exc).__name__}: {exc}",
            })
        if clamped:
            result.breathing_clamped = True
            result.breathing_floor = float(floor or 0.0)
            stop = float(adjusted)
            result.breathing_stop = stop
            effective = min(effective, stop) if effective > 0 else effective
            result.trace.append({
                "guard": "OPTION_BREATHING_ROOM", "state": "CLAMPED", "time": stamp,
                "threshold": stop, "floor": result.breathing_floor,
            })

    if stop_viable is not None and lock_stop > 0 and not stop_viable(stop):
        result.trace.append({
            "guard": lock_reason or "PROFIT_LOCK", "state": "CANCELLED", "time": stamp,
            "threshold": stop, "detail": "lock stop would not bank a net gain",
        })
        lock_reason = ""
        lock_stop = 0.0
        stop = float(pre_lock_stop or 0.0)

    if option_space:
        hit = bool(mark_verified) and effective > 0.0 and 0.0 < float(mark or 0.0) <= effective
    elif callable(non_option_hit):
        hit = bool(non_option_hit(stop))
    else:
        hit = bool(non_option_hit)
    hit = hit and bool(lock_reason)

    if hit and lock_reason in DEFERRABLE_LOCK_REASONS:
        try:
            defer, defer_reason = scanner._should_defer_trend_option_profit_lock(
                trade_like,
                float(mark or 0.0),
                effective if option_space else float(non_option_defer_floor or stop),
                float(current_pnl_rs or 0.0),
                float(peak_pnl_rs or 0.0),
                latest_time,
                regime=regime,
            )
        except Exception as exc:
            # A HOLD that cannot be evaluated does not hold: the crossed lock exits.
            defer, defer_reason = False, f"trend deferral unavailable ({type(exc).__name__}: {exc})"
            result.trace.append({
                "guard": "TREND_DEFERRAL", "state": "ERROR_NOT_APPLIED", "time": stamp,
                "detail": defer_reason,
            })
        if defer:
            result.trace.append({
                "guard": lock_reason, "state": "SUPPRESSED_BY_HOLD", "time": stamp,
                "threshold": effective, "mark": float(mark or 0.0),
                "hold": "TREND_DEFERRAL", "detail": str(defer_reason or ""),
            })
            if on_defer is not None:
                on_defer(lock_reason, str(defer_reason or ""))
            result.trend_deferred = True
            result.defer_reason = str(defer_reason or "")
            lock_reason = ""
            lock_stop = 0.0
            effective = 0.0
            stop = float(pre_lock_stop or 0.0)
            result.restored = True
            hit = False
        else:
            try:
                setattr(trade_like, "_last_trend_profit_lock_defer_reason", defer_reason)
            except Exception:
                pass

    if result.trace and lock_reason:
        result.trace.append({
            "guard": lock_reason, "state": "HIT" if hit else "ARMED_NOT_HIT", "time": stamp,
            "threshold": effective, "mark": float(mark or 0.0),
        })
    result.lock_reason = lock_reason
    result.lock_stop = lock_stop
    result.effective_lock_stop = effective
    result.stop = stop
    result.hit = hit
    try:
        setattr(trade_like, "_profit_lock_trace", list(result.trace))
    except Exception:
        pass
    return result
