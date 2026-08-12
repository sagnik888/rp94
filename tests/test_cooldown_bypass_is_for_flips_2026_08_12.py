"""The trend-tape cooldown bypass exists for FLIPS, but never checked direction.

_index_cooldown_applies: "UT1 is a flip engine - after an opposite-signal exit
the reverse entry IS the trade, so a blanket cooldown eats reversals." Correct
for a reversal. But the bypass was granted on regime alone, so it equally
excused re-entering the SAME direction that had just lost.

2026-08-12, NIFTY, regime TRENDING_DOWN the whole time:

    13:26:20  BUY CE (LONG)              -> 13:27:35 EARLY_LOSS_CUT  -Rs.424
    13:31:04  BUY CE (LONG) again        -> 13:39:37 EARLY_LOSS_CUT  -Rs.414

3m29s apart with index_cooldown_minutes at 4.0. Not a flip - the same
counter-trend bet repeated. Six of the nine losing exits that session were
repeats of two strikes.

A genuine same-direction re-entry is not lost: it still reaches
_stop_out_reentry_allowed, which asks whether the trend is actually still
running rather than assuming it from the regime label.
"""
from types import SimpleNamespace

import pytest

from scanner import Scanner


def _scanner(regime="TRENDING_DOWN", last_direction=None, day_type="TREND_DAY_DOWN"):
    sc = Scanner.__new__(Scanner)
    sc.latest_regimes = {"NIFTY": regime}
    sc._current_day_type = lambda _base: day_type
    sc._last_exit_direction = {}
    if last_direction:
        sc._last_exit_direction["NIFTY"] = last_direction
    return sc


def test_trend_tape_still_skips_the_cooldown_for_a_real_flip():
    """Exited LONG, now entering SHORT: this is the reversal the bypass is for."""
    sc = _scanner(last_direction="LONG")

    applies, basis = sc._index_cooldown_applies("NIFTY", "SHORT")

    assert applies is False
    assert "trend/normal tape" in basis


def test_the_same_direction_re_entry_that_lost_twice_is_held():
    """Today's 13:26 -> 13:31 NIFTY sequence: LONG, lost, LONG again."""
    sc = _scanner(last_direction="LONG")

    applies, basis = sc._index_cooldown_applies("NIFTY", "LONG")

    assert applies is True
    assert "same-direction" in basis


def test_chop_still_gates_everything_including_flips():
    sc = _scanner(regime="CHOPPY", last_direction="LONG")

    applies, basis = sc._index_cooldown_applies("NIFTY", "SHORT")

    assert applies is True
    assert "choppy regime" in basis


def test_a_range_day_still_gates_everything():
    sc = _scanner(regime="TRENDING_DOWN", last_direction="LONG", day_type="RANGE_DAY")

    applies, basis = sc._index_cooldown_applies("NIFTY", "SHORT")

    assert applies is True
    assert "range-day mood" in basis


def test_no_recorded_direction_behaves_as_before():
    """First trade of the session on an index: nothing to compare against."""
    sc = _scanner(last_direction=None)

    applies, _basis = sc._index_cooldown_applies("NIFTY", "LONG")

    assert applies is False


@pytest.mark.parametrize("direction", ["", None])
def test_an_unknown_proposed_direction_behaves_as_before(direction):
    sc = _scanner(last_direction="LONG")

    applies, _basis = sc._index_cooldown_applies("NIFTY", direction)

    assert applies is False


def test_the_legacy_blanket_mode_is_untouched(monkeypatch):
    from config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "index_cooldown_regime_scoped", False)
    sc = _scanner(last_direction="LONG")

    applies, basis = sc._index_cooldown_applies("NIFTY", "SHORT")

    assert applies is True
    assert "legacy blanket cooldown" in basis


def test_exit_direction_is_recorded_for_the_comparison():
    sc = Scanner.__new__(Scanner)
    sc._last_exit_direction = {}

    sc._note_index_exit_direction("NIFTY", SimpleNamespace(direction="LONG"))
    assert sc._last_exit_direction["NIFTY"] == "LONG"

    # An instrument string carrying a contract suffix keys on the index.
    sc._note_index_exit_direction("SENSEX 77500CE", SimpleNamespace(direction="SHORT"))
    assert sc._last_exit_direction["SENSEX"] == "SHORT"


def test_recording_a_junk_direction_never_raises_or_poisons_the_map():
    sc = Scanner.__new__(Scanner)
    sc._last_exit_direction = {}

    sc._note_index_exit_direction("NIFTY", SimpleNamespace(direction="SIDEWAYS"))
    sc._note_index_exit_direction("NIFTY", None)
    sc._note_index_exit_direction("NIFTY", SimpleNamespace())

    assert sc._last_exit_direction == {}
