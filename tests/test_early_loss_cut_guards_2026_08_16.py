"""EARLY_LOSS_CUT was the only major exit with no cost floor, no minimum hold, and no
deferral to the profit side.

Measured over 19 sessions (20 cuts; the two stale-mark trades are excluded throughout,
they were fixed on 2026-08-11): 18 cuts for -Rs.9,108.

The exit is NOT the problem people assume from that headline. On the 9 rows carrying a
measurable stop it lost Rs.3,455 where running to the full stop would have cost Rs.7,710
— it SAVES about Rs.473 a trade. So its R thresholds are deliberately left alone; there
is no evidence to move them, and the counterfactual (would a cut trade have recovered?)
is not in the data because only cut trades are observed. The pnl_path recording added on
2026-08-11 is what will eventually answer that.

What it genuinely lacked was the guards its siblings already carry:

1. A minimum hold. p10 was 2.0 seconds; 5 of 18 fired inside 15s. Kept SHORT on purpose:
   this exit exists to be fast, so LOSS_THREAT_EXIT's 60s would disable it, not guard it.

2. Deferral once a trade has been green. 7 of 18 had peaked POSITIVE — one to Rs.348 —
   before being booked as an "early loss". That is a give-back, and the profit lock
   prices it off the peak instead of off entry. It never got the chance, because the two
   monitor paths disagreed about order:
       path A   loss guard -> profit lock -> giveback
       path B   profit lock -> loss guard -> giveback
   In path A a `continue` after the cut meant the lock was never reached. Putting the
   deferral INSIDE _loss_guard_decision fixes both orders at once rather than shuffling
   two call sites and hoping they stay in step.

3. A cost floor, on the same principle as every other discretionary exit.

The hard stop is untouched, so worst case per trade is exactly what it was.
"""
import datetime as dt
import inspect
from types import SimpleNamespace

import pytest
import pytz

from config.settings import Settings, get_settings
from scanner import Scanner

IST = pytz.timezone("Asia/Kolkata")
ENTRY = IST.localize(dt.datetime(2026, 8, 14, 12, 37, 25))


def _sc(mode="LIVE"):
    sc = Scanner.__new__(Scanner)
    sc.mode = mode
    return sc


def _trade(peak=0.0, held_s=600.0, **kw):
    base = dict(
        instrument="BANKNIFTY", timeframe="5min", inst_type="OPT", direction="SHORT",
        entry_timestamp=ENTRY, peak_pnl=peak, scalp_lock_mode=False,
        lots=1, lot_size=35, entry_price=361.50, current_price=354.45,
    )
    base.update(kw)
    return SimpleNamespace(**base), ENTRY + dt.timedelta(seconds=held_s)


def _decide(sc, trade, now, pnl=-900.0, risk=1000.0):
    return sc._loss_guard_decision(
        trade, current_pnl_rs=pnl, initial_risk_rs=risk,
        latest_time=now, regime="RANGING", current_price=354.45,
    )


# ── 1. minimum hold ──────────────────────────────────────────────────────────

def test_a_two_second_old_trade_is_not_cut():
    """p10 of the measured cohort. -Rs.900 on a 1,000 risk is 0.9R, past every
    threshold — the only thing standing between it and an exit is the hold."""
    sc = _sc()
    trade, now = _trade(held_s=2.0)
    cut, detail, _ = _decide(sc, trade, now)
    assert cut is False
    assert "under the 15s minimum" in detail


def test_a_matured_trade_is_still_cut():
    sc = _sc()
    trade, now = _trade(held_s=600.0)
    cut, _, _ = _decide(sc, trade, now)
    assert cut is True


def test_an_unknown_age_does_not_disarm_the_exit():
    """The 2026-08-10 rule. A row with no entry stamp must not block the guard
    outright, or a missing field silently switches the exit off."""
    sc = _sc()
    trade, now = _trade(held_s=600.0)
    del trade.entry_timestamp
    cut, _, _ = _decide(sc, trade, now)
    assert cut is True


def test_the_hold_falls_back_to_the_wall_clock_when_live():
    """latest_time is the CANDLE clock; an intracandle entry is stamped after the bar
    it fired inside, so the helper refuses to invent an age and the hold would never
    apply. Same defect, same fix, as the loss-threat hold."""
    src = inspect.getsource(Scanner._loss_guard_decision)
    idx = src.index("early_loss_cut_min_hold_seconds")
    assert "datetime.now(IST)" in src[idx:idx + 900]
    assert "HISTORICAL" in src[idx:idx + 900]


def test_a_zero_hold_restores_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(get_settings(), "early_loss_cut_min_hold_seconds", 0.0)
    sc = _sc()
    trade, now = _trade(held_s=2.0)
    cut, _, _ = _decide(sc, trade, now)
    assert cut is True


