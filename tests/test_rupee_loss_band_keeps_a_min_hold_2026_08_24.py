"""The rupee-loss-band guard is the last exit with no clock at all. Give it one.

Every other loss-side exit grew a minimum hold once seconds-old cuts were measured:

    LOSS_THREAT_EXIT      60s   peak_threat_exit_min_hold_seconds
    EARLY_LOSS_CUT        15s   early_loss_cut_min_hold_seconds
    PEAK_THREAT (winner)  45s / 120s in the opening window

`_rupee_loss_band_guard_decision` has none — grep the whole function for min_hold,
_trade_like_age_seconds, held, age_seconds or datetime.now and there is not one hit,
even though it already RECEIVES `latest_time` as a parameter. Its CATASTROPHE branch
(`if loss >= force_loss`) short-circuits ahead of every freefall and slow-bleed
evidence test, so it fires the instant the rupee threshold is crossed.

That is how a trade exited 2.59 SECONDS after entry on a premium that had "collapsed"
46.6% — a stale entry mark, not a real move — for -Rs.4,695. It is the single largest
loss in the post-2026-08-12 book and 20.6% of that period's entire loser pot, from one
trade.

The hold is the same 15s the early loss cut uses, and for the same reason: this exit
exists to be fast, so LOSS_THREAT's 60s would disable it rather than guard it. The rupee
thresholds are untouched, so worst case per trade is what it was, bounded by
rupee_loss_band_force_loss_rs_opt (Rs.3,000/lot) for the 15 seconds it now waits.

Absence never blocks, and a replay keeps the candle answer — the same rules the two
sibling holds follow.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytz

from config.settings import Settings, get_settings
from scanner import IST, Scanner

TZ = pytz.timezone("Asia/Kolkata")


def _sc(mode="REAL"):
    sc = Scanner.__new__(Scanner)
    sc.mode = mode
    return sc


def _trade(age_seconds):
    entry = (
        None if age_seconds is None
        else datetime.now(IST).replace(tzinfo=None) - timedelta(seconds=age_seconds)
    )
    return SimpleNamespace(
        instrument="BANKNIFTY", timeframe="5min", inst_type="OPT", option_type="CE",
        direction="LONG", lots=1, lot_size=30, entry_time=entry, grade="A",
        entry_price=152.40, initial_stop=140.0, runner_mode=False,
    )


def _decide(sc, trade, pnl=-9000.0, latest_time=None):
    """A loss far past any force threshold, so only the hold can stop it."""
    return sc._rupee_loss_band_guard_decision(
        trade,
        current_pnl_rs=pnl,
        latest_time=latest_time,
        regime="VOLATILE",
        current_price=81.40,
    )


def test_the_setting_exists_and_matches_the_early_loss_cut():
    s = Settings()
    assert s.rupee_loss_band_min_hold_seconds == 15.0
    assert s.rupee_loss_band_min_hold_seconds == s.early_loss_cut_min_hold_seconds


def test_a_two_second_old_trade_is_not_force_exited():
    """The 2026-08-18 shape: -Rs.4,695 booked 2.59s after entry on a stale mark."""
    fired, reason, _detail = _decide(_sc(), _trade(2.59))

    assert fired is False
    assert reason != "RUPEE_LOSS_BAND_EXIT"


def test_a_trade_past_the_hold_is_force_exited_normally():
    fired, reason, _detail = _decide(_sc(), _trade(60))

    assert fired is True
    assert reason == "RUPEE_LOSS_BAND_EXIT"


def test_the_boundary_is_the_configured_hold():
    hold = float(get_settings().rupee_loss_band_min_hold_seconds)
    assert _decide(_sc(), _trade(hold - 2))[0] is False
    assert _decide(_sc(), _trade(hold + 2))[0] is True


def test_the_candle_clock_does_not_disarm_the_hold():
    """Same intracandle trap the winner-side guard had: bar stamp behind the fill."""
    trade = _trade(3)
    candle_clock = trade.entry_time - timedelta(seconds=60)

    assert Scanner._trade_like_age_seconds(trade, candle_clock) is None
    assert _decide(_sc(), trade, latest_time=candle_clock)[0] is False


def test_a_replay_keeps_the_candle_answer():
    """In HISTORICAL "now" is meaningless, exactly as the sibling holds say."""
    trade = _trade(3)
    candle_clock = trade.entry_time - timedelta(seconds=60)

    assert _decide(_sc(mode="HISTORICAL"), trade, latest_time=candle_clock)[0] is True


def test_an_unknown_entry_stamp_never_blocks():
    assert _decide(_sc(), _trade(None))[0] is True


def test_disabling_the_hold_restores_the_previous_behaviour(monkeypatch):
    monkeypatch.setattr(get_settings(), "rupee_loss_band_min_hold_seconds", 0.0)

    assert _decide(_sc(), _trade(1))[0] is True
