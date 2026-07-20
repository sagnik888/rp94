"""Candidate normalization helpers shared by scanner/dashboard paths."""

from __future__ import annotations

from typing import Callable

from engine.market_regime import futures_stop_tolerance_pct
from engine.signal_processor import TradeCandidate


def normalize_loaded_exit_candidate(
    candidate: TradeCandidate,
    *,
    futures_sl_pct: float,
    options_sl_pct: float,
    candidate_exit_resolution: Callable[[TradeCandidate, float, str, str], tuple[float, str, str]],
    candidate_net_pnl: Callable[[TradeCandidate, float], float],
) -> bool:
    """Repair persisted live/session rows created by older builds before accounting them."""
    changed = False
    entry = float(getattr(candidate, "price", 0.0) or 0.0)
    if entry <= 0:
        return False

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
