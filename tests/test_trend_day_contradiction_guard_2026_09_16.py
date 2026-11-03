"""A regime label pointing against a confirmed trend day must be corrected.

2026-09-15: the indices fell ~2%. Session context called TREND_DAY_DOWN with 0.76-0.91
confidence from 09:45 and breadth read BEARISH 4/4 from 10:15, while the detector held
TRENDING_UP until 11:00. A with-trend SHORT was therefore scored -0.25 in the regime
pillar instead of +0.30 -- a 0.55 swing that dropped 36 refused SHORTs to 15-37%
confidence, under the 55% B+ floor. Their shadow outcomes were +Rs.43,285 at 30 minutes.

The lag was not the 5-bar majority vote: `raw_regime` itself read TRENDING_UP on 34 of
those 36 rows. These tests pin the correction on the regime INPUT, and pin the limits
that keep it from inventing trends.
"""
import pytest

from config.settings import Settings
from engine.market_regime import MarketRegime
from intelligence.regime_detector import RegimeDetector


DETECTOR = RegimeDetector.__new__(RegimeDetector)

# A sentinel, so a test can pass an explicit None and mean it. Defaulting to None here
# would silently substitute the healthy fixture and make the "absent evidence" tests
# assert nothing.
_UNSET = object()


def _guard(regime, *, day_type="TREND_DAY_DOWN", day_conf=0.85,
           bias="BEARISH", votes=4, settings=None, session=_UNSET, breadth=_UNSET):
    if session is _UNSET:
        session = {"effective_day_type": day_type, "effective_confidence": day_conf}
    if breadth is _UNSET:
        breadth = {"bias": bias, "aligned_count": votes}
    return RegimeDetector._trend_day_contradicted_regime(
        DETECTOR,
        settings=settings or Settings(),
        session_context=session,
        breadth_context=breadth,
        regime=regime,
    )


# --- the 2026-09-15 case --------------------------------------------------------
def test_trending_up_on_a_confirmed_down_day_is_corrected():
    assert _guard("TRENDING_UP") == MarketRegime.TRENDING_DOWN.value


def test_breakout_up_on_a_confirmed_down_day_is_corrected():
    assert _guard("BREAKOUT_UP") == MarketRegime.TRENDING_DOWN.value


def test_the_mirror_case_is_corrected_too():
    assert _guard("TRENDING_DOWN", day_type="TREND_DAY_UP", bias="BULLISH") == (
        MarketRegime.TRENDING_UP.value
    )
    assert _guard("BREAKOUT_DOWN", day_type="TREND_DAY_UP", bias="BULLISH") == (
        MarketRegime.TRENDING_UP.value
    )


def test_the_guard_stays_silent_while_breadth_still_disagreed():
    """09:25-10:00 on 09-15: session said DOWN but breadth was still BULLISH 4/4.
    One witness is not enough to overrule the detector."""
    assert _guard("TRENDING_UP", bias="BULLISH", votes=4) == ""


# --- it must not invent a trend --------------------------------------------------
@pytest.mark.parametrize("regime", ["RANGING", "VOLATILE", "UNKNOWN", "MEAN_REVERTING", ""])
def test_a_non_trend_label_is_never_promoted_to_a_trend(regime):
    assert _guard(regime) == ""


def test_a_label_that_already_agrees_is_left_alone():
    """Correcting an agreeing label would double-count the same evidence."""
    assert _guard("TRENDING_DOWN") == ""
    assert _guard("BREAKOUT_DOWN") == ""


def test_a_range_day_never_triggers_the_trend_correction():
    assert _guard("TRENDING_UP", day_type="RANGE_DAY") == ""
    assert _guard("TRENDING_UP", day_type="UNDECIDED") == ""
    assert _guard("TRENDING_UP", day_type="") == ""


# --- both witnesses must be measured ---------------------------------------------
def test_a_low_confidence_day_type_does_not_overrule():
    assert _guard("TRENDING_UP", day_conf=0.69) == ""
    assert _guard("TRENDING_UP", day_conf=0.70) == MarketRegime.TRENDING_DOWN.value


def test_the_confidence_floor_is_configurable():
    settings = Settings()
    settings.regime_trend_day_contradiction_min_confidence = 0.90
    assert _guard("TRENDING_UP", day_conf=0.85, settings=settings) == ""
    assert _guard("TRENDING_UP", day_conf=0.91, settings=settings) == (
        MarketRegime.TRENDING_DOWN.value
    )


def test_thin_breadth_does_not_count_as_a_second_witness():
    assert _guard("TRENDING_UP", votes=2) == ""
    assert _guard("TRENDING_UP", votes=3) == MarketRegime.TRENDING_DOWN.value


