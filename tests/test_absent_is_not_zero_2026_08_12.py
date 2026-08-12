"""`x.get(a) or x.get(b) or default` reads a real 0.0 as absent.

Order flow is a buy/sell ratio: 0.0 is not "no reading", it is every trade on the
sell side — the most bearish value the pillar can produce. The idiom turned that
into the neutral default 1.0 and dropped its vote, so the one reading that most
deserved to count was the one silently discarded.

The same conflation runs the other way for PCR, where 0.0 IS the codebase's own
sentinel for an absent chain (kite_market_data.put_call_ratio of an empty frame
returns 0.0). Absent and zero therefore have to be told apart per pillar, not by a
shared falsy default.

_peak_threat_exit_decision already states the rule this file enforces:
"'Unclear' is not 'against'".
"""
from types import SimpleNamespace

import pytest

from scanner import Scanner


def _sc():
    return Scanner.__new__(Scanner)


# ── the resolver ─────────────────────────────────────────────────────────────

def test_a_present_zero_is_a_reading_not_an_absence():
    assert Scanner._first_present_number({"ratio": 0.0}, "ratio") == 0.0


def test_an_absent_key_is_unknown():
    assert Scanner._first_present_number({}, "ratio") is None
    assert Scanner._first_present_number({"ratio": None}, "ratio") is None


def test_it_falls_through_to_the_next_key_only_when_the_first_is_absent():
    assert Scanner._first_present_number({"b": 1.2}, "a", "b") == 1.2
    # Present-and-zero wins; it must not fall through to a stale alternate.
    assert Scanner._first_present_number({"a": 0.0, "b": 1.2}, "a", "b") == 0.0


@pytest.mark.parametrize("value", ["junk", object(), float("nan"), float("inf")])
def test_unusable_values_are_unknown_rather_than_raising_or_defaulting(value):
    assert Scanner._first_present_number({"ratio": value}, "ratio") is None


def test_a_malformed_first_key_still_finds_a_good_second():
    assert Scanner._first_present_number({"a": "junk", "b": 0.7}, "a", "b") == 0.7


# ── how the pillars vote ─────────────────────────────────────────────────────

def _votes(intel):
    return Scanner._intel_pillar_votes(_sc(), intel)[:2]


def test_all_sell_order_flow_votes_bearish_instead_of_being_read_as_balanced():
    """0.0 was becoming 1.0 — a maximally sell-side tape scored as neutral."""
    assert _votes({"order_flow": {"ratio": 0.0}}) == (1, 0)


def test_absent_order_flow_votes_for_nobody():
    assert _votes({}) == (0, 0)
    assert _votes({"order_flow": {}}) == (0, 0)


def test_malformed_order_flow_votes_for_nobody():
    assert _votes({"order_flow": {"ratio": "junk"}}) == (0, 0)
    assert _votes({"order_flow": {"ratio": float("nan")}}) == (0, 0)


def test_real_readings_still_vote_as_before():
    assert _votes({"order_flow": {"ratio": 0.70}}) == (1, 0)
    assert _votes({"order_flow": {"ratio": 1.30}}) == (0, 1)
    assert _votes({"order_flow": {"ratio": 1.00}}) == (0, 0)


def test_the_message_says_unknown_rather_than_inventing_a_number():
    reason = Scanner._intel_reversal_reason(
        _sc(), "LONG", {"pcr": {"contrarian_signal": "BEARISH"},
                        "oi": {"signal": "BEARISH"}}, min_votes=2
    )
    assert reason is not None
    assert "OFR=n/a" in reason and "PCR=n/a" in reason


# ── what counts as having evidence at all ────────────────────────────────────

def test_a_zero_pcr_is_the_empty_chain_sentinel_not_a_reading():
    """kite_market_data.put_call_ratio(empty frame) returns 0.0 by design."""
    assert Scanner._intel_evidence_is_usable(_sc(), {"pcr": {"primary_pcr": 0.0}}) is False


def test_a_real_pcr_is_evidence():
    assert Scanner._intel_evidence_is_usable(_sc(), {"pcr": {"primary_pcr": 0.89}}) is True


def test_a_malformed_pcr_is_not_evidence():
    assert Scanner._intel_evidence_is_usable(_sc(), {"pcr": {"primary_pcr": "junk"}}) is False


def test_order_flow_alone_is_evidence_including_at_zero():
    assert Scanner._intel_evidence_is_usable(_sc(), {"order_flow": {"ratio": 0.0}}) is True
    assert Scanner._intel_evidence_is_usable(_sc(), {"order_flow": {"ratio": "junk"}}) is False


# ── the peak-threat score reads the same pillar ──────────────────────────────

def test_peak_threat_counts_all_sell_flow_against_a_long():
    """`or 0.0` then `if flow_ratio > 0` skipped a genuine 0.0, so the most
    sell-heavy tape possible contributed nothing to a LONG's threat score."""
    sc = _sc()
    trade = SimpleNamespace(instrument="NIFTY", timeframe="5min", volume_ratio=0.0)
    scored = Scanner._peak_threat_flow_points(sc, trade, {"ratio": 0.0}, "LONG")
    assert scored == (1, "sell-heavy flow 0.00")


def test_peak_threat_ignores_absent_flow():
    sc = _sc()
    trade = SimpleNamespace(instrument="NIFTY", timeframe="5min", volume_ratio=0.0)
    assert Scanner._peak_threat_flow_points(sc, trade, {}, "LONG") == (0, "")
    assert Scanner._peak_threat_flow_points(sc, trade, {"ratio": "junk"}, "LONG") == (0, "")
