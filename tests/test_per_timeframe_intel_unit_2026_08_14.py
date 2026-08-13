"""A small legacy intelligence score reads as a huge canonical one. NOT YET FIXED.

This file documents a real unit hazard and pins the two facts that keep it dormant.
It deliberately does NOT assert a fix, because every one-line fix tried made things
worse, and the guard here is what proved that.

THE HAZARD
_build_timeframe_contexts fills the per-timeframe intelligence that the
per_timeframe_grading_enabled flag routes into grading:

    "intel_score": aggregate.get("score", aggregate.get("intel_score", 0.0)),

aggregate["score"] is ALWAYS legacy-scaled (-100..100). It reaches
normalize_intelligence_score, which infers the scale from MAGNITUDE:

    if abs(score) > 1.0:
        score /= LEGACY_SCORE_SCALE

A legacy 0.9 -- all but neutral on -100..100 -- is under that threshold, so it is
never divided and reads as canonical 0.9: near-maximum conviction, wrong by 100x.
Only SMALL readings are affected, which is what makes it quiet. The loud cases
(70 -> 0.70) look perfectly correct.

WHY IT IS NOT A ONE-LINER
  - Store the corrected LEGACY value (aggregate_intelligence_score * 100) and it
    round-trips straight back into the same magnitude test: 0.009 * 100 = 0.9, and
    normalize sees abs(0.9) <= 1.0 and returns 0.9 again. No improvement at all.
  - Store the CANONICAL value and this path diverges from the flag-off path, which
    hands the caller's legacy intel_score to the same consumers. _grade_signal would
    receive ~100x less conviction than it does today.

The real fix is to make the whole per-timeframe route canonical -- fallback included
-- which is a prerequisite for ever enabling the flag, not a patch to this line.

WHY IT IS DORMANT
per_timeframe_grading_enabled defaults False, so these contexts are never consulted.
The last test below fails the moment that default flips, which is the point.
"""
import inspect

import pytest

from engine.intelligence_score import (
    LEGACY_SCORE_SCALE,
    aggregate_intelligence_score,
    normalize_intelligence_score,
)
from engine.signal_processor import SignalProcessor


# ── the hazard is real ───────────────────────────────────────────────────────

def test_a_near_neutral_legacy_score_reads_as_near_maximum():
    """0.9 on a -100..100 scale is ~neutral. The magnitude guess calls it 0.9."""
    assert normalize_intelligence_score(0.9) == pytest.approx(0.9)

    correct = aggregate_intelligence_score({"score": 0.9})
    assert correct == pytest.approx(0.009)


@pytest.mark.parametrize("legacy", [0.1, 0.5, 0.9, -0.9])
def test_the_magnitude_guess_is_lossy_below_one(legacy):
    assert normalize_intelligence_score(legacy) == pytest.approx(legacy)
    assert aggregate_intelligence_score({"score": legacy}) == pytest.approx(
        legacy / LEGACY_SCORE_SCALE
    )


def test_ordinary_scores_are_unaffected():
    """Why this stayed hidden: everything above 1.0 behaves correctly."""
    for legacy in (70.0, -70.0, 42.0, 100.0):
        assert normalize_intelligence_score(legacy) == pytest.approx(
            legacy / LEGACY_SCORE_SCALE
        )


# ── why the obvious fixes do not work ────────────────────────────────────────

def test_rescaling_back_to_legacy_does_not_help():
    """Pins the failed fix so it is not attempted again."""
    stored = aggregate_intelligence_score({"score": 0.9}) * LEGACY_SCORE_SCALE

    assert normalize_intelligence_score(stored) == pytest.approx(0.9), (
        "the round trip lands back on the wrong value -- the heuristic is lossy "
        "for any |legacy| <= 1, so the legacy unit cannot carry these readings"
    )


def test_the_canonical_value_is_a_hundred_times_smaller():
    """Why storing canonical here would break grading on the flag-off path."""
    legacy = 70.0
    canonical = aggregate_intelligence_score({"score": legacy})

    assert legacy / canonical == pytest.approx(LEGACY_SCORE_SCALE)


# ── the guards that keep it dormant ──────────────────────────────────────────

def test_the_hazard_is_documented_at_the_site():
    src = inspect.getsource(SignalProcessor)
    i = src.index('"intel_score": aggregate.get("score"')

    assert "UNIT HAZARD" in src[i - 1400: i], (
        "the warning above this line is the only thing telling the next reader why "
        "the obvious fix is wrong"
    )


def test_per_timeframe_grading_is_still_off():
    """The hazard is unreachable only while this is False.

    If this test fails because the default was flipped, the unit handling on the
    per-timeframe route must be made canonical FIRST -- read this file's docstring.
    """
    from config.settings import Settings

    field = Settings.model_fields.get("per_timeframe_grading_enabled")
    if field is None:
        pytest.skip("flag not defined as a setting")

    assert field.default is False, (
        "per-timeframe grading was enabled while the intelligence unit on that route "
        "is still legacy-scaled and read through a magnitude guess"
    )
