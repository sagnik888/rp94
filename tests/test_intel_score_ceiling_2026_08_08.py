"""Five intelligence gates are set above what the score can physically reach.

IntelligenceAggregator publishes `averaged_score = sum(votes) / components`, clamps it to
[-1, 1] and scales by 100. Every consumer runs `normalize_intelligence_score`, which
divides by 100 again — so the number the gates test is that MEAN of votes.

Summing ALL twenty literal `score +=` deltas in the aggregator gives 3.00, and that is
already a wild over-count because most sit in mutually exclusive if/elif arms. Divided by
the component count:

    5 components -> 0.600   (an absolute, unreachable ceiling)
    8 components -> 0.375   (there are 8 `components += 1` sites)

against these configured gates:

    rare_burst_min_intel_score                      0.70   (.env:357)
    live_15min_trending_momentum_intel_threshold    0.60
    live_15min_momentum_intel_threshold             0.65
    live_5min_special_intel_threshold               0.75
    volume_spike_entry_strong_1min_intel_threshold  0.70

So the rare-burst third-trade permission can never be granted, strong intelligence can
never substitute for ADX in the "strong context" tests, and the top severity tier of
several exit ladders can never fire — while .env reads as if all of it were tuned.

Re-scaling the score, or re-basing the thresholds, is a CALIBRATION decision for the
operator: either one changes live trading. This module only makes the dead gates audible
at startup, and pins the arithmetic so the constant cannot drift.
"""
import ast

import pytest

from engine.safe_mode import (
    INTEL_SCORE_CEILING,
    INTEL_SCORE_GATES,
    unreachable_intel_gates,
)


def _aggregator_delta_sum_and_components():
    """Re-derive the ceiling inputs from the aggregator's own source."""
    src = open("intelligence/intelligence_aggregator.py", encoding="utf-8").read()
    tree = ast.parse(src)
    total, components = 0.0, 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name)):
            continue
        if node.target.id == "score" and isinstance(node.value, ast.Constant):
            total += abs(float(node.value.value))
        if node.target.id == "components" and isinstance(node.value, ast.Constant):
            components += 1
    return total, components


# ── the arithmetic the constant rests on ─────────────────────────────────────

def test_the_ceiling_matches_the_aggregator_source():
    """If the aggregator's votes change, this constant must be re-derived."""
    total, components = _aggregator_delta_sum_and_components()
    assert components >= 5, "component count collapsed; re-check the ceiling"
    generous = total / 5.0
    assert generous == pytest.approx(INTEL_SCORE_CEILING, abs=0.05), (
        f"sum of all deltas {total:.2f} over 5 components is {generous:.3f}, but "
        f"INTEL_SCORE_CEILING says {INTEL_SCORE_CEILING}"
    )


def test_the_realistic_ceiling_is_far_below_the_generous_one():
    total, components = _aggregator_delta_sum_and_components()
    assert total / components < 0.40


def test_no_single_vote_can_carry_a_gate_alone():
    """The largest single delta is well under every configured threshold."""
    src = open("intelligence/intelligence_aggregator.py", encoding="utf-8").read()
    tree = ast.parse(src)
    biggest = max(
        abs(float(n.value.value))
        for n in ast.walk(tree)
        if isinstance(n, ast.AugAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "score"
        and isinstance(n.value, ast.Constant)
    )
    assert biggest <= 0.35


# ── the detector ─────────────────────────────────────────────────────────────

def _settings(**kw):
    from types import SimpleNamespace

    return SimpleNamespace(**kw)


def test_an_unreachable_gate_is_reported():
    found = unreachable_intel_gates(_settings(rare_burst_min_intel_score=0.70))
    assert found == [("rare_burst_min_intel_score", 0.70)]


def test_a_reachable_gate_is_not_reported():
    assert unreachable_intel_gates(_settings(rare_burst_min_intel_score=0.25)) == []


def test_every_known_gate_is_checked():
    s = _settings(**{name: 0.99 for name in INTEL_SCORE_GATES})
    assert len(unreachable_intel_gates(s)) == len(INTEL_SCORE_GATES)


def test_a_missing_or_junk_setting_is_ignored():
    assert unreachable_intel_gates(_settings()) == []
    assert unreachable_intel_gates(_settings(rare_burst_min_intel_score="high")) == []


# ── it reaches the operator ──────────────────────────────────────────────────

def test_the_preflight_surfaces_it():
    from config.settings import get_settings
    from engine.safe_mode import validate_real_mode_readiness

    text = " | ".join(validate_real_mode_readiness(get_settings()).get("warnings") or [])
    assert "can never be met" in text, (
        "an unreachable intelligence gate is not reported at startup"
    )


def test_the_live_config_really_has_one():
    """Guards the premise: if the operator re-bases these, this test should change."""
    from config.settings import get_settings

    assert unreachable_intel_gates(get_settings()), (
        "no unreachable gate in the live config — re-read this module's reasoning"
    )
