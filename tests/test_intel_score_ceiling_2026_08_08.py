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

So the rare-burst third-trade permission could never be granted, strong intelligence could
never substitute for ADX in the "strong context" tests, and the top severity tier of
several exit ladders could never fire — while .env read as if all of it were tuned.

RESOLVED 2026-08-08. The operator chose Option A: rebase the thresholds onto the real
scale rather than rescale the score (which would have moved the intelligence number
everywhere at once, including on the dashboard). The five gates are now 0.107-0.134 —
see test_intel_gates_are_reachable_2026_08_08, which MEASURES the ceiling by driving the
real aggregator instead of bounding it statically.

This module keeps the detector honest: the static AST sum stays as a sanity bound, and
the preflight is exercised against a SYNTHETIC over-high gate rather than the live config,
so it no longer passes only while that config happens to be broken.
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

def test_the_static_bound_is_above_the_measured_ceiling():
    """The AST sum is a loose UPPER bound; the measured ceiling must sit below it.

    Superseded 2026-08-08. INTEL_SCORE_CEILING was originally this static bound (0.60).
    It is now the MEASURED maximum (0.18) from driving the real aggregator with maximal
    inputs — see test_intel_gates_are_reachable_2026_08_08, which re-measures it. The
    static sum is retained here only as a sanity ceiling on the ceiling.
    """
    total, components = _aggregator_delta_sum_and_components()
    assert components >= 5, "component count collapsed; re-check the ceiling"
    assert INTEL_SCORE_CEILING <= total / 5.0


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
    assert unreachable_intel_gates(_settings(rare_burst_min_intel_score=0.125)) == []


def test_every_known_gate_is_checked():
    s = _settings(**{name: 0.99 for name in INTEL_SCORE_GATES})
    assert len(unreachable_intel_gates(s)) == len(INTEL_SCORE_GATES)


def test_a_missing_or_junk_setting_is_ignored():
    assert unreachable_intel_gates(_settings()) == []
    assert unreachable_intel_gates(_settings(rare_burst_min_intel_score="high")) == []


# ── it reaches the operator ──────────────────────────────────────────────────

def test_the_preflight_surfaces_an_unreachable_gate(monkeypatch):
    """Drives a SYNTHETIC over-high gate.

    This used to read the live config, which meant it passed only while that config was
    broken. The gates were rebased on 2026-08-08 and it inverted — the same
    testing-the-config-not-the-code mistake already fixed once in this suite.
    """
    from config.settings import get_settings
    from engine.safe_mode import validate_real_mode_readiness

    s = get_settings()
    monkeypatch.setattr(s, "rare_burst_min_intel_score", 0.99, raising=False)
    text = " | ".join(validate_real_mode_readiness(s).get("warnings") or [])
    assert "can never be met" in text


def test_the_live_config_now_has_none():
    """The outcome of Option A: every gate is reachable on the real scale."""
    from config.settings import get_settings

    assert unreachable_intel_gates(get_settings()) == []
