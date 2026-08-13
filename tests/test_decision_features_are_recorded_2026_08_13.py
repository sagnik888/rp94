"""A gate cannot be tuned from an archive that never recorded what it judged.

Measured across the whole 66,027-row archive:

    confidence            100.0%
    regime.adx              5.1%
    volume.buy_sell_ratio   4.7%
    breadth.aligned_count   2.5%
    aggregate.score_norm    1.0%
    volume.volume_ratio     0.0%   (19 rows out of 66,027)

and entry_intel is present on 19 rows, all EXITs — 0 of 65,423 REJECTED rows.

So every threshold in this system (volume ratio floors, ADX ceilings, intel
alignment, PCR votes) is argued from an archive that does not contain the
readings those thresholds were compared against. The rejected population is
exactly the one gate-tuning needs — "what did the gate turn away, and was it
right" is unanswerable without it.

_entry_intel_snapshot already captures the right block. It runs only at
acceptance, and deliberately so: entry_intel means "the intel that existed when
this entry was ACCEPTED", and its own test pins that it is taken once. So the
decision-time block is recorded separately as `decision_intel`, leaving that
contract untouched.

Observation only. Nothing reads decision_intel to make a decision — same rule
entry_intel follows.
"""
from types import SimpleNamespace

import pytest

from scanner import Scanner


def _sc(intel=None):
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc._cached_timeframe_intel = lambda *a, **k: intel if intel is not None else {
        "volume": {"volume_ratio": 2.4, "buy_sell_ratio": 1.23},
        "regime": {"regime": "RANGING", "adx": 18.2, "kaufman_er": 0.34,
                   "atr_pct": 0.31, "bb_width": 0.9, "confidence": 71.0},
        "pcr": {"pcr": 1.337},
        "order_flow": {"ratio": 0.84},
        "aggregate": {"score": -3.2},
        "breadth_context": {"aligned_count": 3, "bias": "BEARISH"},
        "session_context": {"day_type": "RANGE_DAY", "confidence": 0.81},
    }
    sc._premium_cache = {}
    sc._vix_percentile = lambda v, t: 41.2
    recorded = []
    sc.rtracker = SimpleNamespace(
        record_candidate=lambda cand, **kw: recorded.append((cand, kw))
    )
    sc._recorded = recorded
    return sc


def _cand(**kw):
    base = dict(instrument="NIFTY", timeframe="5min", option_type="CE",
                direction="LONG", grade="B", confidence=0.41)
    base.update(kw)
    return SimpleNamespace(**base)


def _record(sc, cand, decision="REJECTED"):
    sc._rtracker_record_candidate(cand, decision=decision, reason="Choppy market blocked")
    return sc._recorded[0][0]


# ── the rejected population now carries what the gate judged ─────────────────

def test_a_rejected_signal_records_the_features_it_was_judged_on():
    sc = _sc()

    cand = _record(sc, _cand())

    block = getattr(cand, "decision_intel", None)
    assert isinstance(block, dict) and block
    assert block["volume_ratio"] == pytest.approx(2.4)
    assert block["adx"] == pytest.approx(18.2)
    assert block["pcr"] == pytest.approx(1.337)
    assert block["order_ratio"] == pytest.approx(0.84)


@pytest.mark.parametrize("decision", ["REJECTED", "STABILIZED", "ACCEPTED", "EXIT"])
def test_every_decision_type_carries_it(decision):
    sc = _sc()

    cand = _record(sc, _cand(), decision=decision)

    assert isinstance(getattr(cand, "decision_intel", None), dict)


def test_it_does_not_disturb_the_entry_intel_contract():
    """entry_intel means "intel at ACCEPTANCE" and is taken once; a rejection
    recorded earlier must not pre-fill or overwrite it."""
    sc = _sc()

    cand = _record(sc, _cand())

    assert getattr(cand, "entry_intel", None) is None


def test_an_existing_block_is_not_overwritten():
    """One decision, one reading: later gates speaking about the same candidate
    must not restamp it with a later market."""
    sc = _sc()
    cand = _cand()
    cand.decision_intel = {"volume_ratio": 9.9}

    sc._rtracker_record_candidate(cand, decision="REJECTED", reason="x")

    assert cand.decision_intel["volume_ratio"] == pytest.approx(9.9)


# ── it can never cost a decision ─────────────────────────────────────────────

def test_a_failing_snapshot_never_blocks_the_record():
    sc = _sc()

    def boom(*a, **k):
        raise RuntimeError("intel down")

    sc._cached_timeframe_intel = boom

    sc._rtracker_record_candidate(_cand(), decision="REJECTED", reason="x")

    assert len(sc._recorded) == 1, "the row must still be written"


def test_absent_intel_records_no_block_rather_than_zeros():
    """Absent is not zero — the defect this archive already has in four places."""
    sc = _sc(intel={})

    cand = _record(sc, _cand())

    block = getattr(cand, "decision_intel", None) or {}
    assert "volume_ratio" not in block
    assert "adx" not in block


def test_nothing_consumes_the_block_for_a_decision():
    import inspect

    src = inspect.getsource(Scanner)
    reads = [
        ln for ln in src.splitlines()
        if "decision_intel" in ln and not ln.strip().startswith("#")
    ]
    for line in reads:
        assert ("setattr" in line or "getattr" in line), (
            f"decision_intel is being read for a decision: {line.strip()}"
        )
