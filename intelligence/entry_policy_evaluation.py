"""After-cost evaluation for executable entry-decision shadow outcomes.

The live scanner writes one ``SHADOW_OUTCOME`` rtracker event per signal and
horizon.  This module is deliberately pure and advisory: it reads those rows,
computes comparable accepted/rejected economics, and never changes a setting or
live decision.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


GROUP_FIELDS: Tuple[str, ...] = (
    "instrument",
    "timeframe",
    "regime",
    "source_lane",
    "decision_group",
)


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _quantile(values: Sequence[float], probability: float) -> Optional[float]:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, float(probability))) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _canonical_shadow_row(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(event, dict):
        return None
    if str(event.get("decision") or "").upper() != "SHADOW_OUTCOME":
        return None
    shadow = event.get("shadow_outcome")
    if not isinstance(shadow, dict):
        return None
    horizon = _number(shadow.get("horizon_minutes"))
    decision = str(shadow.get("decision_group") or "").upper()
    if horizon is None or horizon <= 0 or decision not in {"ACCEPTED", "REJECTED"}:
        return None
    regime = event.get("regime") if isinstance(event.get("regime"), dict) else {}
    forecast = (
        event.get("expected_move")
        if isinstance(event.get("expected_move"), dict)
        else {}
    )
    policy = event.get("entry_policy") if isinstance(event.get("entry_policy"), dict) else {}
    geometry = event.get("geometry") if isinstance(event.get("geometry"), dict) else {}
    completed_at = str(shadow.get("completed_at") or event.get("timestamp") or "")
    session_date = str(shadow.get("session_date") or event.get("date") or "")
    accepted_decision_to_fill = _number(
        shadow.get("accepted_decision_to_fill_delta_rs")
    )
    # Keep a stable, decision-neutral name for downstream aggregation while
    # retaining the accepted-only source field for backwards compatibility.
    decision_to_fill = _number(shadow.get("decision_to_fill_delta_rs"))
    if decision_to_fill is None:
        decision_to_fill = accepted_decision_to_fill
    # A rejected signal has no broker fill. Old archives occasionally populated this
    # field anyway; treating it as fill telemetry mixes a hypothetical touch with an
    # execution that never happened.
    if decision != "ACCEPTED":
        accepted_decision_to_fill = None
        decision_to_fill = None
    actual_fill_slippage = _number(
        shadow.get("accepted_actual_fill_slippage_rs")
    )
    if actual_fill_slippage is None and str(
        shadow.get("entry_slippage_basis") or ""
    ) == "executable_touch_to_actual_fill":
        actual_fill_slippage = _number(shadow.get("entry_slippage_rs"))
    if decision != "ACCEPTED":
        actual_fill_slippage = None
    directional_mfe = _number(shadow.get("directional_mfe_pct"))
    underlying_observations = _number(
        shadow.get("underlying_observation_count")
    )
    if underlying_observations is None or underlying_observations < 1:
        directional_mfe = None
    row = {
        "event_id": str(event.get("id") or ""),
        "lifecycle_id": str(event.get("lifecycle_id") or ""),
        "session_date": session_date,
        "completed_at": completed_at,
        "instrument": str(event.get("instrument") or "UNKNOWN").upper(),
        "timeframe": str(event.get("timeframe") or "UNKNOWN").lower(),
        "direction": str(event.get("direction") or "").upper(),
        "regime": str(
            regime.get("smoothed_regime")
            or regime.get("hard_label")
            or shadow.get("regime")
            or "UNKNOWN"
        ).upper(),
        "source_lane": str(event.get("source_lane") or shadow.get("source_lane") or "UNKNOWN"),
        "decision_group": decision,
        "horizon_minutes": int(round(horizon)),
        "outcome_available": bool(shadow.get("outcome_available", False)),
        "unavailable_reason": str(shadow.get("unavailable_reason") or ""),
        "after_cost_pnl_rs": _number(shadow.get("after_cost_pnl_rs")),
        "gross_pnl_rs": _number(shadow.get("gross_pnl_rs")),
        "mfe_gross_rs": _number(shadow.get("mfe_gross_rs")),
        "mae_gross_rs": _number(shadow.get("mae_gross_rs")),
        "directional_mfe_pct": directional_mfe,
        "underlying_observation_count": (
            int(underlying_observations)
            if underlying_observations is not None and underlying_observations >= 1
            else 0
        ),
        "underlying_last_observed_at": str(
            shadow.get("underlying_last_observed_at") or ""
        ),
        "underlying_last_source": str(
            shadow.get("underlying_last_source") or ""
        ),
        "entry_slippage_rs": actual_fill_slippage,
        "decision_price_basis": str(
            shadow.get("decision_price_basis") or "UNKNOWN"
        ),
        "decision_to_executable_delta_rs": _number(
            shadow.get("decision_to_executable_delta_rs")
        ),
        "entry_spread_cost_rs": _number(shadow.get("entry_spread_cost_rs")),
        "round_trip_cost_rs": _number(shadow.get("round_trip_cost_rs")),
        "entry_quote_age_seconds": _number(shadow.get("entry_quote_age_seconds")),
        "exit_quote_age_seconds": _number(shadow.get("exit_quote_age_seconds")),
        "entry_quote_delay_seconds": _number(shadow.get("entry_quote_delay_seconds")),
        "exit_quote_delay_seconds": _number(shadow.get("exit_quote_delay_seconds")),
        "accepted_actual_fill_slippage_rs": actual_fill_slippage,
        "accepted_decision_to_fill_delta_rs": accepted_decision_to_fill,
        "decision_to_fill_delta_rs": decision_to_fill,
        "forecast_probability": _number(forecast.get("probability_meeting_floor")),
        "forecast_floor_pct": _number(forecast.get("floor_pct")),
        "forecast_value_pct": _number(forecast.get("expected_favorable_move_pct")),
        "forecast_model_version": str(forecast.get("model_version") or ""),
        "policy_actual_rr": _number(policy.get("actual_rr")),
        "policy_min_rr": _number(policy.get("min_rr")),
        "geometry_conversion_ratio": _number(
            geometry.get("geometry_conversion_ratio", geometry.get("conversion_ratio"))
        ),
        "underlying_entry": _number(geometry.get("underlying_entry")),
        "underlying_entry_basis": str(
            geometry.get("underlying_entry_basis") or "UNKNOWN"
        ),
    }
    return row


def extract_shadow_rows(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return normalized, deduplicated shadow rows from rtracker events."""
    rows: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    for event in events:
        row = _canonical_shadow_row(event)
        if row is None:
            continue
        key = (
            row["lifecycle_id"] or row["event_id"],
            row["decision_group"],
            row["horizon_minutes"],
        )
        previous = rows.get(key)
        # Prefer an executable outcome over an unavailable timeout if malformed or
        # externally merged archives contain both. Otherwise keep the later record.
        if previous is None or (
            row["outcome_available"] and not previous["outcome_available"]
        ) or (
            row["outcome_available"] == previous["outcome_available"]
            and row["completed_at"] > previous["completed_at"]
        ):
            rows[key] = row
    return sorted(
        rows.values(),
        key=lambda row: (row["session_date"], row["completed_at"], row["event_id"]),
    )


