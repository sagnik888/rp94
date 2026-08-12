"""A market-wide reversal is context, not a licence to trade any signal.

2026-08-12 12:42, two entries admitted 3 seconds apart and both dead inside 90s:

  12:42:28 admitting NIFTY 15min LONG from utbot (C, 12%) ... trend15=False
  12:42:31 admitting SENSEX 15min LONG from utbot (C, 13%) ... trend15=False
  12:43:33 NIFTY  -> LOSS_THREAT_EXIT   -Rs.268
  12:43:52 SENSEX -> REPAINT_ABORT      -Rs.155

Ninety seconds later the same gate BLOCKED a far better signal:

  12:44:10 blocked MIDCPNIFTY 5min SHORT; quad-prime raw quality floor failed
           (solo_sources=1, grade=B+, conf=67%, adx=30.0, trend15=True)

B+ at 67% refused, C at 12% admitted. market_wide_reversal_entry is a term in the
quality floor's `allowed` expression, and it was the only way in: with every flag
False the same candidate is refused. It exists to relax the OPPOSITE-EXPOSURE
rule — "the market turned, so a reverse entry is acceptable this cycle" — which
says nothing about whether this particular signal is worth trading.

Every other bypass in that expression already carries a confidence floor
(high_grade 0.70, multi_confirmed 0.55, b_grade_ok 0.50, exceptional_spike 0.50).
The two context flags carried none. exceptional_spike is the template: a special
context that still demands quad_prime_raw_min_confidence.
"""
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from scanner import Scanner


def _scanner():
    sc = Scanner.__new__(Scanner)
    sc._quad_union_sources = lambda _c: ["utbot"]
    sc._candidate_15m_trend_agrees = lambda _c: False
    return sc


def _candidate(confidence, grade="C", **flags):
    cand = SimpleNamespace(
        grade=grade, confidence=confidence, timeframe="15min", adx_value=0.0,
        intelligence_score=0.0, volume_spike_entry=False, power_move=False,
        market_wide_reversal_entry=False, one_way_cascade_entry=False,
    )
    for key, value in flags.items():
        setattr(cand, key, value)
    return cand


def _decide(cand):
    return Scanner._quad_prime_raw_entry_decision(
        _scanner(), cand, regime="TRENDING_DOWN",
        confidence=float(cand.confidence), risky_regime=False, momentum_override=False,
    )


@pytest.mark.parametrize("flag", ["market_wide_reversal_entry", "one_way_cascade_entry"])
@pytest.mark.parametrize("confidence", [0.12, 0.13, 0.49])
def test_context_alone_cannot_admit_a_signal_below_the_raw_floor(flag, confidence):
    allowed, _scalp, reason = _decide(_candidate(confidence, **{flag: True}))

    assert allowed is False
    assert "quality floor failed" in reason


@pytest.mark.parametrize("flag", ["market_wide_reversal_entry", "one_way_cascade_entry"])
def test_the_context_bypass_still_works_for_a_signal_worth_taking(flag):
    """The feature is preserved: only sub-floor conviction is refused."""
    allowed, _scalp, reason = _decide(_candidate(0.60, grade="B+", **{flag: True}))

    assert allowed is True
    assert "quality floor failed" not in reason


def test_the_floor_is_the_same_one_every_other_bypass_uses():
    floor = float(getattr(get_settings(), "quad_prime_raw_min_confidence", 0.50))

    below, _s1, _r1 = _decide(_candidate(floor - 0.01, market_wide_reversal_entry=True))
    at, _s2, _r2 = _decide(_candidate(floor, market_wide_reversal_entry=True))

    assert below is False
    assert at is True


def test_todays_two_admissions_are_now_refused():
    """The exact candidates from 12:42:28 and 12:42:31."""
    for confidence in (0.12, 0.13):
        allowed, _scalp, reason = _decide(
            _candidate(confidence, market_wide_reversal_entry=True)
        )
        assert allowed is False, f"{confidence:.0%} still admitted: {reason}"


def test_a_good_signal_is_unaffected_by_the_change():
    """A+ at 80% with no context flag at all still goes through as before."""
    allowed, scalp, _reason = _decide(_candidate(0.80, grade="A+"))

    assert allowed is True
    assert scalp is False  # runner, not scalp
