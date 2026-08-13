"""Per-timeframe grading must reach the candidate, not stop at the grade.

A 2026-08-13 external audit claimed that enabling per_timeframe_grading_enabled
"affects only part of the route". Traced against this build, that was true:

    _candidate_timeframe_context   grades a 15m candidate on 15m intelligence
    sig.graded_regime / .graded_intel_score   record which context graded it
    candidate.market_regime = str(regime)     <- the 5-MINUTE value, unconditionally
    candidate.intelligence_score = intel_norm  <- normalize(5-minute intel_score)

`regime` and `intel_norm` are computed once per instrument from the 5-minute
snapshot, before candidate selection. So a 15-minute candidate was graded on 15m
intelligence and then stamped with the 5m regime — and every downstream consumer
reads the stamp, not the grade:

    scanner.py:1641   the opened trade's market_regime
    scanner.py:9157   candidate regime gate
    scanner.py:12492  exit-path regime read
    scanner.py:13359  live regime resolution
    scanner.py:15249  exit-path regime read

The flag therefore changed the grade and nothing else. _graded_candidate_context
carries the grading context through to the stamp.

Scope note: this is a no-op until the flag is turned on. With
per_timeframe_grading_enabled off, _candidate_timeframe_context returns the
caller's 5-minute values verbatim, so graded_regime == regime and
graded_intel_score == intel_score and the stamp is byte-identical to before.
"""
import inspect

import pytest

from engine.intelligence_score import normalize_intelligence_score
from engine.signal_processor import SignalProcessor


_resolve = SignalProcessor._graded_candidate_context


class _Sig:
    """A signal as the grading loop leaves it."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ── flag off: the previous 5-minute pair, unchanged ──────────────────────────

def test_an_ungraded_signal_keeps_the_five_minute_pair():
    """No graded_* attributes at all — the pre-fix behaviour must be preserved."""
    regime, norm = _resolve(_Sig(), "TRENDING_UP", 0.62)

    assert regime == "TRENDING_UP"
    assert norm == pytest.approx(0.62)


def test_grading_that_resolved_to_the_five_minute_values_is_identical():
    """Flag off: _candidate_timeframe_context echoes the caller's 5m values."""
    sig = _Sig(graded_regime="TRENDING_UP", graded_intel_score=70.0)

    regime, norm = _resolve(sig, "TRENDING_UP", normalize_intelligence_score(70.0))

    assert regime == "TRENDING_UP"
    assert norm == pytest.approx(normalize_intelligence_score(70.0))


# ── flag on: the candidate's own timeframe wins ──────────────────────────────

def test_the_candidates_own_regime_reaches_the_stamp():
    """The 15m candidate was graded RANGING; it must not be stamped TRENDING_UP."""
    sig = _Sig(graded_timeframe="15min", graded_regime="RANGING", graded_intel_score=15.0)

    regime, _ = _resolve(sig, "TRENDING_UP", 0.80)

    assert regime == "RANGING", "downstream gates read the stamp, not the grade"


def test_the_candidates_own_intelligence_reaches_the_stamp():
    sig = _Sig(graded_regime="RANGING", graded_intel_score=15.0)

    _, norm = _resolve(sig, "TRENDING_UP", 0.80)

    assert norm == pytest.approx(normalize_intelligence_score(15.0))


def test_the_stamped_score_is_normalised_not_raw():
    """graded_intel_score is the RAW score; intelligence_score is the canonical unit.

    Stamping the raw value would put a 0-100 number where every consumer expects
    the normalised scale.
    """
    sig = _Sig(graded_regime="RANGING", graded_intel_score=70.0)

    _, norm = _resolve(sig, "RANGING", 0.0)

    assert norm == pytest.approx(normalize_intelligence_score(70.0))
    assert norm != pytest.approx(70.0), "raw score leaked into the normalised field"


# ── absent is not zero ───────────────────────────────────────────────────────

def test_a_genuine_zero_score_is_kept_not_replaced_by_the_fallback():
    """0.0 is a real reading. Truthiness testing would discard it for the 5m score."""
    sig = _Sig(graded_regime="RANGING", graded_intel_score=0.0)

    _, norm = _resolve(sig, "TRENDING_UP", 0.80)

    assert norm == pytest.approx(normalize_intelligence_score(0.0))
    assert norm != pytest.approx(0.80), "a real 0.0 was mistaken for absent"


@pytest.mark.parametrize("missing", [None, "", "junk", [], {}, True, False])
def test_an_unusable_score_falls_back_to_the_five_minute_reading(missing):
    sig = _Sig(graded_regime="RANGING", graded_intel_score=missing)

    _, norm = _resolve(sig, "TRENDING_UP", 0.80)

    assert norm == pytest.approx(0.80)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_score_falls_back(bad):
    sig = _Sig(graded_regime="RANGING", graded_intel_score=bad)

    _, norm = _resolve(sig, "TRENDING_UP", 0.80)

    assert norm == pytest.approx(0.80)


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_blank_regime_falls_back_rather_than_stamping_empty(blank):
    """An empty market_regime would read as UNKNOWN downstream and change gating."""
    sig = _Sig(graded_regime=blank, graded_intel_score=15.0)

    regime, _ = _resolve(sig, "TRENDING_UP", 0.80)

    assert regime == "TRENDING_UP"


def test_a_missing_fallback_regime_is_unknown_not_empty():
    regime, _ = _resolve(_Sig(), "", 0.0)

    assert regime == "UNKNOWN"


# ── the stamp sites actually use it ──────────────────────────────────────────

def test_both_candidate_stamps_use_the_graded_context():
    """Pinned at source: the fix is two stamps, and both must stay wired.

    Stamping a 15-minute regime beside a 5-minute intelligence score would leave
    the candidate internally inconsistent, so neither site may regress alone.
    """
    src = inspect.getsource(SignalProcessor.process_best_signal)

    assert "_graded_candidate_context(" in src, "resolution helper not called"
    assert 'setattr(candidate, "market_regime", _stamp_regime)' in src
    assert 'setattr(candidate, "intelligence_score", float(_stamp_intel_norm))' in src
    assert 'setattr(candidate, "market_regime", str(regime or "UNKNOWN"))' not in src, (
        "the 5-minute regime is being stamped again"
    )


def test_the_helper_is_resolved_before_either_stamp():
    """Ordering guard — the assignment must dominate both uses."""
    src = inspect.getsource(SignalProcessor.process_best_signal)

    assign = src.index("_stamp_regime, _stamp_intel_norm = ")

    assert assign < src.index('setattr(candidate, "intelligence_score", float(_stamp_intel_norm))')
    assert assign < src.index('setattr(candidate, "market_regime", _stamp_regime)')
