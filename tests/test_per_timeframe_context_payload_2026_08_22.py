"""The intelligence bundle on a candidate must match the bundle that graded it."""

import inspect
from types import SimpleNamespace

from engine.signal_processor import GRADING_TELEMETRY_ATTRS, SignalProcessor


FIVE_MIN = {
    "aggregate": {"score": 72.0, "source": "5min"},
    "regime": {"regime": "TRENDING_UP", "source": "5min"},
    "volume": {"source": "5min"},
}
FIFTEEN_MIN = {
    "aggregate": {"score": -18.0, "source": "15min"},
    "regime": {"regime": "RANGING", "source": "15min"},
    "volume": {"source": "15min"},
}


def _resolve_like_grading_loop(timeframe_contexts):
    signal = SimpleNamespace()
    selected, _score, _regime = SignalProcessor._candidate_timeframe_context(
        "15min",
        FIVE_MIN,
        0.72,
        "TRENDING_UP",
        timeframe_contexts,
    )
    # This is the explicit grading→construction handoff in process_best_signal.
    signal.graded_intel_result = selected
    return SignalProcessor._graded_candidate_intel_result(signal, FIVE_MIN)


def test_flag_off_preserves_the_exact_five_minute_bundle_object():
    resolved = _resolve_like_grading_loop(None)

    assert resolved is FIVE_MIN
    assert resolved["aggregate"]["source"] == "5min"


def test_flag_on_propagates_the_candidates_own_timeframe_bundle():
    contexts = {
        "15min": {
            "intel_result": FIFTEEN_MIN,
            "intel_score": -0.18,
            "regime": "RANGING",
        }
    }
    resolved = _resolve_like_grading_loop(contexts)

    assert resolved is FIFTEEN_MIN
    assert resolved["aggregate"]["source"] == "15min"
    assert resolved["regime"]["regime"] == "RANGING"


def test_missing_or_invalid_handoff_falls_back_without_raising():
    assert SignalProcessor._graded_candidate_intel_result(
        SimpleNamespace(), FIVE_MIN
    ) is FIVE_MIN
    assert SignalProcessor._graded_candidate_intel_result(
        SimpleNamespace(graded_intel_result="bad"), FIVE_MIN
    ) is FIVE_MIN


def test_full_bundle_is_not_duplicated_into_decision_tape_telemetry():
    assert "graded_intel_result" not in GRADING_TELEMETRY_ATTRS


def test_both_candidate_context_packaging_sites_use_the_graded_bundle():
    src = inspect.getsource(SignalProcessor.process_best_signal)

    assert 'setattr(sig, "graded_intel_result", _tf_intel_result)' in src
    assert "rejected_intel_result = self._graded_candidate_intel_result(" in src
    assert "candidate_intel_result = self._graded_candidate_intel_result(" in src
    assert '"aggregate": dict(rejected_intel_result.get(' in src
    assert '"aggregate": dict(candidate_intel_result.get(' in src
