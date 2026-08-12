"""A winner may not be cut before its post-entry tape has been observed.

restore_point_51 gates the WINNER side of the near-peak threat exit by age, and
names the failure in its own comment:

    # Winner-side minimum age: gap/synthetic marks at the open can
    # manufacture an instant peak that arms this exit on a seconds-old
    # position (2026-07-15; ledger rows exited 1-22s after entry).

    peak_threat_exit_min_age_seconds            45.0
    peak_threat_exit_open_window_min_age_seconds 120.0   # 09:15-09:30

This build kept the LOSS-side hold (peak_threat_exit_min_hold_seconds, 60s) and
lost the winner-side one, so the profit branch could fire on a position seconds
old — an opening mark that manufactures a peak arms the exit before any real
post-entry tape exists.

An age of None means "cannot tell" and must never block, matching
_trade_like_age_seconds' own contract and the loss branch's behaviour.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from scanner import IST, Scanner


def _sc():
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc._clear_loss_threat_confirmation = lambda *_a, **_k: None
    return sc


def _winner(age_seconds, at="11:00"):
    """A trade well past the peak arm, retaining most of it — the profit branch."""
    hh, mm = (int(x) for x in at.split(":"))
    now = datetime.now(IST).replace(tzinfo=None).replace(hour=hh, minute=mm, second=0)
    trade = SimpleNamespace(
        instrument="BANKNIFTY", timeframe="5min", inst_type="OPT",
        option_type="CE", direction="LONG", lots=1, lot_size=30,
        entry_time=None if age_seconds is None else now - timedelta(seconds=age_seconds),
        grade="A", runner_mode=False, volume_ratio=0.0,
    )
    return trade, now


def _decide(sc, trade, now):
    return sc._peak_threat_exit_decision(
        trade,
        current_price=742.0,
        current_pnl_rs=1300.0,
        peak_pnl=1400.0,     # retention 93%, comfortably past min_retention
        initial_risk_rs=820.0,
        latest_time=now,
        regime="VOLATILE",
        inst_type="OPT",
        option_type="CE",
        direction="LONG",
    )


def test_a_seconds_old_winner_is_not_cut():
    """2026-08-12 14:33: a BANKNIFTY winner exited 32s after entry."""
    sc = _sc()
    trade, now = _winner(32)

    fired, _reason, detail = _decide(sc, trade, now)

    assert fired is False
    assert "post-entry tape" in detail


def test_a_winner_past_the_minimum_age_is_judged_normally():
    sc = _sc()
    trade, now = _winner(90)

    _fired, _reason, detail = _decide(sc, trade, now)

    assert "post-entry tape" not in detail


def test_the_opening_window_demands_longer():
    """Gap and synthetic marks at the open manufacture instant peaks."""
    sc = _sc()
    trade, now = _winner(60, at="09:20")

    fired, _reason, detail = _decide(sc, trade, now)

    assert fired is False
    assert "post-entry tape" in detail
    assert "120" in detail


def test_after_the_opening_window_the_base_minimum_applies():
    sc = _sc()
    trade, now = _winner(60, at="09:45")

    _fired, _reason, detail = _decide(sc, trade, now)

    assert "post-entry tape" not in detail


def test_an_unknowable_age_never_blocks():
    """None means "cannot tell". Refusing on it would disarm the exit for any row
    without an entry stamp — the never-conclude-from-absence rule."""
    sc = _sc()
    trade, now = _winner(None)

    _fired, _reason, detail = _decide(sc, trade, now)

    assert "post-entry tape" not in detail


def test_the_toggle_restores_the_previous_behaviour(monkeypatch):
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "peak_threat_exit_min_age_enabled", False)
    sc = _sc()
    trade, now = _winner(5)

    _fired, _reason, detail = _decide(sc, trade, now)

    assert "post-entry tape" not in detail


def test_the_loss_side_is_untouched():
    """The loss branch keeps its own hold; this port only adds the winner side."""
    sc = _sc()
    trade, now = _winner(5)

    fired, _reason, detail = sc._peak_threat_exit_decision(
        trade,
        current_price=690.0,
        current_pnl_rs=-400.0,
        peak_pnl=0.0,
        initial_risk_rs=820.0,
        latest_time=now,
        regime="VOLATILE",
        inst_type="OPT",
        option_type="CE",
        direction="LONG",
    )

    assert fired is False
    assert "post-entry tape" not in detail


@pytest.mark.parametrize("settings_name,default", [
    ("peak_threat_exit_min_age_seconds", 45.0),
    ("peak_threat_exit_open_window_min_age_seconds", 120.0),
])
def test_the_ported_defaults_match_the_old_build(settings_name, default):
    from config.settings import get_settings

    assert float(getattr(get_settings(), settings_name)) == pytest.approx(default)
