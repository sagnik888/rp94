"""Fail-closed strategy promotion gate for paired causal evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


REQUIRED_GATES = (
    "starting_capital_provided",
    "causal_no_network_attempts",
    "missing_data_accounted",
    "costs_accounted",
    "paired_coverage",
    "paired_net_improves",
    "drawdown_acceptable",
    "single_day_concentration_acceptable",
    "positive_unseen_periods",
    "all_indices_robust",
)


@dataclass(frozen=True)
class PromotionDecision:
    allowed: bool
    mode: str
    blockers: tuple[str, ...]


def assess_promotion(report: dict[str, Any] | None) -> PromotionDecision:
    payload = report if isinstance(report, dict) else {}
    gates = payload.get("gates") if isinstance(payload.get("gates"), dict) else {}
    blockers = tuple(name for name in REQUIRED_GATES if gates.get(name) is not True)
    schema_ok = payload.get("schema") == "paired_causal_evaluation.v1"
    if not schema_ok:
        blockers = ("valid_paired_causal_evaluation", *blockers)
    promotable = payload.get("promotable") is True
    if not promotable:
        blockers = (*blockers, "report_promotable")
    blockers = tuple(dict.fromkeys(blockers))
    return PromotionDecision(
        allowed=not blockers,
        mode="PROMOTED" if not blockers else "SHADOW_ONLY",
        blockers=blockers,
    )
