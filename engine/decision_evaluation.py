"""Paired causal replay comparison and robustness promotion gates."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable


@dataclass(frozen=True)
class EvaluationThresholds:
    max_drawdown_pct: float = 20.0
    max_single_day_profit_concentration: float = 0.35
    min_positive_unseen_period_ratio: float = 0.55
    min_index_profit_factor: float = 1.0
    min_pair_coverage: float = 0.95


def _pnl(trade: dict[str, Any]) -> float:
    try:
        return float(trade.get("pnl") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _identity(trade: dict[str, Any]) -> str:
    return str(
        trade.get("decision_id")
        or (trade.get("decision_record") or {}).get("decision_id")
        or trade.get("lifecycle_id")
        or trade.get("id")
        or ""
    )


def _index_name(trade: dict[str, Any]) -> str:
    explicit = str(trade.get("index") or trade.get("underlying") or "").upper()
    if explicit:
        return explicit
    return str(trade.get("instrument") or "UNKNOWN").upper().split()[0]


def _period(trade: dict[str, Any]) -> str:
    for key in ("exit_timestamp", "entry_timestamp"):
        value = trade.get(key)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value)).date().isoformat()
        if value:
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
            except ValueError:
                pass
    return "UNKNOWN"


def trade_metrics(trades: Iterable[dict[str, Any]], starting_capital: float) -> dict[str, Any]:
    rows = sorted(
        (dict(row) for row in trades),
        key=lambda row: float(row.get("entry_timestamp") or 0.0),
    )
    pnls = [_pnl(row) for row in rows]
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = -sum(value for value in pnls if value < 0)
    equity = peak = drawdown = 0.0
    by_day: dict[str, float] = defaultdict(float)
    by_index: dict[str, list[float]] = defaultdict(list)
    for row, value in zip(rows, pnls):
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        by_day[_period(row)] += value
        by_index[_index_name(row)].append(value)
    positive_day_profit = sum(value for value in by_day.values() if value > 0)
    concentration = (
        max((value for value in by_day.values() if value > 0), default=0.0)
        / positive_day_profit
        if positive_day_profit > 0
        else 1.0
    )
    index_metrics = {}
    for name, values in sorted(by_index.items()):
        wins = sum(value for value in values if value > 0)
        losses = -sum(value for value in values if value < 0)
        index_metrics[name] = {
            "trades": len(values),
            "net": round(sum(values), 2),
            "profit_factor": round(wins / losses, 4) if losses > 0 else None,
        }
    return {
        "trades": len(rows),
        "net": round(sum(pnls), 2),
        "wins": sum(1 for value in pnls if value > 0),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        "max_drawdown": round(drawdown, 2),
        "max_drawdown_pct": (
            round(drawdown / float(starting_capital) * 100.0, 4)
            if starting_capital > 0
            else None
        ),
        "positive_period_ratio": (
            round(sum(value > 0 for value in by_day.values()) / len(by_day), 4)
            if by_day
            else 0.0
        ),
        "single_day_profit_concentration": round(concentration, 4),
        "periods": dict(sorted(by_day.items())),
        "indices": index_metrics,
    }


def _network_attempt_count(document: dict[str, Any]) -> int:
    value = document.get("network_attempts", (document.get("summary") or {}).get("network_attempts", 0))
    if isinstance(value, list):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def evaluate_paired_replays(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    starting_capital: float,
    thresholds: EvaluationThresholds = EvaluationThresholds(),
) -> dict[str, Any]:
    base_rows = [dict(row) for row in baseline.get("trades", []) if isinstance(row, dict)]
    candidate_rows = [dict(row) for row in candidate.get("trades", []) if isinstance(row, dict)]
    base_by_id = {_identity(row): row for row in base_rows if _identity(row)}
    candidate_by_id = {_identity(row): row for row in candidate_rows if _identity(row)}
    shared = sorted(set(base_by_id) & set(candidate_by_id))
    union = set(base_by_id) | set(candidate_by_id)
    coverage = len(shared) / len(union) if union else 0.0
    paired_base = [base_by_id[key] for key in shared]
    paired_candidate = [candidate_by_id[key] for key in shared]
    base_metrics = trade_metrics(paired_base, starting_capital)
    candidate_metrics = trade_metrics(paired_candidate, starting_capital)
    indices = candidate_metrics["indices"]
    min_pf_ok = all(
        item["net"] > 0
        and (item["profit_factor"] is None or item["profit_factor"] >= thresholds.min_index_profit_factor)
        for item in indices.values()
    ) and bool(indices)
    candidate_summary = candidate.get("summary") or {}
    missing_data_accounted = any(
        key in candidate for key in ("data_coverage", "historical_intelligence_coverage", "limitations")
    ) or "rejects" in candidate_summary
    costs_flag = candidate.get("costs_and_slippage_included")
    costs_accounted = (
        bool(costs_flag)
        if costs_flag is not None
        else bool(candidate_rows) and all("charges" in row for row in candidate_rows)
    )
    gates = {
        "starting_capital_provided": starting_capital > 0,
        "causal_no_network_attempts": _network_attempt_count(candidate) == 0,
        "missing_data_accounted": missing_data_accounted,
        "costs_accounted": costs_accounted,
        "paired_coverage": coverage >= thresholds.min_pair_coverage,
        "paired_net_improves": candidate_metrics["net"] > base_metrics["net"],
        "drawdown_acceptable": candidate_metrics["max_drawdown_pct"] is not None
        and candidate_metrics["max_drawdown_pct"] <= thresholds.max_drawdown_pct,
        "single_day_concentration_acceptable": candidate_metrics["single_day_profit_concentration"]
        <= thresholds.max_single_day_profit_concentration,
        "positive_unseen_periods": candidate_metrics["positive_period_ratio"]
        >= thresholds.min_positive_unseen_period_ratio,
        "all_indices_robust": min_pf_ok,
    }
    return {
        "schema": "paired_causal_evaluation.v1",
        "thresholds": asdict(thresholds),
        "pairing": {
            "baseline_trades": len(base_rows),
            "candidate_trades": len(candidate_rows),
            "matched": len(shared),
            "coverage": round(coverage, 4),
            "baseline_only": sorted(set(base_by_id) - set(candidate_by_id)),
            "candidate_only": sorted(set(candidate_by_id) - set(base_by_id)),
        },
        "baseline": base_metrics,
        "candidate": candidate_metrics,
        "paired_delta_net": round(candidate_metrics["net"] - base_metrics["net"], 2),
        "missing_data": {
            "rejects": dict(candidate_summary.get("rejects") or {}),
            "data_coverage": candidate.get("data_coverage"),
        },
        "gates": gates,
        "promotable": all(gates.values()),
    }
