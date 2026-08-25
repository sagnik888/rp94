"""Stand aside entirely while the session bias reads BEARISH.

Measured over 320 advisory round-trips (2026-07-08 .. 2026-08-20), joining each
ACCEPTED entry's intel snapshot to its EXIT outcome:

    entries taken while session.bias == "BEARISH"   n=40   -Rs.7,079
      by time-third:  3 / -Rs.368     18 / -Rs.5     19 / -Rs.309    (negative in all 3)

Dropping them lifts the book from +Rs.5,961 to +Rs.18,240 net of a Rs.130 round trip,
and win rate from 40% to 43%.

DELIBERATELY BLANKET, not direction-aware. The direction-aware forms were tested and
REFUTED on the same corpus:

    block BEARISH + LONG only          n=23  -Rs.1,427  but T2 is +Rs.11 -> not sign-stable
    block counter-bias BOTH ways       n=43  +Rs.1,904  -> blocking it LOSES money
    block regime_confidence < 55       n=80  +Rs.9,524  -> blocking it LOSES money
    block conviction-contradicts-label n=20  +Rs.4,711  -> blocking it LOSES money

The SHORT side during a bearish session is the worse half (-Rs.5,652 over 17 versus
-Rs.1,427 over 23 for LONG), which is why standing aside beats picking a side. For an
options book both directions are long premium, so "bearish session" is not a licence to
buy puts.

2026-08-24 is the live illustration: NIFTY fell 148 points and the system took five
LONG entries for -Rs.572. The two worst (-Rs.249 and -Rs.171) were both graded A+ at
0.93-0.94 confidence with session.bias reading BEARISH, close_above_vwap at 11.8-14.3%
and the conviction vector reading "Range/Chop" then "Reversal Risk". This rule blocks
exactly those two.

SCOPE HONESTY: measured on the rtracker advisory lane, whose P&L is a mark rather than a
fill. The DIRECTION of the effect held in every time-third; the rupee magnitudes should
not be read as live P&L. HISTORICAL replays are untouched because the gate returns early
in that mode. One setting turns it off.
"""
from types import SimpleNamespace

import pytest

from config.settings import Settings, get_settings
from scanner import Scanner


@pytest.fixture(autouse=True)
def _explicit_experiment(monkeypatch):
    # Behavioral tests exercise the opt-in policy; the default-off contract is
    # asserted independently with Settings(_env_file=None).
    monkeypatch.setattr(get_settings(), "session_bearish_standaside_enabled", True)


def _sc(session_bias, mode="REAL"):
    sc = Scanner.__new__(Scanner)
    sc.mode = mode
    sc._candidate_market_intelligence_context = lambda _c: {
        "session_context": {"bias": session_bias}
    }
    return sc


def _cand(direction="LONG"):
    return SimpleNamespace(
        instrument="NIFTY", timeframe="5min", direction=direction,
        action="ENTRY", inst_type="OPT", option_type="CE",
    )


def test_the_setting_exists_and_is_opt_in():
    assert Settings(_env_file=None).session_bearish_standaside_enabled is False


def test_a_bearish_session_stands_aside_on_the_long_side():
    blocked, why = Scanner._session_bias_standaside(_sc("BEARISH"), _cand("LONG"))
    assert blocked is True
    assert "bearish" in why.lower()


def test_a_bearish_session_stands_aside_on_the_short_side_too():
    """The measured rule is blanket: the SHORT half lost more, not less."""
    blocked, _why = Scanner._session_bias_standaside(_sc("BEARISH"), _cand("SHORT"))
    assert blocked is True


def test_a_neutral_session_is_untouched():
    blocked, _ = Scanner._session_bias_standaside(_sc("NEUTRAL"), _cand("LONG"))
    assert blocked is False


def test_a_bullish_session_is_untouched():
    """Blocking counter-bias both ways was refuted (+Rs.1,904); only BEARISH stands aside."""
    assert Scanner._session_bias_standaside(_sc("BULLISH"), _cand("SHORT"))[0] is False
    assert Scanner._session_bias_standaside(_sc("BULLISH"), _cand("LONG"))[0] is False


def test_a_missing_bias_never_blocks():
    """Never-conclude-from-absence: no intel is not evidence of a bearish session."""
    assert Scanner._session_bias_standaside(_sc(""), _cand("LONG"))[0] is False
    assert Scanner._session_bias_standaside(_sc(None), _cand("LONG"))[0] is False


def test_unavailable_intel_never_blocks():
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc._candidate_market_intelligence_context = lambda _c: None

    assert Scanner._session_bias_standaside(sc, _cand("LONG"))[0] is False


def test_a_raising_context_never_blocks():
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"

    def _boom(_c):
        raise RuntimeError("intel exploded")

    sc._candidate_market_intelligence_context = _boom

    assert Scanner._session_bias_standaside(sc, _cand("LONG"))[0] is False


def test_the_toggle_restores_the_previous_behaviour(monkeypatch):
    monkeypatch.setattr(get_settings(), "session_bearish_standaside_enabled", False)

    assert Scanner._session_bias_standaside(_sc("BEARISH"), _cand("LONG"))[0] is False


def test_the_live_gate_consults_it():
    """The helper is worthless if the entry gate never asks."""
    import inspect

    src = inspect.getsource(Scanner._passes_live_trade_ready_gate)
    assert "_session_bias_standaside" in src
