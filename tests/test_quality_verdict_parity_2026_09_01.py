"""One quality decision, in one place, making exactly the decisions it replaced.

WHY IT WAS CONSOLIDATED
-----------------------
The live entry gate had grown to 322 lines in which the quality question -- "is this
signal good enough to trade?" -- was answered by six separate checks, each reading some
mixture of grade, confidence, regime, ADX and 15-minute agreement, and each able to veto
on its own. Whichever fired first became the recorded reason and masked the rest.

Measured over 22 session files: 11,966 raw rejection rows collapsing to 2,969 distinct
signal-time opportunities, and EVERY populated (grade, confidence) cell had four or more
different gates able to reject in it:

    B+ 55-70%   passes_quality_gate 2,553 | repaint 184 | readiness 142 | choppy 128
    A+ >= 70%   late_trend 130 | passes_quality_gate 80 | repaint 75 | readiness 46
    C  < 50%    c_grade_block 1,921 | repaint 406 | readiness 384 | price_action 167

WHAT THIS RELEASE CHANGES: NOTHING, DELIBERATELY
------------------------------------------------
engine/quality_verdict.py is a faithful transcription of the rules it replaces. The
value is structural -- one readable place, an explicit route, a stable reason code -- so
that the NEXT change can be argued from evidence rather than from a 322-line function.

Shipping a simplification and a behaviour change in the same step would make any later
regression impossible to attribute. So parity is the contract, and this file is what
holds it: the evaluator is swept against a verbatim transcription of the old logic over
the full input space.

The reference implementation below is that transcription. It is intentionally ugly; it
is a record of what the code did, not of what it should do.
"""
from itertools import product

import pytest

from config.settings import get_settings
from engine.quality_verdict import POLICY_VERSION, QualityInputs, evaluate_quality
from engine.signal_quality_policy import (
    b_grade_entry_allowed,
    choppy_confidence_gate,
    confidence_to_grade,
    grade_min_confidence,
    relaxed_threshold,
)

REGIMES = ("TRENDING_UP", "TRENDING_DOWN", "VOLATILE", "RANGING", "CHOPPY",
           "UNKNOWN", "BREAKOUT")
TIMEFRAMES = ("1min", "5min", "15min")
INSTRUMENTS = ("OPT", "FUT")
ADX_VALUES = (0.0, 10.0, 21.6, 25.0, 30.0, 45.0)
BOOLS = (False, True)


def _legacy_quality(conf, regime, timeframe, inst_type, adx, trend15, impulse,
                    risky, choppy, settings):
    """Verbatim transcription of the live gate's quality tail, pre-2026-09-01.

    scanner.py lines 25030-25132 as they stood, minus the market-location gate, which
    mutates the candidate and is not a quality rule.
    """
    if regime == "UNKNOWN" and conf < grade_min_confidence("A"):
        return False
    if conf < grade_min_confidence("B+"):
        return bool(
            b_grade_entry_allowed(conf, regime, trend_15m_agrees=trend15,
                                  adx_value=adx, settings=settings)
        )
    if timeframe == "5min":
        min_conf = relaxed_threshold(
            float(getattr(settings, "ut_5min_option_min_confidence", 0.70) or 0.70),
            settings,
        )
        grade_conf_ok = conf >= max(grade_min_confidence("A"), min_conf)
        if risky and inst_type == "OPT":
            risky_min_conf = max(min_conf, 0.70)
            if conf < risky_min_conf or not (trend15 or impulse):
                return False
        if not (grade_conf_ok or trend15):
            return False
        if choppy and not (grade_conf_ok or trend15):
            return False
    if choppy and inst_type == "OPT" and conf < min(
        max(grade_min_confidence("A"), relaxed_threshold(0.72, settings)),
        relaxed_threshold(choppy_confidence_gate(settings), settings),
    ):
        return False
    if choppy and conf < min(
        grade_min_confidence("A"),
        relaxed_threshold(choppy_confidence_gate(settings), settings),
    ):
        return False
    return True


