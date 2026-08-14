"""A small legacy intelligence score read as a huge canonical one. NOW FIXED.

THE HAZARD
_build_timeframe_contexts filled the per-timeframe intelligence that the
per_timeframe_grading_enabled flag routes into grading, and read it raw:

    "intel_score": aggregate.get("score", aggregate.get("intel_score", 0.0)),

aggregate["score"] is ALWAYS legacy-scaled (-100..100). It reached
normalize_intelligence_score, which infers the scale from MAGNITUDE:

    if abs(score) > 1.0:
        score /= LEGACY_SCORE_SCALE

A legacy 0.9 -- all but neutral on -100..100 -- is under that threshold, so it was
never divided and read as canonical 0.9: near-maximum conviction, wrong by 100x. Only
SMALL readings were affected, which is what made it quiet. The loud cases (70 -> 0.70)
looked perfectly correct.

WHY THE EARLIER "NOT A ONE-LINER" VERDICT WAS WRONG
This file previously refused to assert a fix, on the reasoning that storing the
canonical value "diverges from the flag-off path, which hands the caller's LEGACY
intel_score to the same consumers".

That premise was false, and checking it is all it took. scanner.py:10414 computes the
caller's value as aggregate_intelligence_score(intel_result["aggregate"]) and passes it
to process_best_signal unchanged -- so the flag-off path was ALREADY canonical. Storing
canonical here CONVERGES the two paths instead of splitting them.

The other bullet was correct: storing a corrected LEGACY value would round-trip back
through the same magnitude test and improve nothing. Only the canonical form works.
"""
import inspect

import pytest

from engine.intelligence_score import (
    LEGACY_SCORE_SCALE,
    aggregate_intelligence_score,
    normalize_intelligence_score,
)
from engine.signal_processor import SignalProcessor


# ── the conversion itself ────────────────────────────────────────────────────

def test_a_near_neutral_legacy_score_stays_near_neutral():
    """The whole bug in one line: 0.9 legacy is 0.9%, not 90%."""
    assert aggregate_intelligence_score({"score": 0.9}) == pytest.approx(0.009)


def test_the_magnitude_guess_would_have_got_that_wrong():
    """Pins WHY the raw read was unsafe, so the reason cannot be forgotten."""
    assert normalize_intelligence_score(0.9) == pytest.approx(0.9)
    assert normalize_intelligence_score(0.9) != pytest.approx(0.009)


def test_a_loud_reading_is_unchanged():
    """70 -> 0.70 both ways, which is why the bug was invisible in normal ranges."""
    assert aggregate_intelligence_score({"score": 70.0}) == pytest.approx(0.70)
    assert normalize_intelligence_score(70.0) == pytest.approx(0.70)


@pytest.mark.parametrize("legacy", [0.9, -0.9, 70.0, -85.0, 0.0, 100.0, -100.0])
def test_normalising_a_canonical_value_is_a_no_op(legacy):
    """Downstream still calls normalize; on canonical input it must change nothing."""
    canonical = aggregate_intelligence_score({"score": legacy})

    assert normalize_intelligence_score(canonical) == pytest.approx(canonical)


def test_score_norm_is_preferred_over_the_legacy_field():
    """The aggregator publishes the explicit value; it must win."""
    payload = {"score": 90.0, "score_norm": 0.12}

    assert aggregate_intelligence_score(payload) == pytest.approx(0.12)


# ── the context builder no longer guesses ────────────────────────────────────

def test_the_builder_reads_the_aggregate_explicitly():
    src = inspect.getsource(SignalProcessor._build_timeframe_contexts)
    # The comment explains the old read by quoting it; only executable lines matter.
    code = " ".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )

    assert "aggregate_intelligence_score(aggregate)" in code, (
        "the per-timeframe score is being inferred from magnitude again"
    )
    assert 'aggregate.get("score"' not in code, "the raw legacy field is being read again"


def test_both_routes_now_agree_on_the_scale():
    """Flag on and flag off must hand grading the same units.

    The flag-off path supplies the caller's intel_score, computed by scanner.py with
    aggregate_intelligence_score. The flag-on path now computes it the same way, so
    enabling the flag changes WHICH timeframe's intelligence is used and not what a
    number means.
    """
    import scanner as scanner_module

    scanner_src = inspect.getsource(scanner_module)
    builder_src = inspect.getsource(SignalProcessor._build_timeframe_contexts)

    assert "intel_score = aggregate_intelligence_score(" in scanner_src
    assert "aggregate_intelligence_score(aggregate)" in builder_src


def test_the_fallback_path_is_untouched_when_the_flag_is_off():
    """Disabled still returns None, so grading stays byte-identical."""
    src = inspect.getsource(SignalProcessor._build_timeframe_contexts)
    i = src.index("per_timeframe_grading_enabled")

    assert "return None" in src[i: i + 300]


# ── the flag is still off, and that is a separate decision ───────────────────

def test_per_timeframe_grading_is_still_disabled_by_default():
    """The unit hazard is fixed; whether to ENABLE the route is its own question.

    The remaining reason to leave it off is unrelated to units: the candidate's nested
    intelligence context and several pre-entry gates still read the outer 5-minute
    bundle, so enabling it would grade on one timeframe and gate on another.
    """
    from config.settings import Settings

    field = Settings.model_fields.get("per_timeframe_grading_enabled")
    assert field is not None
    assert field.default is False
