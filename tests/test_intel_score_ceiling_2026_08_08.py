"""The unreachable-gate detector.

HISTORY. This file was written when the aggregate score was a MEAN of votes, which
capped it at 0.29 on thin evidence and 0.178 with an option chain while five gates tested
it against 0.60-0.75. All five were dead, and so was the aggregator's own 0.3/0.6
BUY/STRONG_BUY, leaving aggregate.signal permanently HOLD.

That was fixed at the root on 2026-08-08 by weight-normalising the score (Option B, chosen
over rebasing the thresholds after measuring both). The gates went back to their original
0.60-0.75 and are reachable again — see test_intel_gates_are_reachable_2026_08_08 for the
A-vs-B measurement and the live re-measurement of the ceiling.

What remains worth testing here is the DETECTOR: a threshold set above what the score can
reach is a gate that never fires while reading, in .env, as though it were tuned. The
mean-scale arithmetic tests are gone because the premise they rested on is.
"""
import ast

import pytest

from engine.safe_mode import (
    INTEL_SCORE_CEILING,
    INTEL_SCORE_GATES,
    unreachable_intel_gates,
)


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
    """0.95 is above the 0.823 a fully-evidenced maximal read reaches."""
    found = unreachable_intel_gates(_settings(rare_burst_min_intel_score=0.95))
    assert found == [("rare_burst_min_intel_score", 0.95)]


def test_a_reachable_gate_is_not_reported():
    assert unreachable_intel_gates(_settings(rare_burst_min_intel_score=0.70)) == []


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
    """The outcome of Option B: every gate is reachable on the normalised scale."""
    from config.settings import get_settings

    assert unreachable_intel_gates(get_settings()) == []
