"""The divergence vote must carry its own weight in the score average.

averaged_score = score / components. The divergence stress test contributes +/-0.30 —
LARGER than any counted pillar (regime is +/-0.25) — but it was the only term of that
size that never incremented `components`. Its influence therefore depended on how many
UNRELATED pillars happened to have data: measured +0.043 with seven pillars available
against +0.100 with three, a 2.3x swing driven by option-chain availability rather than
by anything in the market.
"""
import inspect

from intelligence.intelligence_aggregator import IntelligenceAggregator


def _analyze_source():
    return inspect.getsource(IntelligenceAggregator.analyze)


def test_the_divergence_vote_increments_the_component_count():
    src = _analyze_source()
    assert "if divergence_detected:" in src, "the divergence vote is uncounted again"
    # Slice to the end of the block: the next statement is the results assignment.
    start = src.index("if divergence_detected:")
    block = src[start:src.index('results["divergence_alert"]', start)]
    assert "components += 1" in block, (
        "divergence adds +/-0.30 to the numerator without adding to the denominator, so "
        "its weight moves with unrelated data availability"
    )


def test_every_term_at_least_as_large_as_regime_is_counted():
    """Guards the principle, not just this one site.

    Small context nudges (market structure +/-0.04, aroon +/-0.03) are deliberately
    uncounted: giving a 0.04 term the same denominator weight as a 0.25 pillar would
    over-correct. The rule being pinned is that a vote comparable to the LARGEST pillar
    must be counted.
    """
    src = _analyze_source()
    # The three big votes: regime 0.25, volume 0.20(+0.15), order flow 0.20, divergence 0.30.
    for marker in ("score -= 0.3", "score += 0.3"):
        assert marker in src, "the divergence weights changed; re-check this invariant"
    assert src.count("components += 1") >= 8, (
        "a large-weight vote appears to have lost its component increment"
    )
