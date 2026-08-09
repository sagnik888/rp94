"""Canonical intelligence-score units used by live and historical paths.

TWO scales exist and they overlap, which is the whole problem:

  * the aggregator PUBLISHES ``aggregate["score"]`` on a legacy -100..+100 scale
  * every candidate/signal attribute carries the canonical -1..+1

``normalize_intelligence_score`` guesses which one it was handed from the magnitude.
That guess is unrecoverably wrong in the band where the scales overlap: a published
0.9 means 0.9% bullish -- a genuinely NEUTRAL read -- but ``abs(0.9) > 1.0`` is False,
so the divide is skipped and 0.9 is taken as 90% conviction. That feeds the 30%
intelligence pillar (0.27 of 0.30) and the ``>= 0.70`` "strong intelligence" gates.

So aggregate readers must NOT guess. Use ``aggregate_intelligence_score``, which reads
the explicit ``score_norm`` the aggregator publishes. The magnitude heuristic stays
only for values that are already canonical, where it is a no-op.
"""

from __future__ import annotations

from collections.abc import Mapping

#: ``aggregate["score"]`` is this many times the canonical -1..+1 value.
LEGACY_SCORE_SCALE = 100.0


def normalize_intelligence_score(value: float) -> float:
    """Normalize legacy -100..100 scores and canonical -1..1 scores to -1..1.

    Only safe on values that are ALREADY canonical (candidate attributes, a score a
    caller normalized earlier), where it is idempotent. For an aggregator payload use
    ``aggregate_intelligence_score`` -- see the module docstring for why.
    """
    try:
        score = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if score != score:  # NaN
        return 0.0
    if abs(score) > 1.0:
        score /= LEGACY_SCORE_SCALE
    return max(-1.0, min(1.0, score))


def aggregate_intelligence_score(aggregate) -> float:
    """Canonical -1..+1 conviction from an aggregator ``aggregate`` payload.

    Prefers the explicit ``score_norm``. Falls back to dividing ``score`` -- always,
    regardless of magnitude, because that field is always legacy-scaled. Payloads
    predating ``score_norm`` (persisted intelligence memory, rtracker archives) take
    the fallback and still land on the right scale.
    """
    if not isinstance(aggregate, Mapping):
        return 0.0
    raw = aggregate.get("score_norm")
    if raw is not None:
        try:
            score = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return 0.0 if score != score else max(-1.0, min(1.0, score))
    try:
        score = float(aggregate.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if score != score:
        return 0.0
    return max(-1.0, min(1.0, score / LEGACY_SCORE_SCALE))