# ── 2. a green trade belongs to the profit lock ──────────────────────────────

def test_a_trade_that_peaked_past_the_lock_arm_is_deferred():
    """The 2026-08-12 MIDCPNIFTY case: peaked Rs.348, booked -Rs.356 as an 'early
    loss'. The lock would have priced it off the peak."""
    sc = _sc()
    trade, now = _trade(peak=900.0)
    cut, detail, _ = _decide(sc, trade, now)
    assert cut is False
    assert "give-back belongs to the profit lock" in detail


def test_a_never_green_trade_is_still_cut():
    """The deferral must not become a blanket exemption — 11 of the 18 never went
    green at all, and those are the cases the exit exists for."""
    sc = _sc()
    trade, now = _trade(peak=0.0)
    cut, _, _ = _decide(sc, trade, now)
    assert cut is True


def test_a_peak_too_small_to_arm_the_lock_is_still_cut():
    """Deferring to a lock that cannot arm would strand the trade with no exit."""
    sc = _sc()
    trade, now = _trade(peak=1.0)
    cut, _, _ = _decide(sc, trade, now)
    assert cut is True


def test_the_deferral_is_toggleable(monkeypatch):
    monkeypatch.setattr(get_settings(), "early_loss_cut_defer_to_profit_lock_enabled", False)
    sc = _sc()
    trade, now = _trade(peak=900.0)
    cut, _, _ = _decide(sc, trade, now)
    assert cut is True


# ── 3. cost floor ────────────────────────────────────────────────────────────

# The risk is set BELOW the cost deliberately, so the loss clears the R threshold
# (0.9*cost against 0.5*cost of risk = 1.8R, past every cut_r) and the floor is the
# only thing that can refuse it. The first draft used risk=cost, which put the loss at
# 0.5R — blocked by the R arithmetic, so the test passed without the floor existing.
_FLOOR_PNL_MULT = 0.9
_FLOOR_RISK_MULT = 0.5


def test_a_loss_under_the_round_trip_is_not_cut():
    sc = _sc()
    trade, now = _trade()
    cost = float(sc._round_trip_cost_rs(trade) or 0.0)
    assert cost > 0, "no cost model configured — the floor test proves nothing"
    cut, detail, _ = _decide(sc, trade, now,
                             pnl=-(cost * _FLOOR_PNL_MULT), risk=cost * _FLOOR_RISK_MULT)
    assert cut is False
    assert "round trip" in detail


def test_the_cost_floor_is_toggleable(monkeypatch):
    """Same loss, floor off: the R arithmetic alone would have cut it at 1.8R."""
    monkeypatch.setattr(get_settings(), "early_loss_cut_cost_floor_enabled", False)
    sc = _sc()
    trade, now = _trade()
    cost = float(sc._round_trip_cost_rs(trade) or 0.0)
    cut, _, _ = _decide(sc, trade, now,
                        pnl=-(cost * _FLOOR_PNL_MULT), risk=cost * _FLOOR_RISK_MULT)
    assert cut is True


# ── scope: what must NOT have changed ────────────────────────────────────────

def test_the_r_thresholds_are_untouched():
    """The exit saves Rs.473/trade against the full stop. Nothing measured justifies
    moving these, and the recovery counterfactual is not observable yet."""
    f = Settings.model_fields
    assert f["fut_loss_cut_r"].default == 0.8
    assert f["opt_loss_cut_r"].default == 0.8
    assert f["loss_guard_intel_min_r"].default == 0.35
    assert f["loss_guard_soft_intel_r"].default == 0.55


def test_the_hold_is_far_shorter_than_the_loss_threat_hold():
    """This exit's value IS its speed; borrowing the 60s would disable it."""
    f = Settings.model_fields
    assert f["early_loss_cut_min_hold_seconds"].default == 15.0
    assert f["peak_threat_exit_min_hold_seconds"].default == 60.0


def test_winners_are_still_idle():
    sc = _sc()
    trade, now = _trade()
    cut, detail, _ = _decide(sc, trade, now, pnl=250.0)
    assert cut is False and "idle" in detail


def test_the_guards_sit_before_the_r_arithmetic():
    """They must cover the intel-accelerated path (0.55R/0.35R) too — that path does
    most of the firing, at a measured median 0.45R against a 0.8R base."""
    src = inspect.getsource(Scanner._loss_guard_decision)
    assert src.index("early_loss_cut_min_hold_seconds") < src.index("cut_r_from_severity")
    assert src.index("early_loss_cut_cost_floor_enabled") < src.index("cut_r_from_severity")


def test_both_monitor_paths_inherit_the_guards():
    """The ordering split is why these live in the decision, not at a call site."""
    src = inspect.getsource(Scanner)
    assert src.count("self._loss_guard_decision(") == 2, "call-site count changed"
