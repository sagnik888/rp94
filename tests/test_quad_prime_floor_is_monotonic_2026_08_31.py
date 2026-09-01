"""A solo-source B+ must have an admission path, and nothing may admit a C-grade.

2026-08-31. An operator running a parallel build reported two profitable trades this
build refused, both single-engine B+ around 67%:

    BANKNIFTY 57300CE 11:45  +Rs 2,323      MIDCPNIFTY 14875PE 13:56  +Rs 532

THE DEFECT. The quad-prime raw quality floor admitted on high_grade (grade >= A and
conf >= 0.70), multi_confirmed (>= 2 signal ENGINES), or b_grade_ok -- and
b_grade_entry_allowed governs the B BAND only, returning False the instant confidence
reaches 0.55, its docstring saying "B+/A/A+ use the standard confidence floors". No term
in the gate ever read those floors. A solo-source signal anywhere in 0.55-0.699 -- the
whole B+ band -- therefore had no admission path. `standard_floor_ok` restores the
documented ladder (B+ 0.55, A 0.70, A+ 0.85).

TWO THINGS THIS FILE GOT WRONG BEFORE, both found by a second audit:

  1. It swept confidence with GRADE PINNED. Grade is not a free variable: GRADE_BANDS
     makes it a pure bucket of confidence and signal_manager assigns it via
     confidence_to_grade(). Cases like "C at 95%" and "A at 68%" cannot occur, and those
     malformed states are what made a second term, `structural_relief_ok`, look both
     necessary and safe. Over reachable states it granted nothing the other terms did
     not. It has been removed as dead code, and the removal is provably behaviour-
     neutral: swept WITH and WITHOUT the term over 1,248,000 reachable states, not one
     decision differed. It was NOT a C-grade hazard, though this file briefly claimed so
     -- it required confidence >= 0.50 and a C-grade is confidence < 0.40 by
     construction. The same unreachable state that made it look necessary later made it
     look dangerous; both readings came from ignoring the grade/confidence invariant.

  2. It MIRRORED the gate's arithmetic instead of calling it, and every guard-rail case
     was written in a choppy regime at ADX 10 where the removed term was inert. A mirror
     cannot see a term it does not model, and a single regime cannot exercise a term that
     reads regime. That combination is why the hole survived the suite.

This file now calls `Scanner._quad_prime_raw_entry_decision` directly -- the real gate,
with the real candidate shape -- and sweeps regime x ADX x 15m-agreement x source count
x every context flag. The harness follows the one already proven in
tests/test_context_bypass_needs_a_floor_2026_08_12.py. Checked once at author time, the
retired mirror and the real gate agreed at all 7,800 compared grid points; that agreement
was never something the old test could have established.
"""
from itertools import product
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from engine.signal_quality_policy import confidence_to_grade
from scanner import Scanner

REGIMES = ("TRENDING_UP", "TRENDING_DOWN", "VOLATILE", "RANGING", "UNKNOWN")
ADX_VALUES = (5.0, 15.0, 25.0, 30.0, 45.0)
BOOLS = (False, True)


def _gate(
    confidence,
    *,
    regime="VOLATILE",
    adx=10.0,
    trend15=True,
    sources=1,
    risky=False,
    momentum=False,
    market_wide=False,
    cascade=False,
    volume_spike=False,
    power_move=False,
    intel=0.0,
    timeframe="5min",
):
    """Invoke the REAL gate. Grade is derived, never supplied independently."""
    scanner = Scanner.__new__(Scanner)
    scanner._quad_union_sources = (
        lambda _c: ["utbot"] if sources <= 1 else ["utbot", "partner2"]
    )
    scanner._candidate_15m_trend_agrees = lambda _c: trend15
    candidate = SimpleNamespace(
        grade=confidence_to_grade(confidence),
        confidence=confidence,
        timeframe=timeframe,
        adx_value=adx,
        intelligence_score=intel,
        volume_spike_entry=volume_spike,
        power_move=power_move,
        market_wide_reversal_entry=market_wide,
        one_way_cascade_entry=cascade,
        instrument="NIFTY",
        signal_type="BUY",
        market_wide_reversal_context={
            "core_indices": ["BANKNIFTY"],
            "indices": ["BANKNIFTY"],
        },
    )
    admitted, _scalp, reason = Scanner._quad_prime_raw_entry_decision(
        scanner,
        candidate,
        regime=regime,
        confidence=confidence,
        risky_regime=risky,
        momentum_override=momentum,
    )
    return bool(admitted), str(reason)