def _max_drawdown(pnls: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for pnl in pnls:
        equity += float(pnl)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _metrics(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    entry_rows = list(rows)
    available = [
        row
        for row in rows
        if bool(row.get("outcome_available"))
        and _number(row.get("after_cost_pnl_rs")) is not None
    ]
    pnls = [float(row["after_cost_pnl_rs"]) for row in available]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    tail_cutoff = _quantile(pnls, 0.05)
    tail_rows = [pnl for pnl in pnls if tail_cutoff is not None and pnl <= tail_cutoff]
    slippage = [
        value
        for value in (_number(row.get("entry_slippage_rs")) for row in entry_rows)
        if value is not None
    ]
    decision_to_executable = [
        value
        for value in (
            _number(row.get("decision_to_executable_delta_rs")) for row in entry_rows
        )
        if value is not None
    ]
    spreads = [
        value
        for value in (_number(row.get("entry_spread_cost_rs")) for row in entry_rows)
        if value is not None
    ]
    entry_quote_ages = [
        value
        for value in (_number(row.get("entry_quote_age_seconds")) for row in entry_rows)
        if value is not None
    ]
    exit_quote_ages = [
        value
        for value in (_number(row.get("exit_quote_age_seconds")) for row in available)
        if value is not None
    ]
    entry_quote_delays = [
        value
        for value in (_number(row.get("entry_quote_delay_seconds")) for row in entry_rows)
        if value is not None
    ]
    exit_quote_delays = [
        value
        for value in (_number(row.get("exit_quote_delay_seconds")) for row in available)
        if value is not None
    ]
    actual_fill_slippage = [
        value
        for value in (
            _number(row.get("accepted_actual_fill_slippage_rs"))
            for row in entry_rows
            if row.get("decision_group") == "ACCEPTED"
        )
        if value is not None
    ]
    decision_to_fill = [
        value
        for value in (_number(row.get("decision_to_fill_delta_rs")) for row in rows)
        if value is not None
    ]
    accepted_decision_to_fill = [
        value
        for value in (
            _number(row.get("accepted_decision_to_fill_delta_rs"))
            for row in rows
            if row.get("decision_group") == "ACCEPTED"
        )
        if value is not None
    ]

    calibration_pairs = []
    for row in available:
        probability = _number(row.get("forecast_probability"))
        floor_pct = _number(row.get("forecast_floor_pct"))
        directional_mfe = _number(row.get("directional_mfe_pct"))
        observation_count = _number(row.get("underlying_observation_count"))
        if (
            probability is None
            or floor_pct is None
            or directional_mfe is None
            or observation_count is None
            or observation_count < 1
        ):
            continue
        if not 0.0 <= probability <= 1.0 or floor_pct < 0:
            continue
        calibration_pairs.append((probability, 1.0 if directional_mfe >= floor_pct else 0.0))
    mean_probability = (
        sum(pair[0] for pair in calibration_pairs) / len(calibration_pairs)
        if calibration_pairs
        else None
    )
    observed_rate = (
        sum(pair[1] for pair in calibration_pairs) / len(calibration_pairs)
        if calibration_pairs
        else None
    )
    brier = (
        sum((probability - actual) ** 2 for probability, actual in calibration_pairs)
        / len(calibration_pairs)
        if calibration_pairs
        else None
    )
    return {
        "rows": len(rows),
        "outcomes": len(available),
        "coverage_pct": round(100.0 * len(available) / len(rows), 2) if rows else 0.0,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(pnls), 2) if pnls else None,
        "after_cost_pnl_rs": round(sum(pnls), 2),
        "after_cost_expectancy_rs": round(sum(pnls) / len(pnls), 2) if pnls else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        "max_drawdown_rs": round(_max_drawdown(pnls), 2) if pnls else None,
        "tail_loss_p05_rs": round(tail_cutoff, 2) if tail_cutoff is not None else None,
        "tail_expected_shortfall_rs": (
            round(sum(tail_rows) / len(tail_rows), 2) if tail_rows else None
        ),
        "worst_outcome_rs": round(min(pnls), 2) if pnls else None,
        "avg_entry_slippage_rs": (
            round(sum(slippage) / len(slippage), 2) if slippage else None
        ),
        "entry_slippage_samples": len(slippage),
        "avg_executable_to_fill_slippage_rs": (
            round(sum(slippage) / len(slippage), 2) if slippage else None
        ),
        "decision_to_executable_samples": len(decision_to_executable),
        "avg_decision_to_executable_delta_rs": (
            round(sum(decision_to_executable) / len(decision_to_executable), 2)
            if decision_to_executable
            else None
        ),
        "avg_entry_spread_cost_rs": (
            round(sum(spreads) / len(spreads), 2) if spreads else None
        ),
        "entry_spread_samples": len(spreads),
        "avg_entry_quote_age_seconds": (
            round(sum(entry_quote_ages) / len(entry_quote_ages), 3)
            if entry_quote_ages
            else None
        ),
        "entry_quote_age_samples": len(entry_quote_ages),
        "avg_exit_quote_age_seconds": (
            round(sum(exit_quote_ages) / len(exit_quote_ages), 3)
            if exit_quote_ages
            else None
        ),
        "avg_entry_quote_delay_seconds": (
            round(sum(entry_quote_delays) / len(entry_quote_delays), 3)
            if entry_quote_delays
            else None
        ),
        "avg_exit_quote_delay_seconds": (
            round(sum(exit_quote_delays) / len(exit_quote_delays), 3)
            if exit_quote_delays
            else None
        ),
        "avg_accepted_actual_fill_slippage_rs": (
            round(sum(actual_fill_slippage) / len(actual_fill_slippage), 2)
            if actual_fill_slippage
            else None
        ),
        "accepted_actual_fill_slippage_samples": len(actual_fill_slippage),
        # These are measured from the decision price to the eventual accepted
        # fill, not from the executable touch.  Positive values are adverse by
        # construction in the scanner's LONG/SHORT-normalized convention.
        "decision_to_fill_samples": len(decision_to_fill),
        "avg_decision_to_fill_delta_rs": (
            round(sum(decision_to_fill) / len(decision_to_fill), 2)
            if decision_to_fill
            else None
        ),
        "p95_decision_to_fill_delta_rs": (
            round(_quantile(decision_to_fill, 0.95), 2)
            if decision_to_fill
            else None
        ),
        "accepted_decision_to_fill_samples": len(accepted_decision_to_fill),
        "avg_accepted_decision_to_fill_delta_rs": (
            round(sum(accepted_decision_to_fill) / len(accepted_decision_to_fill), 2)
            if accepted_decision_to_fill
            else None
        ),
        "p95_accepted_decision_to_fill_delta_rs": (
            round(_quantile(accepted_decision_to_fill, 0.95), 2)
            if accepted_decision_to_fill
            else None
        ),
        "decision_price_basis_counts": dict(
            sorted(
                Counter(
                    str(row.get("decision_price_basis") or "UNKNOWN")
                    for row in entry_rows
                ).items()
            )
        ),
        "underlying_entry_basis_counts": dict(
            sorted(
                Counter(
                    str(row.get("underlying_entry_basis") or "UNKNOWN")
                    for row in entry_rows
                ).items()
            )
        ),
        "calibration": {
            "samples": len(calibration_pairs),
            "mean_forecast_probability": (
                round(mean_probability, 4) if mean_probability is not None else None
            ),
            "observed_floor_hit_rate": (
                round(observed_rate, 4) if observed_rate is not None else None
            ),
            "absolute_calibration_error": (
                round(abs(mean_probability - observed_rate), 4)
                if mean_probability is not None and observed_rate is not None
                else None
            ),
            "brier_score": round(brier, 4) if brier is not None else None,
        },
    }


def _decision_metrics(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        decision.lower(): _metrics(
            [row for row in rows if row.get("decision_group") == decision]
        )
        for decision in ("ACCEPTED", "REJECTED")
    }


def evaluate_entry_shadow_walk_forward(
    events: Iterable[Dict[str, Any]],
    *,
    horizon_minutes: int = 30,
    min_train_sessions: int = 5,
    test_sessions: int = 1,
) -> Dict[str, Any]:
    """Evaluate one horizon with chronological expanding walk-forward folds.

    Training rows are exposed so a researcher can freeze a proposed rule using only
    past sessions; each test block remains the next unseen session(s). No threshold is
    selected automatically and nothing is promoted into live settings.
    """
    all_rows = extract_shadow_rows(events)
    horizon = max(1, int(horizon_minutes or 30))
    rows = [row for row in all_rows if row["horizon_minutes"] == horizon]
    sessions = sorted({row["session_date"] for row in rows if row["session_date"]})
    minimum = max(1, int(min_train_sessions or 1))
    block = max(1, int(test_sessions or 1))

    grouped: Dict[Tuple[str, ...], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(str(row.get(field) or "UNKNOWN") for field in GROUP_FIELDS)].append(row)
    group_rows = []
    for key, group in grouped.items():
        identity = dict(zip(GROUP_FIELDS, key))
        identity["metrics"] = _metrics(group)
        group_rows.append(identity)
    group_rows.sort(
        key=lambda item: (
            item["instrument"],
            item["timeframe"],
            item["regime"],
            item["source_lane"],
            item["decision_group"],
        )
    )

    folds = []
    for split in range(minimum, len(sessions), block):
        train_days = sessions[:split]
        test_days = sessions[split : split + block]
        if not test_days:
            break
        train_set, test_set = set(train_days), set(test_days)
        train_rows = [row for row in rows if row["session_date"] in train_set]
        test_rows = [row for row in rows if row["session_date"] in test_set]
        folds.append(
            {
                "train_sessions": train_days,
                "test_sessions": test_days,
                "train": _decision_metrics(train_rows),
                "test": _decision_metrics(test_rows),
            }
        )

    unavailable = Counter(
        row["unavailable_reason"] or "unspecified"
        for row in rows
        if not row["outcome_available"]
    )
    versions = Counter(
        row["forecast_model_version"] or "UNKNOWN" for row in rows
    )
    decision_summary = _decision_metrics(rows)
    accepted_expectancy = decision_summary["accepted"].get("after_cost_expectancy_rs")
    rejected_expectancy = decision_summary["rejected"].get("after_cost_expectancy_rs")
    expectancy_edge = (
        round(float(accepted_expectancy) - float(rejected_expectancy), 2)
        if accepted_expectancy is not None and rejected_expectancy is not None
        else None
    )
    return {
        "mode": "ADVISORY_ONLY",
        "live_settings_changed": False,
        "horizon_minutes": horizon,
        "sessions": sessions,
        "rows": len(rows),
        "available_outcomes": sum(bool(row["outcome_available"]) for row in rows),
        "unavailable_reasons": dict(sorted(unavailable.items())),
        "forecast_model_versions": dict(sorted(versions.items())),
        "overall": _metrics(rows),
        "by_decision": decision_summary,
        "accepted_minus_rejected_expectancy_rs": expectancy_edge,
        "by_index_timeframe_regime_lane_decision": group_rows,
        "walk_forward": {
            "method": "expanding_train_then_next_unseen_session_block",
            "minimum_train_sessions": minimum,
            "test_sessions_per_fold": block,
            "folds": folds,
        },
        "promotion_rule": (
            "Do not tune live settings from in-sample win rate. Require positive "
            "after-cost expectancy and profit factor, acceptable drawdown/tail loss, "
            "stable slippage, and calibrated forecast probabilities across unseen folds."
        ),
    }


__all__ = [
    "GROUP_FIELDS",
    "evaluate_entry_shadow_walk_forward",
    "extract_shadow_rows",
]
