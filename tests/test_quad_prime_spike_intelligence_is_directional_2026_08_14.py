"""The exceptional-spike leg accepted intelligence pointing the wrong way.

_quad_prime_raw_entry_decision is the quality floor that quad_prime raw candidates
must clear — and quad_prime is the operator's live engine (SCANNER_ENGINE='quad_prime').
Its exceptional_spike term read:

    intel_abs = abs(float(getattr(candidate, "intelligence_score", 0.0) or 0.0))
    ...
    exceptional_spike = bool(
        (power_move or volume_spike or momentum_override)
        and confidence >= min_conf
        and (adx_val >= 25.0 or intel_abs >= 0.70 or trend_15m_agrees)
    )

intelligence_score is SIGNED — positive bullish, negative bearish. abs() threw the
sign away, so a reading of -0.80 (strongly bearish) cleared the bar for a LONG. The
term sits in an OR beside adx and trend_15m_agrees, so it could carry the leg alone,
and exceptional_spike carries no grade floor: a C-grade at the 50% minimum with a
volume spike and intelligence arguing the other way was admitted.

The sibling site at scanner.py:20417 uses the same abs() safely, because there it is
AND-ed with trend_agrees, which already establishes direction. This one had nothing.

The rest of the codebase already had the right convention:

    scanner.py:22511  intel_ok = signed >= min if signal_type == "BUY" else signed <= -min
    scanner.py:22583  intel_aligns = signed >= min if signal_type == "BUY" else signed <= -min

The threshold is now quad_prime_raw_spike_min_intel_score rather than a literal,
matching rare_burst_min_intel_score which uses the same 0.70 the same way.
"""
import inspect

import pytest

from scanner import Scanner


def _decide(**kw):
    """Evaluate just the direction-aware intelligence leg as the fix computes it."""
    signed = kw["signed_intel"]
    signal_type = "BUY" if kw["direction"] == "LONG" else "SELL"
    floor = kw.get("floor", 0.70)
    return signed >= floor if signal_type == "BUY" else signed <= -floor


# ── the defect ───────────────────────────────────────────────────────────────

def test_bearish_intelligence_no_longer_qualifies_a_long():
    """The reported case: -0.80 satisfied abs() >= 0.70 for a LONG."""
    assert abs(-0.80) >= 0.70, "the old sign-blind test would have passed this"

    assert _decide(signed_intel=-0.80, direction="LONG") is False


def test_bullish_intelligence_no_longer_qualifies_a_short():
    assert abs(0.80) >= 0.70

    assert _decide(signed_intel=0.80, direction="SHORT") is False


# ── aligned intelligence still qualifies ─────────────────────────────────────

def test_bullish_intelligence_still_qualifies_a_long():
    assert _decide(signed_intel=0.80, direction="LONG") is True


def test_bearish_intelligence_still_qualifies_a_short():
    assert _decide(signed_intel=-0.80, direction="SHORT") is True


@pytest.mark.parametrize("direction,signed", [("LONG", 0.70), ("SHORT", -0.70)])
def test_the_boundary_is_inclusive(direction, signed):
    assert _decide(signed_intel=signed, direction=direction) is True


@pytest.mark.parametrize("direction,signed", [("LONG", 0.69), ("SHORT", -0.69)])
def test_just_under_the_bar_does_not_qualify(direction, signed):
    assert _decide(signed_intel=signed, direction=direction) is False


def test_a_neutral_reading_qualifies_neither_side():
    assert _decide(signed_intel=0.0, direction="LONG") is False
    assert _decide(signed_intel=0.0, direction="SHORT") is False


# ── wired into the real decision ─────────────────────────────────────────────

def test_the_decision_uses_the_signed_value_not_the_absolute():
    src = inspect.getsource(Scanner._quad_prime_raw_entry_decision)

    assert "intel_aligned" in src, "the direction-aware term is missing"
    assert "_candidate_signed_intelligence(candidate)" in src
    assert "_candidate_signal_type(candidate)" in src
    assert "or intel_abs >= 0.70 or" not in src, (
        "the sign-blind intelligence leg is back in exceptional_spike"
    )


def test_the_spike_intelligence_floor_is_configurable():
    """The operator's standing rule: thresholds are settings, never literals."""
    from config.settings import Settings

    assert "quad_prime_raw_spike_min_intel_score" in Settings.model_fields

    src = inspect.getsource(Scanner._quad_prime_raw_entry_decision)
    assert "quad_prime_raw_spike_min_intel_score" in src


def test_the_alignment_reads_the_candidates_own_side():
    """SELL/SHORT must invert the comparison, not reuse the BUY branch."""
    src = inspect.getsource(Scanner._quad_prime_raw_entry_decision)
    i = src.index("intel_aligned")
    window = src[i: i + 300]

    assert '== "BUY"' in window
    assert "-spike_min_intel" in window, "the short side must test the negative bar"


def test_the_rejection_note_reports_the_signed_reading():
    """Without the sign in the log the operator cannot tell this case from a weak one."""
    src = inspect.getsource(Scanner._quad_prime_raw_entry_decision)

    assert "intel={signed_intel:+.2f}" in src