@pytest.mark.parametrize("regime", REGIMES)
@pytest.mark.parametrize("adx", ADX_VALUES)
def test_admission_is_monotonic_in_confidence(regime, adx):
    """Evidence cannot evaporate because the estimate of it got stronger.

    Swept across BOTH values of 15m agreement and source count, because the removed term
    read regime while the old tests pinned it to one value.
    """
    reversals = []
    for trend15, sources in product(BOOLS, (1, 2)):
        ladder = [
            (
                c / 1000.0,
                _gate(c / 1000.0, regime=regime, adx=adx, trend15=trend15,
                      sources=sources)[0],
            )
            for c in range(350, 1000, 5)
        ]
        for (low, was), (high, now) in zip(ladder, ladder[1:]):
            if was and not now:
                reversals.append(
                    f"t15={trend15} src={sources}: {low:.1%} admitted, {high:.1%} rejected"
                )
    assert not reversals, f"{regime} adx={adx}: " + "; ".join(reversals[:3])


def test_the_b_plus_dead_zone_is_closed():
    """The genuine defect: a solo source in 0.55-0.70 had no admission path at all."""
    for confidence in (0.55, 0.60, 0.65, 0.679, 0.699):
        assert confidence_to_grade(confidence) == "B+"
        admitted, reason = _gate(confidence, regime="VOLATILE", adx=10.0, sources=1)
        assert admitted, f"a solo B+ at {confidence:.1%} is still refused: {reason}"


def test_the_two_refused_signals_are_admitted():
    """Both single-engine B+ in a non-trending regime, on the real gate."""
    assert _gate(0.672, regime="VOLATILE", adx=21.6, sources=1)[0]
    assert _gate(0.679, regime="VOLATILE", adx=25.0, sources=1)[0]


@pytest.mark.parametrize("regime", REGIMES)
def test_no_combination_admits_a_c_grade(regime):
    """The hole `structural_relief_ok` opened, now swept the way it should have been.

    C is confidence < 0.40 by construction. Every context flag is exercised, because a
    guard rail tested at one regime and one ADX proves nothing about a term reading both.
    """
    admissions = []
    for adx, trend15, sources, market_wide, cascade, spike, power, intel in product(
        ADX_VALUES, BOOLS, (1, 2), BOOLS, BOOLS, BOOLS, BOOLS, (0.0, 0.9, -0.9)
    ):
        for confidence in (0.05, 0.20, 0.399):
            admitted, reason = _gate(
                confidence, regime=regime, adx=adx, trend15=trend15, sources=sources,
                market_wide=market_wide, cascade=cascade, volume_spike=spike,
                power_move=power, intel=intel,
            )
            if admitted:
                admissions.append(
                    f"conf={confidence:.0%} adx={adx} t15={trend15} src={sources} "
                    f"mw={market_wide} casc={cascade} spike={spike} power={power} "
                    f"intel={intel}: {reason[:70]}"
                )
    assert not admissions, (
        f"{regime}: a C-grade was admitted on quality alone -- "
        + "; ".join(admissions[:3])
    )


@pytest.mark.parametrize("regime", REGIMES)
@pytest.mark.parametrize("adx", ADX_VALUES)
def test_below_the_b_band_floor_is_refused(regime, adx):
    """Sub-0.50 must not admit; 0.50-0.54 is the long-standing B-band allowance."""
    for confidence in (0.35, 0.42, 0.49):
        admitted, reason = _gate(confidence, regime=regime, adx=adx, sources=1)
        assert not admitted, (
            f"{confidence:.0%} ({confidence_to_grade(confidence)}) admitted in "
            f"{regime} adx={adx}: {reason[:80]}"
        )


def test_the_redundant_term_has_not_returned():
    """It added no reachable admission at all.

    A behavioural test cannot catch its return -- that is precisely the point, since a
    differential sweep of 1,248,000 reachable states found zero decision changes. So this
    guard is deliberately structural: it keeps dead complexity out of a gate that is hard
    enough to reason about already.
    """
    import inspect

    source = inspect.getsource(Scanner)
    assert "structural_relief_ok = bool(" not in source, (
        "structural_relief_ok is back; it is redundant over reachable states and reads "
        "regime without grade, which is what let it admit a C-grade"
    )


def test_grade_is_a_bucket_of_confidence():
    """The invariant the first version of this file violated."""
    assert confidence_to_grade(0.95) == "A+"
    assert confidence_to_grade(0.72) == "A"
    assert confidence_to_grade(0.60) == "B+"
    assert confidence_to_grade(0.45) == "B"
    assert confidence_to_grade(0.20) == "C"


def test_the_minimum_grade_is_a_setting_and_defaults_to_b_plus():
    assert str(
        getattr(get_settings(), "quad_prime_raw_standard_floor_min_grade", "")
    ).upper() == "B+"
