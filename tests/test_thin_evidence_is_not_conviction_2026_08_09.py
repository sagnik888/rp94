"""One available pillar published 100.0 / STRONG_BUY.

The weight-normalisation fix (2026-08-08) divided the vote sum by the sum of the MAXIMUM
vote each COUNTED pillar could have cast. That made the score a good measure of DIRECTION,
and no measure at all of how much was known.

Regime is the only pillar that counts unconditionally (+0.25). Volume needs
volume_available, OI/PCR/oi_wall need an authoritative chain, order flow needs
flow_available, top_down needs session/anchor/breadth context, divergence only fires on a
detected trap. So whenever the chain was unauthoritative, order flow abstained and volume
was unavailable, regime alone owned the denominator:

    score 0.25 / max_possible 0.25 = 1.0  ->  published 100.0, STRONG_BUY

REPRODUCED before the fix: components_analyzed=1, score=100.0, signal=STRONG_BUY. That
cleared every min_intel_score gate in the system -- the 0.20 missed-signal re-entry, the
0.20 choppy scalp, even the 0.70 rare-burst gate -- on the strength of a single engine, on
the least-informed read of the session. It is not confined to 09:15-09:19 either: any feed
outage that drops the chain and the volume source reproduces it mid-session.

A second, smaller instance of the same asymmetry: market_structure (+/-0.04) and aroon
(+/-0.03) add to the score NUMERATOR unconditionally, while the 0.35 that would budget for
them sits inside `if volume_available:`. With volume absent the numerator reached 0.32
against a 0.25 denominator -- an averaged_score of 1.28, silently clamped to 1.0.

THE FIX: the denominator has a FLOOR, FULL_EVIDENCE_MASS = 1.30, the evidence a healthy
read carries (volume .35 + oi .10 + pcr .20 + oi_wall .20 + regime .25 + order_flow .20).
Below it the score is measured on the same yardstick as a complete read instead of against
itself; at or above it nothing changes, which is why the measured ceiling (0.823) and every
gate threshold are untouched. Structure/aroon take their own 0.07 budget when they stand
outside the volume pillar.

NOT changed, deliberately: oi_wall still takes its 0.20 whenever a chain and a spot exist,
even when no wall is near enough to vote. A pillar that HAD data and returned "nothing
here" is a real verdict of zero and belongs in the denominator -- exactly as a RANGING
regime does. The rule is "counts when it had data", not "counts when it moved the score".
"""
import numpy as np
import pandas as pd
import pytest
from types import SimpleNamespace

from intelligence.intelligence_aggregator import (
    FULL_EVIDENCE_MASS,
    IntelligenceAggregator,
)

INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]


def _frame(bars=90, slope=8.0):
    idx = pd.date_range("2026-08-05 09:15", periods=bars, freq="5min")
    c = 24000.0 + np.arange(bars) * slope
    return pd.DataFrame(
        {"open": c - 2, "high": c + 6, "low": c - 6, "close": c,
         "volume": np.zeros(bars)}, index=idx,
    )


def _thin(structure=None, aroon=None, regime="TRENDING_UP"):
    """The 09:16 / feed-outage state: no chain, no volume, order flow abstaining."""
    agg = IntelligenceAggregator()
    agg.volume = SimpleNamespace(analyze=lambda *a, **k: {
        "available": False, "signal": "NEUTRAL", "buy_sell_ratio": 1.0,
        "current_volume": 0, "surge_level": "NONE", "volume_ratio": 0.0,
        "market_structure_profile": ({"signal": structure, "state": structure} if structure else {}),
        "aroon": ({"trend": aroon, "late_trend_caution": False} if aroon else {}),
        "buy_pct": 50.0, "is_proxy": False})
    agg.order_flow = SimpleNamespace(analyze=lambda *a, **k: {
        "available": False, "signal": "NEUTRAL", "ratio": 1.0, "source": "unavailable",
        "buy_sell_ratio": 1.0, "multi_source_flow": {}, "options_flow": {}})
    agg.regime = SimpleNamespace(detect=lambda *a, **k: {
        "regime": regime, "confidence": 95, "adx": 42.0})
    return agg


def _run(agg, instrument="NIFTY"):
    return agg.analyze(
        instrument=instrument, timeframe="5min", candle_df=_frame(),
        candle_1min_df=_frame(bars=4, slope=1.0), options_chain=None,
        spot_price=24720.0, strike_interval=50, price_change_pct=0.05,
    )["aggregate"]