def test_absent_breadth_is_not_agreement():
    """Missing evidence must not read as confirmation."""
    assert _guard("TRENDING_UP", breadth={}) == ""
    assert _guard("TRENDING_UP", breadth=None) == ""


def test_absent_session_context_is_not_agreement():
    assert _guard("TRENDING_UP", session={}) == ""
    assert _guard("TRENDING_UP", session=None) == ""


def test_unparseable_evidence_is_refused_rather_than_raising():
    assert _guard("TRENDING_UP", session={"day_type": "TREND_DAY_DOWN",
                                          "confidence": "not-a-number"}) == ""
    assert _guard("TRENDING_UP", breadth={"bias": "BEARISH",
                                          "aligned_count": "four"}) == ""


def test_the_plain_day_type_keys_are_honoured_when_effective_is_absent():
    assert _guard("TRENDING_UP",
                  session={"day_type": "TREND_DAY_DOWN", "confidence": 0.8}) == (
        MarketRegime.TRENDING_DOWN.value
    )


# --- the switch ------------------------------------------------------------------
def test_the_guard_can_be_switched_off():
    settings = Settings()
    settings.regime_trend_day_contradiction_guard_enabled = False
    assert _guard("TRENDING_UP", settings=settings) == ""


def test_the_guard_ships_on():
    settings = Settings()
    assert settings.regime_trend_day_contradiction_guard_enabled is True
    assert settings.regime_trend_day_contradiction_min_confidence == pytest.approx(0.70)
    assert settings.regime_trend_day_contradiction_min_breadth_votes == 3


def test_the_guard_reaches_the_archive():
    """rtracker copies a FIXED regime key list, so a field not named there is invisible.

    Verified on 2026-09-16: the guard shipped, but no archived row carried it, which
    would have made a label rewrite impossible to audit after the fact.
    """
    from intelligence.rtracker import RegimeTracker

    class _Candidate:
        instrument = "NIFTY"
        direction = "SHORT"
        timeframe = "5min"
        status = "REJECTED"
        market_regime = "TRENDING_DOWN"
        market_intelligence_context = {
            "regime": {
                "regime": "TRENDING_DOWN",
                "trend_day_contradiction_guard": True,
                "trend_day_contradiction_guard_reason": (
                    "trend_day_contradiction_TRENDING_UP_to_TRENDING_DOWN"
                ),
            }
        }

    event = RegimeTracker._event_from_candidate(
        RegimeTracker.__new__(RegimeTracker),
        _Candidate(),
        decision="REJECTED",
        reason="",
        source="test",
        extra=None,
    )

    assert event["regime"]["trend_day_contradiction_guard"] is True
    assert event["regime"]["trend_day_contradiction_guard_reason"].startswith(
        "trend_day_contradiction_"
    )


def test_an_unguarded_row_archives_false_not_missing():
    from intelligence.rtracker import RegimeTracker

    class _Plain:
        instrument = "NIFTY"
        direction = "LONG"
        timeframe = "5min"
        status = "REJECTED"
        market_regime = "RANGING"

    event = RegimeTracker._event_from_candidate(
        RegimeTracker.__new__(RegimeTracker),
        _Plain(),
        decision="REJECTED",
        reason="",
        source="test",
        extra=None,
    )
    assert event["regime"]["trend_day_contradiction_guard"] is False
    assert event["regime"]["trend_day_contradiction_guard_reason"] == ""


# --- the swing it was built to recover -------------------------------------------
def test_the_corrected_label_pays_a_with_trend_short_instead_of_penalising_it():
    """The whole point: the regime pillar must flip sign for the refused SHORTs."""
    from engine.signal_manager import SignalManager  # noqa: F401  (import guard only)

    wrong_label = "TRENDING_UP"
    corrected = _guard(wrong_label)
    assert corrected == "TRENDING_DOWN"

    # A SELL scores -0.25 under the wrong label and +0.30 under the corrected one.
    def regime_pillar(regime, signal_type):
        if regime in {"TRENDING_UP", "BREAKOUT_UP"}:
            return 0.30 if signal_type == "BUY" else -0.25
        if regime in {"TRENDING_DOWN", "BREAKOUT_DOWN"}:
            return 0.30 if signal_type == "SELL" else -0.25
        return 0.0

    assert regime_pillar(wrong_label, "SELL") == pytest.approx(-0.25)
    assert regime_pillar(corrected, "SELL") == pytest.approx(0.30)
    assert regime_pillar(corrected, "SELL") - regime_pillar(wrong_label, "SELL") == (
        pytest.approx(0.55)
    )
