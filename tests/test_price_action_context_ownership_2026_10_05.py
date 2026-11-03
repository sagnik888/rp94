"""Deterministic interleaving between local decision and accepted evidence stamping."""
import pytest

import engine.signal_processor as sp
from tests.test_ut_smart_live_path_2026_09_26 import live


@pytest.mark.parametrize("local", [
    {"verdict": "FAVORABLE", "score": .4, "target_context": "NIFTY own target", "trailing_context": "NIFTY own trail"},
    {"verdict": "NEUTRAL", "score": -.1, "target_context": "NIFTY neutral target", "trailing_context": ""},
    None,
])
def test_accepted_candidate_uses_worker_local_price_action_even_if_shared_context_changes(live, monkeypatch, local):
    host = live()
    foreign = {"verdict": "ADVERSE", "score": -.99,
               "target_context": "SENSEX foreign target", "trailing_context": "SENSEX foreign trail"}
    host.sc._last_price_action_ctx = foreign
    monkeypatch.setattr(sp, "price_action_context", lambda *args: local)

    # Another worker publishes between this call's own price-action decision and
    # its final candidate stamp. This produces the race without thread timing.
    def interleaved_build(*args):
        host.sc._last_price_action_ctx = foreign
        return {}
    monkeypatch.setattr(sp, "build_identity", interleaved_build)
    candidate = host.processor.process_best_signal(*host.args)[0]
    assert candidate.status == "TRADE SIGNAL"
    assert getattr(candidate, "price_action_verdict", "") == (local["verdict"] if local else "")
    assert getattr(candidate, "price_action_score", 0.) == (local["score"] if local else 0.)
    assert all("SENSEX foreign" not in reason for reason in candidate.reasons)
    if local and local["target_context"]:
        assert any(local["target_context"] in reason for reason in candidate.reasons)

