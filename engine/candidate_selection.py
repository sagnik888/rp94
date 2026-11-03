"""Pure candidate-ranking phase extracted from SignalProcessor."""

from __future__ import annotations

from typing import Any, Callable, Iterable


def candidate_selection_score(raw: dict[str, Any], lane_for_signal: Callable[[Any], str]) -> float:
    signal = raw["sig"]
    timeframe = str(raw.get("tf") or "")
    score = float(raw.get("confidence", 0.0) or 0.0)
    if lane_for_signal(signal) == "UTS":
        score = 0.0  # Native UTS selection never ranks candidates by display grade.
    if timeframe == "15min":
        score += 0.0001
    if bool(getattr(signal, "volume_spike_entry", False)) or bool(
        getattr(signal, "market_ltp_shock_entry", False)
    ):
        score += 1.0
    if lane_for_signal(signal) == "B":
        score += 0.75
    return score


def rank_entry_candidates(
    candidates: Iterable[dict[str, Any]],
    lane_for_signal: Callable[[Any], str],
) -> list[tuple[float, Any, str, str, float, list[str], bool]]:
    """Rank eligible raw setups by quality; native UTS keeps temporal priority."""
    ranked = []
    for raw in candidates:
        ranked.append(
            (
                candidate_selection_score(raw, lane_for_signal),
                raw["sig"],
                raw["tf"],
                raw["grade"],
                raw["confidence"],
                raw["reasons"],
                raw.get("after_no_entry", False),
            )
        )
    ranked.sort(
        key=lambda item: (
            0.0 if lane_for_signal(item[1]) == "UTS" else item[0],
            item[1].timestamp,
            item[0],
        ),
        reverse=True,
    )
    return ranked
