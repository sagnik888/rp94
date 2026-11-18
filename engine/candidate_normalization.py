"""Candidate normalization helpers shared by scanner/dashboard paths."""

from __future__ import annotations

import math
from typing import Callable

from engine.market_regime import futures_stop_tolerance_pct
from engine.signal_processor import TradeCandidate
from engine.trade_row_adapter import frozen_manual_exit_net_pnl, live_signal_exit_mark, trade_row_event_seconds


def _normalize_native_utema(candidate, candidate_net_pnl):
    """Preserve native geometry and observed exits across persistence/restore.

    Normalization is accounting, not new admission authority. Missing or invalid
    evidence stays invalid; neither a current setting nor an ideal level may
    manufacture a replacement entry, protective stop, target, or exit fill.
    """
    try:
        levels = tuple(float(getattr(candidate, key, 0.) or 0.)
                       for key in ("price", "stop", "target"))
    except (ValueError, TypeError):
        return False
    if not all(math.isfinite(value) and value > 0 for value in levels):
        return False
    if str(getattr(candidate, "action", "ENTRY") or "ENTRY").upper() != "EXIT":
        return False
    reason = str(getattr(candidate, "exit_reason", "") or "").upper()
    if reason == "QUOTE_INVALID":
        changed = getattr(candidate, "pnl", 0.) != 0.
        candidate.pnl = 0.
        return changed
    observation = getattr(candidate, "exit_observation", {}) or {}
    if not isinstance(observation, dict):
        return False
    verified = getattr(candidate, "exit_quote_verified", observation.get("quote_verified"))
    if verified is not True:
        return False
    try:
        observed = float(getattr(candidate, "exit_price", 0.) or 0.)
    except (ValueError, TypeError):
        return False
    if not math.isfinite(observed) or observed <= 0:
        return False
    entered = trade_row_event_seconds({"entry_timestamp": getattr(candidate, "entry_timestamp", None)
                                      or getattr(candidate, "signal_timestamp", None)})
    exited = trade_row_event_seconds({"exit_timestamp": getattr(candidate, "exit_timestamp", None)}, prefer_exit=True)
    if not (math.isfinite(entered) and math.isfinite(exited) and 0 < entered <= exited):
        return False
    new_pnl = frozen_manual_exit_net_pnl(candidate)
    if new_pnl is None:
        new_pnl = candidate_net_pnl(candidate, observed)
    if not math.isfinite(float(new_pnl)):
        return False
    changed = (float(getattr(candidate, "current_price", 0.) or 0.) != observed
               or getattr(candidate, "pnl", 0.) != new_pnl)
    candidate.current_price, candidate.pnl = observed, new_pnl
    return changed