def _verdict(conf, regime, timeframe, inst_type, adx, trend15, impulse, risky, choppy):
    return evaluate_quality(
        QualityInputs(
            timeframe=timeframe, inst_type=inst_type, confidence=conf, regime=regime,
            adx_value=adx, trend_15m_agrees=trend15, impulse_confirmed=impulse,
            risky_regime=risky, is_choppy=choppy, grade=confidence_to_grade(conf),
        ),
        get_settings(),
    )


@pytest.fixture
def legacy_floor(monkeypatch):
    """Pin the 5-minute floor to the value the old gate hardcoded.

    The consolidation itself changed nothing; a LATER, separate change made the
    5-minute floor a named setting and dropped its default 0.70 -> 0.65. Parity is
    therefore asserted at 0.70, so this file keeps proving what it was written to prove
    -- that moving the rules did not move the decisions -- and the deliberate change is
    pinned on its own below.
    """
    settings = get_settings()
    original = getattr(settings, "live_5min_quality_min_confidence", 0.65)
    monkeypatch.setattr(settings, "live_5min_quality_min_confidence", 0.70,
                        raising=False)
    yield settings
    monkeypatch.setattr(settings, "live_5min_quality_min_confidence", original,
                        raising=False)


@pytest.mark.parametrize("regime", REGIMES)
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_the_evaluator_reproduces_the_gate_it_replaced(regime, timeframe, legacy_floor):
    """Parity is the contract of the consolidation. Any mismatch is a regression."""
    settings = legacy_floor
    mismatches = []
    for inst_type, adx, trend15, impulse, risky, choppy in product(
        INSTRUMENTS, ADX_VALUES, BOOLS, BOOLS, BOOLS, BOOLS
    ):
        for step in range(30, 100):
            confidence = step / 100.0
            legacy = _legacy_quality(confidence, regime, timeframe, inst_type, adx,
                                     trend15, impulse, risky, choppy, settings)
            verdict = _verdict(confidence, regime, timeframe, inst_type, adx,
                               trend15, impulse, risky, choppy)
            if bool(legacy) != bool(verdict.allowed):
                mismatches.append(
                    f"conf={confidence:.0%} {inst_type} adx={adx} t15={trend15} "
                    f"imp={impulse} risky={risky} chop={choppy}: "
                    f"legacy={legacy} new={verdict.allowed} ({verdict.reason_code})"
                )
    assert not mismatches, (
        f"{regime}/{timeframe}: " + "; ".join(mismatches[:4])
    )


def test_every_verdict_carries_a_route_and_a_reason_code():
    """A decision the operator cannot attribute is the problem being solved."""
    seen_routes, seen_codes = set(), set()
    for regime, timeframe, inst_type, adx, trend15, risky, choppy in product(
        REGIMES, TIMEFRAMES, INSTRUMENTS, (0.0, 30.0), BOOLS, BOOLS, BOOLS
    ):
        for step in (35, 45, 52, 60, 68, 75, 90):
            verdict = _verdict(step / 100.0, regime, timeframe, inst_type, adx,
                               trend15, False, risky, choppy)
            assert verdict.route, "a verdict with no route"
            assert verdict.reason_code, "a verdict with no reason code"
            assert verdict.reason, "a verdict with no human reason"
            assert verdict.policy_version == POLICY_VERSION
            seen_routes.add(verdict.route)
            seen_codes.add(verdict.reason_code)

    assert "standard" in seen_routes and "b_band" in seen_routes
    assert "QUALITY_OK" in seen_codes
    assert len(seen_codes) >= 5, f"only {len(seen_codes)} distinct reason codes"


def test_the_verdict_is_usable_as_a_boolean():
    """The call site replaced a bare bool; the object must not silently be truthy."""
    blocked = _verdict(0.10, "UNKNOWN", "5min", "OPT", 0.0, False, False, False, False)
    assert bool(blocked) is False
    allowed = _verdict(0.90, "TRENDING_UP", "15min", "FUT", 30.0, True, False, False, False)
    assert bool(allowed) is True


