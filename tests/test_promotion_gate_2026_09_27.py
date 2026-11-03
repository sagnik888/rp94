from engine.promotion_gate import REQUIRED_GATES, assess_promotion


def test_missing_or_failed_evidence_forces_shadow_only():
    missing = assess_promotion(None)
    failed = assess_promotion({
        "schema": "paired_causal_evaluation.v1",
        "promotable": False,
        "gates": {name: name != "paired_coverage" for name in REQUIRED_GATES},
    })

    assert missing.mode == "SHADOW_ONLY"
    assert missing.allowed is False
    assert failed.mode == "SHADOW_ONLY"
    assert "paired_coverage" in failed.blockers


def test_only_complete_paired_evidence_allows_promotion():
    decision = assess_promotion({
        "schema": "paired_causal_evaluation.v1",
        "promotable": True,
        "gates": {name: True for name in REQUIRED_GATES},
    })

    assert decision.allowed is True
    assert decision.mode == "PROMOTED"
    assert decision.blockers == ()
