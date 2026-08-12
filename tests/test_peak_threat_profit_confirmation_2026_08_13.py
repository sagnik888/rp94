"""A winner exit must survive two observations, as the loss exit already does.

The loss branch of the near-peak guard builds a confirmation latch: the same
evidence signature has to be seen twice, across an advancing observation clock,
before LOSS_THREAT_EXIT fires. The profit branch had no such requirement — a
single tick that scored high enough cut the winner outright.

restore_point_51 confirms BOTH sides through one helper:

    profit_confirm_attr = "_peak_threat_confirmation"
    peak_threat_exit_confirm_seconds      5.0   (winner side)
    peak_threat_exit_loss_confirm_seconds 3.0   (loss side, already here)

Counting calls would manufacture persistence — scanner cycles can run several
times against the same market timestamp — so the latch advances only when the
observation clock advances, and is dropped on every path that stops short of an
exit so it can never be assembled from observations taken minutes apart.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from scanner import IST, Scanner


def _sc():
    return Scanner.__new__(Scanner)


def _trade():
    return SimpleNamespace(instrument="BANKNIFTY", timeframe="5min")


NOW = datetime(2026, 8, 13, 11, 0, 0)


def test_the_first_observation_never_fires():
    sc, trade = _sc(), _trade()

    ready, detail = sc._exit_confirmation_ready(trade, "_peak_threat_confirmation", ("price",), NOW, 5.0)

    assert ready is False
    assert "1/2" in detail


def test_a_second_observation_after_the_interval_confirms():
    sc, trade = _sc(), _trade()

    sc._exit_confirmation_ready(trade, "_peak_threat_confirmation", ("price",), NOW, 5.0)
    ready, detail = sc._exit_confirmation_ready(
        trade, "_peak_threat_confirmation", ("price",), NOW + timedelta(seconds=6), 5.0
    )

    assert ready is True
    assert "confirmed" in detail


def test_a_second_observation_inside_the_interval_does_not_confirm():
    sc, trade = _sc(), _trade()

    sc._exit_confirmation_ready(trade, "_peak_threat_confirmation", ("price",), NOW, 5.0)
    ready, _detail = sc._exit_confirmation_ready(
        trade, "_peak_threat_confirmation", ("price",), NOW + timedelta(seconds=2), 5.0
    )

    assert ready is False


def test_repeated_calls_on_one_timestamp_cannot_manufacture_persistence():
    """Scanner cycles run several times against the same market timestamp."""
    sc, trade = _sc(), _trade()

    for _ in range(20):
        ready, _detail = sc._exit_confirmation_ready(
            trade, "_peak_threat_confirmation", ("price",), NOW, 5.0
        )

    assert ready is False


def test_a_changed_signature_restarts_the_confirmation():
    """Different evidence is a different claim, not a second sighting of the same one."""
    sc, trade = _sc(), _trade()

    sc._exit_confirmation_ready(trade, "_peak_threat_confirmation", ("price",), NOW, 5.0)
    ready, detail = sc._exit_confirmation_ready(
        trade, "_peak_threat_confirmation", ("regime",), NOW + timedelta(seconds=30), 5.0
    )

    assert ready is False
    assert "1/2" in detail


def test_confirming_clears_the_latch_so_the_next_claim_starts_fresh():
    sc, trade = _sc(), _trade()

    sc._exit_confirmation_ready(trade, "_peak_threat_confirmation", ("price",), NOW, 5.0)
    sc._exit_confirmation_ready(
        trade, "_peak_threat_confirmation", ("price",), NOW + timedelta(seconds=6), 5.0
    )

    assert getattr(trade, "_peak_threat_confirmation", None) is None


def test_clearing_is_safe_on_anything():
    sc = _sc()
    sc._clear_exit_confirmation(_trade(), "_peak_threat_confirmation")
    sc._clear_exit_confirmation(object(), "_peak_threat_confirmation")  # must not raise


def test_an_unusable_clock_clears_rather_than_confirms():
    sc, trade = _sc(), _trade()
    trade._peak_threat_confirmation = {"signature": ["price"], "first_at": 0.0, "last_at": 0.0}

    ready, detail = sc._exit_confirmation_ready(
        trade, "_peak_threat_confirmation", ("price",), "not-a-clock", 5.0
    )

    assert ready is False
    assert "clock" in detail
    assert getattr(trade, "_peak_threat_confirmation", None) is None


# ── wired into the winner branch ─────────────────────────────────────────────

def _winner_scanner():
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc._clear_loss_threat_confirmation = lambda *_a, **_k: None
    return sc


def _winner_trade(age_seconds=600):
    return SimpleNamespace(
        instrument="BANKNIFTY", timeframe="5min", inst_type="OPT",
        option_type="CE", direction="LONG", lots=1, lot_size=30,
        entry_time=NOW - timedelta(seconds=age_seconds),
        grade="A", runner_mode=False, volume_ratio=0.0,
    )


def _decide(sc, trade, at):
    return sc._peak_threat_exit_decision(
        trade,
        current_price=742.0,
        current_pnl_rs=1300.0,
        peak_pnl=1400.0,
        initial_risk_rs=820.0,
        latest_time=at,
        regime="VOLATILE",
        inst_type="OPT",
        option_type="CE",
        direction="LONG",
    )


def test_a_winner_is_not_cut_on_a_single_reading():
    sc, trade = _winner_scanner(), _winner_trade()

    fired, _reason, _detail = _decide(sc, trade, NOW)

    assert fired is False


def test_the_settings_default_matches_the_old_build():
    from config.settings import get_settings

    assert float(getattr(get_settings(), "peak_threat_exit_confirm_seconds")) == pytest.approx(5.0)