def normalize_loaded_exit_candidate(
    candidate: TradeCandidate,
    *,
    futures_sl_pct: float,
    options_sl_pct: float,
    candidate_exit_resolution: Callable[[TradeCandidate, float, str, str], tuple[float, str, str]],
    candidate_net_pnl: Callable[[TradeCandidate, float], float],
) -> bool:
    """Repair persisted live/session rows created by older builds before accounting them."""
    from engine.s1_lane import frozen_exit_owner

    if frozen_exit_owner(candidate) == "UTEMA":
        return _normalize_native_utema(candidate, candidate_net_pnl)
    changed = False
    entry = float(getattr(candidate, "price", 0.0) or 0.0)
    if entry <= 0:
        return False

    frozen_pnl = frozen_manual_exit_net_pnl(candidate)
    live_mark = (float(candidate.exit_price) if frozen_pnl is not None
                 else live_signal_exit_mark(candidate))
    if live_mark is not None:
        # Current manual rows already record the observed exit and entry geometry.
        # Replacing a late stop observation with the ideal stop hides the actual
        # loss; applying today's settings would also rewrite yesterday's geometry.
        # This does not reconstruct rows whose observations were already overwritten.
        reason = str(getattr(candidate, "exit_reason", "") or "").upper()
        new_pnl = frozen_pnl
        if reason == "QUOTE_INVALID":
            new_pnl = 0.0
        elif new_pnl is None:
            new_pnl = candidate_net_pnl(candidate, live_mark)
        if reason in {"SMART_PROFIT_LOCK", "MAJOR_WIN_GUARD"} and new_pnl <= 0:
            candidate.exit_reason = "LEGACY_LOCK_ERROR"
            changed = True
        changed = changed or (
            float(getattr(candidate, "current_price", 0.0) or 0.0) != live_mark
            or float(getattr(candidate, "pnl", 0.0) or 0.0) != new_pnl
        )
        candidate.current_price = live_mark
        candidate.pnl = new_pnl
        return changed

    inst_type = str(getattr(candidate, "inst_type", "") or "").upper()
    direction = str(getattr(candidate, "direction", "") or "").upper()
    rr = max(0.1, float(getattr(candidate, "rr", 1.5) or 1.5))
    old_stop = float(getattr(candidate, "stop", 0.0) or 0.0)
    old_target = float(getattr(candidate, "target", 0.0) or 0.0)
    runner_mode = bool(getattr(candidate, "runner_mode", False))

    if inst_type == "FUT":
        effective_pct = futures_stop_tolerance_pct(
            futures_sl_pct,
            getattr(candidate, "market_regime", "UNKNOWN"),
        )
        max_dist = entry * (effective_pct / 100.0)
        if direction == "LONG":
            repaired_stop = max(old_stop or (entry - max_dist), entry - max_dist)
            repaired_target = old_target if repaired_stop >= entry else entry + ((entry - repaired_stop) * rr)
        else:
            repaired_stop = min(old_stop or (entry + max_dist), entry + max_dist)
            repaired_target = old_target if repaired_stop <= entry else entry - ((repaired_stop - entry) * rr)
    elif inst_type == "OPT":
        max_dist = entry * (float(options_sl_pct or 15.0) / 100.0)
        repaired_stop = max(0.05, max(old_stop or (entry - max_dist), entry - max_dist))
        repaired_target = old_target if repaired_stop >= entry else entry + ((entry - repaired_stop) * rr)
    else:
        repaired_stop = old_stop
        repaired_target = old_target

    if runner_mode:
        repaired_target = 0.0

    if repaired_stop > 0 and abs(repaired_stop - old_stop) > 1e-9:
        candidate.stop = float(repaired_stop)
        changed = True
    if runner_mode and old_target > 0:
        candidate.target = 0.0
        changed = True
    elif repaired_target > 0 and (old_target <= 0 or abs(repaired_target - old_target) > 1e-9):
        candidate.target = float(repaired_target)
        changed = True

    if str(getattr(candidate, "action", "ENTRY") or "").upper() != "EXIT":
        return changed

    observed_price = float(
        getattr(candidate, "exit_price", 0.0)
        or getattr(candidate, "current_price", 0.0)
        or getattr(candidate, "price", 0.0)
        or 0.0
    )
    if observed_price <= 0:
        return changed

    original_reason = str(getattr(candidate, "exit_reason", "") or "EXIT_SIGNAL").upper()
    reason_text = original_reason.replace("_", " ").title()
    exit_reason = str(getattr(candidate, "exit_reason", "") or "EXIT_SIGNAL")
    exit_px = observed_price
    if (
        original_reason in {"SMART_PROFIT_LOCK", "MAJOR_WIN_GUARD"}
        and candidate_net_pnl(candidate, observed_price) <= 0
    ):
        exit_reason = "LEGACY_LOCK_ERROR"
        reason_text = "Legacy invalid profit-lock loss"
    proactive_reasons = {
        "SMART_PROFIT_LOCK",
        "LOW_GAIN_PROTECT",
        "MAJOR_WIN_GUARD",
        "STAGNATION_EXIT",
        "INTEL_FLIP",
        "QUOTE_INVALID",
    }
    if original_reason in proactive_reasons:
        exit_px = observed_price
    elif original_reason in {"STOP_HIT", "TRAILING_STOP", "SL HIT", "SL_HIT"}:
        exit_px = float(candidate.stop)
        exit_reason = "STOP_HIT" if original_reason != "SL HIT" else "SL HIT"
        reason_text = "Stoploss hit"
    elif original_reason == "TARGET_HIT":
        exit_px = float(candidate.target)
        reason_text = "Target hit"
    else:
        exit_px, exit_reason, reason_text = candidate_exit_resolution(
            candidate,
            observed_price,
            exit_reason,
            reason_text,
        )

    new_pnl = 0.0 if original_reason == "QUOTE_INVALID" else candidate_net_pnl(candidate, float(exit_px))
    if (
        abs(float(exit_px) - observed_price) > 1e-9
        or abs(float(getattr(candidate, "pnl", 0.0) or 0.0) - new_pnl) > 1e-9
        or getattr(candidate, "exit_reason", "") != exit_reason
    ):
        changed = True

    candidate.current_price = float(exit_px)
    candidate.pnl = new_pnl
    candidate.exit_reason = exit_reason
    setattr(candidate, "exit_price", float(exit_px))
    if changed:
        reasons = list(getattr(candidate, "reasons", []) or [])
        repair_reason = f"{reason_text} reconciled from persisted exit {observed_price:.2f}"
        if repair_reason not in reasons:
            candidate.reasons = reasons + [repair_reason]
    return changed