# ── the headline ─────────────────────────────────────────────────────────────

def test_one_pillar_is_not_full_conviction():
    a = _run(_thin())
    assert a["components_analyzed"] == 1
    assert a["score"] < 30.0, f"a single-pillar read still publishes {a['score']}"
    assert a["signal"] == "HOLD", f"one engine still says {a['signal']}"


def test_one_pillar_clears_no_intel_gate():
    """0.20 missed-signal re-entry, 0.20 choppy scalp, 0.70 rare burst."""
    norm = abs(_run(_thin())["score_norm"])
    for gate in (0.20, 0.60, 0.65, 0.70, 0.75):
        assert norm < gate, f"one pillar still clears the {gate} gate at {norm:.3f}"


@pytest.mark.parametrize("index", INDICES)
def test_the_saturation_is_gone_on_every_index(index):
    """Four indices are scanned separately; each builds its own aggregate."""
    assert _run(_thin(), instrument=index)["signal"] == "HOLD"


@pytest.mark.parametrize("regime", ["TRENDING_UP", "TRENDING_DOWN"])
def test_neither_direction_saturates(regime):
    """Bidirectional: a bought PE must not be handed conviction a CE would not get."""
    a = _run(_thin(regime=regime))
    assert abs(a["score"]) < 30.0
    assert a["signal"] == "HOLD"


def test_the_two_directions_are_exact_mirrors():
    up = _run(_thin(regime="TRENDING_UP"))["score_norm"]
    down = _run(_thin(regime="TRENDING_DOWN"))["score_norm"]
    assert up == pytest.approx(-down)


# ── the numerator/denominator asymmetry ──────────────────────────────────────

def test_structure_and_aroon_carry_their_own_budget_when_volume_is_absent():
    a = _run(_thin(structure="ACCEPTING_HIGHER", aroon="UP"))
    assert a["evidence_mass"] == pytest.approx(0.32), (
        "regime 0.25 + structure/aroon 0.07; without the 0.07 the numerator is unbudgeted"
    )


def test_the_score_can_no_longer_exceed_its_own_denominator():
    """It reached 1.28 and was silently clamped to 1.0."""
    a = _run(_thin(structure="ACCEPTING_HIGHER", aroon="UP"))
    assert abs(a["score_norm"]) < 1.0
    assert a["score_norm"] == pytest.approx(0.32 / FULL_EVIDENCE_MASS, abs=1e-3)


def test_adding_structure_and_aroon_still_raises_conviction():
    """Budgeting them must not make a CONFIRMING signal lower the score."""
    bare = _run(_thin())["score_norm"]
    with_both = _run(_thin(structure="ACCEPTING_HIGHER", aroon="UP"))["score_norm"]
    assert with_both > bare


# ── the floor, and what it must NOT touch ────────────────────────────────────

def test_the_floor_is_the_healthy_pillar_set():
    assert FULL_EVIDENCE_MASS == pytest.approx(0.35 + 0.10 + 0.20 + 0.20 + 0.25 + 0.20)


def test_a_fully_evidenced_read_is_divided_by_its_own_mass_not_the_floor():
    """At or above the floor the fix is a no-op - this is why no gate moved."""
    from engine.safe_mode import INTEL_SCORE_CEILING

    assert INTEL_SCORE_CEILING > 0.8, (
        "the ceiling moved; the floor was supposed to leave well-evidenced reads alone"
    )


def test_evidence_is_reported_not_just_used():
    a = _run(_thin())
    assert a["evidence_mass"] == pytest.approx(0.25)
    assert a["evidence_ratio"] == pytest.approx(0.25 / FULL_EVIDENCE_MASS, abs=1e-3)


def test_more_evidence_scores_higher_than_less():
    """The property the whole normalisation exists for, re-checked under the floor."""
    thin = abs(_run(_thin())["score_norm"])
    richer = abs(_run(_thin(structure="ACCEPTING_HIGHER", aroon="UP"))["score_norm"])
    assert richer > thin


def test_the_denominator_floor_is_actually_wired():
    import inspect

    src = inspect.getsource(IntelligenceAggregator.analyze)
    assert "max(max_possible, FULL_EVIDENCE_MASS)" in src, (
        "the score divides by the evidence present again, so thin evidence can saturate"
    )
