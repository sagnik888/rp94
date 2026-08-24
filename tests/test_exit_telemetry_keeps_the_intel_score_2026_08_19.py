"""An EXIT record that says "intelligence was zero" invites the wrong conclusion.

2026-08-19. Reading only EXIT events, every trade of the session showed
`grading.intel_score == 0.0`, which reads as "the 30%-weight intelligence pillar is
dead". Joined against the ACCEPTED event for the SAME lifecycle, 13 of 14 held a real
value:

    inst        tf     entry                 ACCEPTED     EXIT
    SENSEX      5min   2026-08-19T10:23:14    -0.2505   +0.0000
    MIDCPNIFTY  5min   2026-08-19T10:56:24    +0.1394   +0.0000
    NIFTY       5min   2026-08-19T12:04:52    -0.3935   +0.0000
    MIDCPNIFTY  5min   2026-08-19T12:40:37    +0.2667   +0.0000

Grading was never wrong. On the ACCEPTED events `grading.intel_score` matches
`aggregate.score_norm` to four decimals (-0.2962 = -0.2962, -0.1145 = -0.1145), and the
pillar tracks the direction-aligned composite at corr +0.997.

The cause is which OBJECT each event is built from. `graded_intel_score` is stamped on
the CANDIDATE during grading; an EXIT event is built from the Trade, which never carries
that attribute, so `_candidate_value(..., default=0.0)` returned the default and wrote a
confident zero.

Nothing about trading changes here. This is an archive-honesty fix: a stored zero that
means "not present" is indistinguishable from one that means "measured neutral", and
that ambiguity produced a false diagnosis of the intelligence pillar.
"""
from types import SimpleNamespace

import pytest

from intelligence.rtracker import _candidate_value, _safe_float


def _resolved(candidate):
    """The expression the exit record now uses for grading.intel_score."""
    return _safe_float(
        _candidate_value(
            candidate,
            "graded_intel_score",
            _candidate_value(candidate, "intelligence_score", 0.0),
        )
    )


# ── the candidate path is unchanged ──────────────────────────────────────────

def test_a_candidate_still_reports_the_score_grading_used():
    """graded_intel_score wins whenever it exists — that IS what graded the signal."""
    cand = SimpleNamespace(graded_intel_score=-0.2962, intelligence_score=0.1234)
    assert _resolved(cand) == pytest.approx(-0.2962)


def test_a_genuine_zero_from_grading_is_preserved():
    """A measured neutral must survive; only ABSENCE falls back."""
    cand = SimpleNamespace(graded_intel_score=0.0, intelligence_score=0.44)
    assert _resolved(cand) == 0.0


# ── the exit path is what was broken ─────────────────────────────────────────

@pytest.mark.parametrize("recorded", [-0.2505, 0.1394, -0.3935, 0.2667, 0.0155, -0.0097])
def test_a_trade_falls_back_to_its_own_intelligence_score(recorded):
    """The 2026-08-19 values, each on a Trade-shaped object with no graded_intel_score."""
    trade = SimpleNamespace(instrument="NIFTY", intelligence_score=recorded)
    assert not hasattr(trade, "graded_intel_score")
    assert _resolved(trade) == pytest.approx(recorded)


def test_an_object_carrying_neither_is_still_zero():
    """No score anywhere is honestly zero, and must not raise."""
    assert _resolved(SimpleNamespace(instrument="NIFTY")) == 0.0


def test_dict_shaped_records_work_too():
    """_candidate_value accepts dicts; exits are sometimes built from them."""
    assert _resolved({"intelligence_score": -0.3164}) == pytest.approx(-0.3164)
    assert _resolved({"graded_intel_score": 0.5, "intelligence_score": -0.3}) == 0.5


def test_a_junk_value_degrades_to_zero_rather_than_raising():
    """Telemetry must never take down an exit."""
    assert _resolved(SimpleNamespace(intelligence_score="not-a-number")) == 0.0
    assert _resolved(SimpleNamespace(intelligence_score=None)) == 0.0


# ── wiring ───────────────────────────────────────────────────────────────────

def test_the_exit_record_actually_uses_the_fallback():
    """A fallback nothing calls leaves the archive exactly as misleading as before."""
    import inspect

    import intelligence.rtracker as rt

    src = inspect.getsource(rt)
    assert '"graded_intel_score",' in src
    idx = src.index('"graded_intel_score",')
    window = src[idx:idx + 240]
    assert "intelligence_score" in window, (
        "graded_intel_score is still read with a bare 0.0 default, so every exit "
        "record will keep reporting a confident zero"
    )
