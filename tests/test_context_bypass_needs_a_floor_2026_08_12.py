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


def _candidate(confidence, grade="C", peers=("BANKNIFTY",), **flags):
    cand = SimpleNamespace(
        grade=grade, confidence=confidence, timeframe="15min", adx_value=0.0,
        intelligence_score=0.0, volume_spike_entry=False, power_move=False,
        market_wide_reversal_entry=False, one_way_cascade_entry=False,
        instrument="NIFTY",
        market_wide_reversal_context={"core_indices": list(peers), "indices": list(peers)},
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


def test_the_cascade_floor_is_the_one_every_other_bypass_uses():
    floor = float(getattr(get_settings(), "quad_prime_raw_min_confidence", 0.50))

    below, _s1, _r1 = _decide(_candidate(floor - 0.01, one_way_cascade_entry=True))
    at, _s2, _r2 = _decide(_candidate(floor, one_way_cascade_entry=True))

    assert below is False
    assert at is True


# ── the market-wide guard restored from restore_point_51 ─────────────────────
# _market_wide_candidate_quality was absent from this build entirely, taking the
# B+ / 60% / independently-confirmed-peer requirements with it.

def test_market_wide_enforces_the_configured_grade_and_confidence():
    """Whatever the bar is set to, it is enforced on both axes."""
    settings = get_settings()
    min_conf = float(getattr(settings, "market_wide_reversal_min_confidence", 0.50))

    good, _s, _r = _decide(_candidate(min_conf, grade="B+", market_wide_reversal_entry=True))
    weak_conf, _s2, r2 = _decide(
        _candidate(min_conf - 0.01, grade="B+", market_wide_reversal_entry=True)
    )
    weak_grade, _s3, r3 = _decide(
        _candidate(0.80, grade="C", market_wide_reversal_entry=True)
    )

    assert good is True
    assert weak_conf is False and "quality floor failed" in r2
    assert weak_grade is False and "quality floor failed" in r3


def test_market_wide_needs_an_independently_confirmed_peer():
    """A "market-wide" reversal naming only this index is not market-wide.

    B+ at 60% is the band where ONLY this bypass can admit: too low for
    high_grade (0.70), one source so not multi_confirmed, and above the B band
    that b_grade_ok governs.
    """
    alone, _s, _r = _decide(
        _candidate(0.60, grade="B+", peers=("NIFTY",), market_wide_reversal_entry=True)
    )
    none, _s2, _r2 = _decide(
        _candidate(0.60, grade="B+", peers=(), market_wide_reversal_entry=True)
    )
    with_peer, _s3, _r3 = _decide(
        _candidate(0.60, grade="B+", peers=("SENSEX",), market_wide_reversal_entry=True)
    )

    assert alone is False
    assert none is False
    assert with_peer is True


def test_the_quality_helper_reports_why_it_refused():
    sc = _scanner()
    ok, why = sc._market_wide_candidate_quality(
        _candidate(0.20, grade="C", market_wide_reversal_entry=True)
    )

    assert ok is False
    assert "grade" in why or "confidence" in why


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


# ── the bar is this build's own floor, not restore_point_51's ────────────────
# v51 sets B+/60% on the market-wide path, but its `allowed` expression carries an
# extra admission route this build does not have:
#     or bool(census_ctx)          # _census_range_scalp_context, absent here
# Porting the restriction without the compensator made this build strictly tighter
# than the one it was matching. 2026-08-12 14:30 BANKNIFTY: grade B, 54%,
# market-wide core 3/3 total 4 — the session's largest winner at +Rs.1,300 — sat
# exactly in the gap, while the junk this guard exists to stop was C grade at
# 12-13%. The floor every other bypass already uses separates them with room.

def test_the_market_wide_bar_admits_the_session_s_largest_winner():
    """2026-08-12 14:30 BANKNIFTY: B / 54% / core 3/3 -> +Rs.1,300."""
    allowed, _scalp, reason = _decide(
        _candidate(0.54, grade="B", peers=("MIDCPNIFTY", "NIFTY", "SENSEX"),
                   market_wide_reversal_entry=True)
    )

    assert allowed is True, reason


def test_the_market_wide_bar_still_refuses_the_c_grade_junk():
    """The 12:42 pair, C grade at 12% and 13%, both dead inside 90 seconds."""
    for confidence in (0.12, 0.13):
        allowed, _scalp, reason = _decide(
            _candidate(confidence, grade="C", market_wide_reversal_entry=True)
        )
        assert allowed is False, f"{confidence:.0%} still admitted: {reason}"


def test_the_bar_is_the_floor_every_other_bypass_uses():
    settings = get_settings()

    assert float(getattr(settings, "market_wide_reversal_min_confidence")) == pytest.approx(
        float(getattr(settings, "quad_prime_raw_min_confidence"))
    )
    assert str(getattr(settings, "market_wide_reversal_min_grade")) == "B"
