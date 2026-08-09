"""A neutral intelligence read was scored as full conviction.

`normalize_intelligence_score` inferred its input's SCALE from the magnitude:

    if abs(score) > 1.0:  score /= 100.0

The aggregator publishes `aggregate["score"]` on the legacy -100..+100 scale
(`final_score = score * 100.0`, `round(final_score, 1)`). So a published **0.9** --
which means 0.9% bullish, a genuinely NEUTRAL read -- sits inside the band the
heuristic reads as already-canonical. The divide is skipped and 0.9 is taken as 90%
conviction: a 100x inflation.

Downstream that is not cosmetic:

    intel_score_part = max(-0.20, min(0.30, aligned_intel * 0.30))

so 0.90 awards 0.27 of the 0.30 intelligence pillar, pushing confidence to ~0.794 and
the grade from C to A on evidence that was a coin flip. The same value clears the five
`abs(...) >= 0.70` "strong intelligence" gates in signal_quality_policy.

The band is narrow but reachable: round(final_score, 1) sends |score| < 0.0005 to
exactly 0.0 (which normalizes correctly), so the damage is |published| in [0.1, 1.0].

FIX: stop inferring. The aggregator now publishes `score_norm` on the canonical
-1..+1 scale alongside the legacy field, and aggregate readers call
`aggregate_intelligence_score`, which never guesses. The magnitude heuristic survives
only where the input is already canonical, and there it is a no-op.

Four indices, both directions: the aggregate is per-index and this system buys CE and
PE, so a maximally bearish read must mirror a bullish one on every index.
"""
import ast
import inspect
import textwrap

import pytest

from engine.intelligence_score import (
    LEGACY_SCORE_SCALE,
    aggregate_intelligence_score,
    normalize_intelligence_score,
)

INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]


# ── the headline: a neutral read is neutral ──────────────────────────────────

def test_a_neutral_published_score_is_not_full_conviction():
    """0.9 on the published scale is 0.9% bullish, not 90%."""
    got = aggregate_intelligence_score({"score": 0.9})
    assert got == pytest.approx(0.009), f"read 0.9% bullish as {got:.3f} conviction"


def test_the_old_heuristic_is_what_inflated_it():
    """The premise. If this ever stops being true the fix is arguing for nothing."""
    assert normalize_intelligence_score(0.9) == pytest.approx(0.90)


@pytest.mark.parametrize("published", [0.1, 0.5, 0.9, -0.1, -0.5, -1.0])
def test_every_value_in_the_ambiguous_band_reads_as_near_zero(published):
    """[0.1, 1.0] is exactly the band where the two scales overlap."""
    got = aggregate_intelligence_score({"score": published})
    assert abs(got) <= 0.01
    assert got == pytest.approx(published / LEGACY_SCORE_SCALE)


def test_the_grading_pillar_no_longer_awards_a_neutral_read():
    """max(-0.20, min(0.30, aligned * 0.30)) -- 0.27 of 0.30 was being handed out."""
    inflated = min(0.30, normalize_intelligence_score(0.9) * 0.30)
    correct = min(0.30, aggregate_intelligence_score({"score": 0.9}) * 0.30)
    assert inflated == pytest.approx(0.27)
    assert correct < 0.01, f"a neutral read still contributes {correct:.3f} to the grade"


@pytest.mark.parametrize("gate", [0.60, 0.65, 0.70, 0.75])
def test_a_neutral_read_clears_no_strong_intelligence_gate(gate):
    assert abs(aggregate_intelligence_score({"score": 0.9})) < gate


# ── real conviction still reads as real conviction ───────────────────────────

def test_a_genuinely_strong_read_is_undamaged():
    assert aggregate_intelligence_score({"score": 82.3}) == pytest.approx(0.823)


def test_the_ceiling_still_maps_to_one():
    assert aggregate_intelligence_score({"score": 100.0}) == pytest.approx(1.0)


@pytest.mark.parametrize("index", INDICES)
def test_bidirectional_symmetry_on_every_index(index):
    """Per-index payloads; a bought PE must mirror a bought CE exactly."""
    bull = aggregate_intelligence_score({"score": 82.3, "instrument": index})
    bear = aggregate_intelligence_score({"score": -82.3, "instrument": index})
    assert bull == pytest.approx(-bear)
    assert bull == pytest.approx(0.823)


# ── score_norm is preferred, and legacy payloads still work ──────────────────

