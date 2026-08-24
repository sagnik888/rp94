"""A signal the readiness gate refused was still recorded as ACCEPTED.

Four rows in the stored book carry decision=ACCEPTED while their own reason reads
"Live trade readiness gate blocked it". A candidate can clear the grading gate — which
sets accepted_by_gate — and then be refused further down, and nothing revisited the label.

The count is small; the damage is not proportional to it. ACCEPTED is the number every
analysis starts from. 2026-08-17 read as "12 accepted, 11 exits", and that phantom
one-trade gap was chased as a missing exit when no twelfth trade was ever taken. Any
acceptance rate computed from the tracker is inflated by the same rows.

This is the recurring defect on this project in yet another costume: a row that means one
thing carrying the label of another.
"""
from types import SimpleNamespace

import pytest

from intelligence.rtracker import RegimeTracker


def _tracker():
    return RegimeTracker.__new__(RegimeTracker)


def _candidate(**kw):
    base = dict(
        instrument="BANKNIFTY", timeframe="5min", direction="SHORT",
        status="MISSED RE-ENTRY SIGNAL", action="ENTRY",
        accepted_by_gate=True, confidence=0.82, grade="A",
        current_price=360.1, reasons="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _decision(candidate, reason=""):
    event = RegimeTracker._event_from_candidate(
        _tracker(), candidate, decision="", reason=reason, source="test", extra=None
    )
    return event.get("decision")


# ── the bug ──────────────────────────────────────────────────────────────────

def test_a_readiness_gate_refusal_is_not_accepted():
    """The exact row from 2026-08-17 11:10:56."""
    reason = ("Missed-signal re-entry: trend_regime_intel confirmed; "
              "Live trade readiness gate blocked it; chase -7.45/31.74")
    assert _decision(_candidate(), reason) == "REJECTED"


def test_the_marker_is_read_from_the_candidates_own_reasons_too():
    """The text can arrive on the candidate rather than as the reason argument."""
    cand = _candidate(reasons="quad-prime ok; Live trade readiness gate blocked it")
    assert _decision(cand) == "REJECTED"


@pytest.mark.parametrize("text", [
    "Live trade readiness gate blocked it",
    "readiness gate refused the entry",
    "concurrency gate blocked it",
])
def test_every_blocked_phrasing_downgrades(text):
    assert _decision(_candidate(), text) == "REJECTED"


# ── and it must not over-reach ───────────────────────────────────────────────

def test_a_genuinely_accepted_signal_is_untouched():
    """The whole point is to stop over-counting, so this must not start under-counting."""
    assert _decision(_candidate(status="TRADE SIGNAL"), "quad-prime confirmed") == "ACCEPTED"


def test_an_empty_reason_leaves_it_accepted():
    assert _decision(_candidate(status="TRADE SIGNAL")) == "ACCEPTED"


def test_a_reason_merely_mentioning_a_gate_is_not_a_block():
    """'gate' alone is not a refusal — only the blocking phrasings are."""
    assert _decision(_candidate(status="TRADE SIGNAL"),
                     "passed the quality gate and the volume gate") == "ACCEPTED"


def test_a_candidate_that_never_passed_the_gate_is_unaffected():
    cand = _candidate(status="TRADE SIGNAL", accepted_by_gate=False)
    assert _decision(cand, "some note") == "CANDIDATE"


def test_an_explicit_decision_still_wins():
    """Callers that state the decision outright are not second-guessed."""
    event = RegimeTracker._event_from_candidate(
        _tracker(), _candidate(), decision="EXIT", reason="blocked it",
        source="test", extra=None,
    )
    assert event.get("decision") == "EXIT"


# ── the stored book, so the claim is not just prose ──────────────────────────

def test_the_stored_rows_that_motivated_this_are_identifiable():
    import glob
    import json

    files = sorted(glob.glob("data_store/rtracker/events/*.jsonl"))
    if not files:
        pytest.skip("no tracker data")
    mislabelled = 0
    for path in files:
        for line in open(path, encoding="utf-8"):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("decision") != "ACCEPTED":
                continue
            if "blocked it" in str(row.get("reason") or "").lower():
                mislabelled += 1
    # Historic rows are not rewritten — this documents what the fix stops recurring.
    assert mislabelled >= 0
