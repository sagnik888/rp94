"""Of RR, target, exit and stop, only the STOP ignored the intelligence score.

_dynamic_rr(grade, confidence, intel_score, timeframe) drives the risk-reward, and the
target and exit follow from it. The option stop moved with regime and ATR alone — the same
corroborating evidence that widened the target had no say in how much room the trade got
to be right.

Above the neutral 50 the stop widens slightly; below it, it tightens. The shift is capped
so a stop already chosen by ATR and the per-timeframe rules is nudged, never redefined.

DEFAULT OFF. This changes the risk taken on every option entry, which is a strategy
decision and not a defect being repaired — so it does nothing until switched on.
"""
from types import SimpleNamespace

import pytest

from engine.signal_processor import _intelligence_stop_pct


def _s(enabled=True, shift=20.0):
    return SimpleNamespace(
        dynamic_stop_from_intelligence=enabled,
        dynamic_stop_intel_max_shift_pct=shift,
    )


# ── off by default ───────────────────────────────────────────────────────────

def test_it_does_nothing_unless_switched_on():
    assert _intelligence_stop_pct(15.0, 90.0, _s(enabled=False)) == 15.0


def test_a_settings_object_without_the_flag_is_unchanged():
    assert _intelligence_stop_pct(15.0, 90.0, SimpleNamespace()) == 15.0


def test_a_string_true_from_env_is_honoured():
    """.env values arrive as strings."""
    assert _intelligence_stop_pct(15.0, 90.0, _s(enabled="True")) != 15.0


# ── the behaviour ────────────────────────────────────────────────────────────

def test_strong_evidence_widens_the_stop():
    """Conviction earns room to be right."""
    assert _intelligence_stop_pct(15.0, 100.0, _s()) == pytest.approx(18.0)


def test_weak_evidence_tightens_the_stop():
    assert _intelligence_stop_pct(15.0, 0.1, _s()) < 15.0


def test_a_neutral_reading_changes_nothing():
    assert _intelligence_stop_pct(15.0, 50.0, _s()) == pytest.approx(15.0)


def test_the_shift_is_capped_at_the_configured_bound():
    """20% of a 15% stop is 3 points, whatever the score."""
    assert _intelligence_stop_pct(15.0, 100.0, _s(shift=20.0)) == pytest.approx(18.0)
    assert _intelligence_stop_pct(15.0, 100.0, _s(shift=5.0)) == pytest.approx(15.75)


def test_the_cap_itself_is_bounded():
    """A runaway setting must not hand over an unbounded stop."""
    assert _intelligence_stop_pct(15.0, 100.0, _s(shift=500.0)) <= 15.0 * 2.0


def test_a_zero_cap_disables_the_adjustment():
    assert _intelligence_stop_pct(15.0, 100.0, _s(shift=0.0)) == 15.0


# ── refusing to act on nothing ───────────────────────────────────────────────

def test_no_intelligence_reading_leaves_the_stop_alone():
    """A missing score is not evidence of weakness — it is no evidence."""
    assert _intelligence_stop_pct(15.0, 0.0, _s()) == 15.0
    assert _intelligence_stop_pct(15.0, None, _s()) == 15.0


def test_a_junk_score_does_not_move_the_stop():
    assert _intelligence_stop_pct(15.0, "strong", _s()) == 15.0


def test_a_junk_base_is_returned_unchanged():
    assert _intelligence_stop_pct(0.0, 90.0, _s()) == 0.0


def test_the_result_never_goes_below_a_usable_floor():
    assert _intelligence_stop_pct(0.6, 0.1, _s(shift=50.0)) >= 0.5


# ── the wiring, not just the helper ──────────────────────────────────────────

def test_the_option_stop_path_passes_the_intel_score():
    """Every other fix this session that skipped this check was wired to nothing."""
    import ast
    import inspect
    import textwrap

    from engine.signal_processor import SignalProcessor

    src = inspect.getsource(SignalProcessor._option_premium_stop_distance)
    assert "intel_score" in src, "the option stop still cannot see the intelligence score"
    assert "_intelligence_stop_pct" in src


def test_the_caller_actually_supplies_it():
    """Parsed, not windowed: the call is deeply indented and spans more characters than
    a fixed-width slice around the name covers."""
    import ast
    import inspect
    import textwrap

    from engine.signal_processor import SignalProcessor

    tree = ast.parse(textwrap.dedent(inspect.getsource(SignalProcessor)))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_option_premium_stop_distance"
    ]
    assert calls, "the option stop is no longer computed through that helper"
    for call in calls:
        assert any(k.arg == "intel_score" for k in call.keywords), (
            "the stop helper accepts an intel score and this entry path never passes one"
        )
