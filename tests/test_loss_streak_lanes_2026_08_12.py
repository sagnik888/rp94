"""A loss streak belongs to an index AND a timeframe, not to an index alone.

This build pools every timeframe for an index into one counter, so two losses on
NIFTY 5min and two on NIFTY 15min read as a run of four and trip the suspension
threshold — even though neither chart lost four in a row. restore_point_51 keeps
a lane per (index, timeframe) and takes the max across lanes for that index:

    def _refresh_loss_streak_aggregate(self, instrument):
        current = max([v for (idx, _tf), v in lane_streaks.items() if idx == base] or [0])
        self._losses_today[base] = current

"A win resets only its own lane. Other indices/timeframes neither extend nor
erase this lane's streak."

On 2026-08-12 the pooled counter reached four on NIFTY and SENSEX and, before
7ae2e7b made the suspension self-clearing, locked both out of the 14:15 reversal.
Lanes make the threshold mean what an operator watching one chart thinks it
means.
"""
import pytest

from scanner import Scanner


def _sc():
    sc = Scanner.__new__(Scanner)
    sc._losses_today = {}
    sc._loss_streaks_by_lane = {}
    sc._last_loss_outcome_instrument = ""
    return sc


def _loss(sc, instrument, timeframe):
    return sc._record_consecutive_loss_outcome(instrument, -100.0, timeframe)


def _win(sc, instrument, timeframe):
    return sc._record_consecutive_loss_outcome(instrument, 250.0, timeframe)


def test_two_lanes_of_two_losses_are_not_a_run_of_four():
    """The exact shape that tripped the threshold on 2026-08-12."""
    sc = _sc()

    _loss(sc, "NIFTY", "5min")
    _loss(sc, "NIFTY", "5min")
    _loss(sc, "NIFTY", "15min")
    _loss(sc, "NIFTY", "15min")

    assert sc._loss_streaks_by_lane[("NIFTY", "5min")] == 2
    assert sc._loss_streaks_by_lane[("NIFTY", "15min")] == 2
    assert sc._losses_today["NIFTY"] == 2


def test_four_losses_in_one_lane_still_reach_the_threshold():
    sc = _sc()

    for _ in range(4):
        _loss(sc, "NIFTY", "15min")

    assert sc._losses_today["NIFTY"] == 4


def test_the_aggregate_is_the_worst_lane():
    sc = _sc()

    _loss(sc, "NIFTY", "5min")
    for _ in range(3):
        _loss(sc, "NIFTY", "15min")

    assert sc._losses_today["NIFTY"] == 3


def test_a_win_resets_only_its_own_lane():
    sc = _sc()

    for _ in range(3):
        _loss(sc, "NIFTY", "15min")
    _loss(sc, "NIFTY", "5min")

    _win(sc, "NIFTY", "5min")

    assert sc._loss_streaks_by_lane[("NIFTY", "5min")] == 0
    assert sc._loss_streaks_by_lane[("NIFTY", "15min")] == 3
    assert sc._losses_today["NIFTY"] == 3


def test_another_index_neither_extends_nor_erases_this_lane():
    sc = _sc()

    for _ in range(3):
        _loss(sc, "NIFTY", "15min")
    _win(sc, "SENSEX", "15min")
    _loss(sc, "BANKNIFTY", "5min")

    assert sc._losses_today["NIFTY"] == 3


def test_a_missing_timeframe_still_records_a_lane():
    """Callers that cannot supply one share a single 'unknown' lane."""
    sc = _sc()

    _loss(sc, "NIFTY", "")
    _loss(sc, "NIFTY", "")

    assert sc._loss_streaks_by_lane[("NIFTY", "unknown")] == 2
    assert sc._losses_today["NIFTY"] == 2


def test_a_blank_instrument_is_ignored_rather_than_raising():
    sc = _sc()

    assert sc._record_consecutive_loss_outcome("", -100.0, "5min") == 0
    assert sc._losses_today == {}


def test_the_contract_symbol_keys_on_the_index():
    sc = _sc()

    _loss(sc, "NIFTY 24250CE", "15min")
    _loss(sc, "NIFTY", "15min")

    assert sc._losses_today["NIFTY"] == 2


def test_the_suspension_clears_every_lane_for_that_index():
    """_can_trade_instrument consumes the streak at index level, so the lanes it
    was computed from have to go with it — otherwise the next recorded outcome
    recomputes the aggregate straight back up."""
    from datetime import datetime

    from scanner import IST

    sc = _sc()
    for _ in range(4):
        _loss(sc, "NIFTY", "15min")
    sc._reset_daily_counters = lambda: None
    sc._trades_today = {"NIFTY": 1}
    sc.max_consecutive_losses = 4
    sc._dynamic_trade_budget = lambda _i: (20, "test")
    sc._index_cooldown_applies = lambda *_a: (False, "")
    sc._last_exit_time = {}
    sc._post_loss_cooldown_block = lambda *_a, **_k: (False, "")
    sc.index_cooldown_minutes = 4.0

    blocked, _reason = sc._can_trade_instrument("NIFTY", datetime.now(IST).replace(tzinfo=None))

    assert blocked is False
    assert sc._losses_today["NIFTY"] == 0
    assert all(v == 0 for (idx, _tf), v in sc._loss_streaks_by_lane.items() if idx == "NIFTY")

    # And a later outcome starts a fresh run rather than restoring the old one.
    _loss(sc, "NIFTY", "15min")
    assert sc._losses_today["NIFTY"] == 1


def test_global_tape_mode_still_collapses_lanes(monkeypatch):
    """consecutive_loss_streak_per_index=False keeps its documented behaviour."""
    from config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "consecutive_loss_streak_per_index", False)
    sc = _sc()

    _loss(sc, "NIFTY", "15min")
    _loss(sc, "SENSEX", "15min")

    assert sc._losses_today["NIFTY"] == 0
    assert sc._losses_today["SENSEX"] == 1