def test_the_canonical_field_wins_over_the_legacy_one():
    got = aggregate_intelligence_score({"score": 0.9, "score_norm": 0.009})
    assert got == pytest.approx(0.009)


def test_a_payload_predating_score_norm_still_lands_on_the_right_scale():
    """Persisted intelligence memory and rtracker archives have no score_norm."""
    assert aggregate_intelligence_score({"score": 45.0}) == pytest.approx(0.45)


def test_the_aggregator_publishes_the_canonical_field():
    import intelligence.intelligence_aggregator as agg_mod

    src = inspect.getsource(agg_mod.IntelligenceAggregator.analyze)
    assert '"score_norm"' in src, "the producer still publishes only the legacy scale"


def test_the_two_published_fields_agree():
    """score_norm must be the SAME conviction, not an independently drifting number."""
    import numpy as np
    import pandas as pd
    from types import SimpleNamespace

    from intelligence.intelligence_aggregator import IntelligenceAggregator

    bars = 90
    idx = pd.date_range("2026-08-05 09:15", periods=bars, freq="5min")
    c = 24000.0 + np.arange(bars) * 8.0
    frame = pd.DataFrame(
        {"open": c - 2, "high": c + 6, "low": c - 6, "close": c,
         "volume": np.linspace(3e5, 9e5, bars)}, index=idx,
    )
    agg = IntelligenceAggregator()
    agg.regime = SimpleNamespace(detect=lambda *a, **k: {
        "regime": "TRENDING_UP", "confidence": 95, "adx": 42.0})
    out = agg.analyze(instrument="NIFTY", timeframe="5min", candle_df=frame,
                      candle_1min_df=frame, options_chain=None,
                      spot_price=24720.0, strike_interval=50,
                      price_change_pct=2.5)["aggregate"]
    assert "score_norm" in out
    # Exact, not approximate: the legacy field IS score_norm rounded to 1dp of the
    # x100 scale. It therefore cannot agree closer than half a step (5e-4 canonical),
    # which is precisely why score_norm exists -- it is the finer of the two.
    assert round(out["score_norm"] * LEGACY_SCORE_SCALE, 1) == out["score"]
    assert out["score_norm"] == pytest.approx(out["score"] / LEGACY_SCORE_SCALE, abs=1e-3)


# ── the wiring: no aggregate reader may guess ────────────────────────────────

def _guessing_reads(path, fn_names=None):
    """Calls of normalize_intelligence_score whose argument mentions an aggregate."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if fname != "normalize_intelligence_score" or not node.args:
            continue
        arg = ast.dump(node.args[0])
        if "aggregate" in arg or "'score'" in arg or '"score"' in arg or "agg" in arg:
            bad.append(node.lineno)
    return bad


@pytest.mark.parametrize("path", [
    "scanner.py", "engine/pre_candle_predictor.py", "engine/signal_processor.py",
])
def test_no_aggregate_payload_is_read_through_the_magnitude_guess(path):
    bad = _guessing_reads(path)
    assert not bad, (
        f"{path} lines {bad} still infer the scale from an aggregate score; "
        "use aggregate_intelligence_score"
    )


def test_the_canonical_reader_is_actually_used():
    src = open("scanner.py", encoding="utf-8").read()
    assert src.count("aggregate_intelligence_score(") >= 6


def test_the_magnitude_heuristic_survives_for_canonical_inputs():
    """scanner.py:16799 passes seed.intelligence_score, which is ALREADY -1..+1.
    Banning the heuristic outright would have been the wrong fix."""
    from scanner import Scanner

    src = inspect.getsource(Scanner)
    assert "normalize_intelligence_score(getattr(seed" in src


def test_normalize_is_idempotent_on_a_canonical_value():
    for v in (0.0, 0.35, -0.82, 1.0, -1.0):
        assert normalize_intelligence_score(normalize_intelligence_score(v)) == pytest.approx(v)


# ── it cannot raise on the hot path ──────────────────────────────────────────

@pytest.mark.parametrize("junk", [None, {}, [], "nope", {"score": "x"}, {"score": None},
                                  {"score_norm": "x"}, {"score": float("nan")},
                                  {"score_norm": float("nan")}])
def test_junk_returns_zero_rather_than_raising(junk):
    assert aggregate_intelligence_score(junk) == 0.0


def test_an_out_of_contract_value_is_still_clamped():
    assert aggregate_intelligence_score({"score": 5000.0}) == 1.0
    assert aggregate_intelligence_score({"score_norm": -9.0}) == -1.0