def test_the_live_gate_delegates_instead_of_reimplementing():
    """The point of the change: the rules live in ONE module now."""
    import inspect

    from scanner import Scanner

    source = inspect.getsource(Scanner._passes_live_trade_ready_gate)
    assert "evaluate_quality(" in source, "the live gate no longer calls the evaluator"
    assert "quality.reason_code" in source, "the reason code is not surfaced to the block"
    for gone in (
        "range/choppy regime allows only",
        "B-grade requires trending regime",
        "needs A/A+ with >= ",
    ):
        assert gone not in source, (
            f"the live gate still hand-rolls a quality rule ({gone!r}); it should read "
            "the verdict instead"
        )


def test_the_unknown_regime_refusal_still_precedes_the_location_gate():
    """Ordering matters: the location gate mutates the candidate.

    The verdict is computed once, up front, but CHECKED either side of that gate so an
    UNKNOWN-regime refusal never runs the location gate's side effects.
    """
    import inspect

    from scanner import Scanner

    source = inspect.getsource(Scanner._passes_live_trade_ready_gate)
    evaluated = source.index("quality = evaluate_quality(")
    unknown = source.index('quality.route == "unknown_regime"')
    # The raw/quad-union branch calls the location gate much earlier in the function on
    # its own path; the one that matters here is the call on the standard path, so search
    # forward from the UNKNOWN check rather than taking the first occurrence.
    location = source.index("_market_location_entry_gate(candidate)", unknown)
    final = source.index("if not quality.allowed:", unknown)

    assert evaluated < unknown < location < final, (
        "the UNKNOWN-regime refusal no longer runs before the market-location gate"
    )


def test_the_only_deliberate_change_is_the_5min_floor(monkeypatch):
    """What lowering the floor to 0.65 actually does -- and does not do.

    It admits 5-minute signals between 0.65 and the old 0.70 that have NO 15-minute
    agreement. Nothing else moves: not other timeframes, not the B band, not the choppy
    rules, not the risky-regime option path (which keeps its own 0.70).
    """
    settings = get_settings()
    changed, unchanged = [], 0
    for regime, timeframe, inst_type, adx, trend15, impulse, risky, choppy in product(
        REGIMES, TIMEFRAMES, INSTRUMENTS, ADX_VALUES, BOOLS, BOOLS, BOOLS, BOOLS
    ):
        for step in range(30, 100):
            confidence = step / 100.0
            monkeypatch.setattr(settings, "live_5min_quality_min_confidence", 0.70,
                                raising=False)
            before = _verdict(confidence, regime, timeframe, inst_type, adx, trend15,
                              impulse, risky, choppy).allowed
            monkeypatch.setattr(settings, "live_5min_quality_min_confidence", 0.65,
                                raising=False)
            after = _verdict(confidence, regime, timeframe, inst_type, adx, trend15,
                             impulse, risky, choppy).allowed
            if before != after:
                changed.append((confidence, timeframe, trend15, risky, inst_type, before, after))
            else:
                unchanged += 1

    assert changed, "lowering the floor changed nothing at all; the knob is not wired"
    # every change must be an ADMISSION, on 5min, in the 0.65-0.70 band, without 15m help
    for confidence, timeframe, trend15, risky, inst_type, before, after in changed:
        assert (before, after) == (False, True), "the floor drop refused something new"
        assert timeframe == "5min", f"a non-5min decision moved: {timeframe}"
        assert 0.65 <= confidence < 0.70, f"out-of-band change at {confidence:.0%}"
        assert trend15 is False, "a signal with 15m agreement was already admitted"
        assert not (risky and inst_type == "OPT"), (
            "the risky-regime option path must keep its own 0.70 floor"
        )
    assert unchanged > len(changed) * 100, (
        f"{len(changed)} decisions moved against {unchanged} unchanged -- far too broad "
        "for a single-threshold change"
    )
