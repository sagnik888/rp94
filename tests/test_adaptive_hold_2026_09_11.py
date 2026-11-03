"""Adaptive hold: never-green cut, then a wide ATR trail that stands profit exits down.

OFF by default because its walk-forward lost more than the current exits at
introduction (engine/adaptive_hold.py). These tests pin the mechanics the operator asked
for, and the two limits they set: nothing changes unless it is switched on, and it never
holds a trade into end-of-day handling or past the 15:15 force exit.
"""
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from engine.adaptive_hold import (
    AdaptiveHoldParams,
    decide,
    momentum_aligned,
    true_range_atr,
)
from engine.exit_cycle import ExitCycleService

P = AdaptiveHoldParams()


def _decide(**kw):
    base = dict(
        entry=100.0, initial_stop=90.0, peak_premium=100.0, current_premium=100.0,
        minutes_held=1.0, premium_atr=2.0, momentum=False, breakeven=101.0, params=P,
        active=False, never_green_checked=False, prior_stop=0.0,
    )
    base.update(kw)
    return decide(**base)


# ---- the pure rule --------------------------------------------------------------

def test_a_trade_that_never_turned_green_is_cut_at_five_minutes():
    d = _decide(peak_premium=101.0, current_premium=97.0, minutes_held=5.0)
    assert d.action == "EXIT" and d.reason == "ADAPTIVE_NEVER_GREEN"


def test_not_before_five_minutes():
    assert _decide(peak_premium=100.5, minutes_held=4.9).action == "NONE"


def test_the_never_green_check_is_one_shot():
    first = _decide(peak_premium=102.0, current_premium=99.0, minutes_held=5.0)
    assert first.action == "NONE" and first.never_green_checked, "1.5R-risk trade that went green"
    later = _decide(peak_premium=102.0, current_premium=99.0, minutes_held=9.0, never_green_checked=True)
    assert later.action == "NONE", "a trade that passed the check was cut on a later cycle"


def test_it_engages_at_half_r_and_trails_k_atr_behind_the_peak():
    d = _decide(peak_premium=110.0, current_premium=109.0, minutes_held=8.0)
    assert d.action == "HOLD"
    assert d.stop == pytest.approx(110.0 - 3.5 * 2.0), "peak - k x ATR"


def test_the_trail_never_sits_below_breakeven_after_costs():
    d = _decide(peak_premium=105.0, current_premium=104.5, minutes_held=8.0, premium_atr=5.0)
    assert d.action == "HOLD" and d.stop == pytest.approx(101.0)


def test_an_extending_index_gives_the_move_more_room():
    tight = _decide(peak_premium=120.0, current_premium=118.0, minutes_held=8.0)
    wide = _decide(peak_premium=120.0, current_premium=118.0, minutes_held=8.0, momentum=True)
    assert wide.stop == pytest.approx(120.0 - 5.0 * 2.0)
    assert wide.stop < tight.stop, "momentum must widen the trail, i.e. hold longer"


def test_the_stop_only_ratchets_up():
    d = _decide(peak_premium=120.0, current_premium=118.0, minutes_held=8.0, momentum=True,
                active=True, prior_stop=113.0)
    assert d.stop == pytest.approx(113.0)


def test_a_breach_of_the_trail_exits():
    d = _decide(peak_premium=120.0, current_premium=112.9, minutes_held=20.0, active=True)
    assert d.action == "EXIT" and d.reason == "ADAPTIVE_TRAIL_STOP"


def test_no_risk_basis_means_no_decision():
    assert _decide(initial_stop=0.0).action == "NONE"


def test_atr_and_momentum_helpers():
    highs = [10, 11, 12, 13]
    lows = [9, 10, 11, 12]
    closes = [9.5, 10.5, 11.5, 12.5]
    assert true_range_atr(highs, lows, closes, 14) == pytest.approx(1.5)
    flat_h = [10.0] * 15 + [10.5, 10.2, 10.1]
    flat_l = [9.0] * 15 + [9.5, 9.4, 9.3]
    assert momentum_aligned(flat_h, flat_l, "LONG") is True
    assert momentum_aligned(flat_h, flat_l, "SHORT") is False


# ---- the live wiring --------------------------------------------------------------

class _Candles:
    def __init__(self, frame):
        self.frame = frame

    def get_candles(self, instrument, timeframe):
        return self.frame


def _spot_frame(extending=False):
    ts = pd.date_range("2026-09-11 13:00", periods=25, freq="1min")
    highs = [23400.0 + (i * 4 if extending else 0) for i in range(25)]
    return pd.DataFrame({
        "timestamp": ts,
        "high": [h + 6 for h in highs],
        "low": [h - 6 for h in highs],
        "close": highs,
    })


def _scanner(monkeypatch, enabled=True, extending=False):
    import scanner as scanner_module
    from scanner import Scanner

    s = Scanner.__new__(Scanner)
    s.candles = _Candles(_spot_frame(extending))
    s.events = []
    s.log_event = lambda message, category="info": s.events.append(message)
    settings = SimpleNamespace(
        adaptive_hold_enabled=enabled, adaptive_hold_never_green_minutes=5.0,
        adaptive_hold_never_green_min_r=0.15, adaptive_hold_activate_r=0.5,
        adaptive_hold_trail_atr_mult=3.5, adaptive_hold_momentum_widen_atr=1.5,
        adaptive_hold_atr_period=14, opt_cost=80.0, ut_force_exit_time="15:15",
        eod_protect_window_minutes=14.0,
    )
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
    return s


def _trade(**kw):
    base = dict(
        instrument="NIFTY", timeframe="5min", direction="LONG", inst_type="OPT",
        option_type="CE", entry_price=100.0, initial_stop=90.0, quantity=65,
        pnl_quantity=65, peak_pnl=0.0, target=115.0, instrument_multiplier=0.5,
        entry_time=datetime(2026, 9, 11, 13, 20),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_off_by_default_changes_nothing(monkeypatch):
    from config.settings import Settings

    assert Settings.model_fields["adaptive_hold_enabled"].default is False
    s = _scanner(monkeypatch, enabled=False)
    t = _trade()
    d = s._adaptive_hold_step(t, latest_time=datetime(2026, 9, 11, 13, 40), premium=97.0)
    assert d.action == "NONE"
    assert not hasattr(t, "adaptive_hold_active") and t.target == 115.0


def test_live_never_green_cut(monkeypatch):
    s = _scanner(monkeypatch)
    d = s._adaptive_hold_step(_trade(), latest_time=datetime(2026, 9, 11, 13, 26), premium=97.0)
    assert d.action == "EXIT" and d.reason == "ADAPTIVE_NEVER_GREEN"


def test_live_engagement_removes_the_fixed_target(monkeypatch):
    s = _scanner(monkeypatch)
    t = _trade(peak_pnl=65 * 8.0)  # +8 premium = 0.8R
    d = s._adaptive_hold_step(t, latest_time=datetime(2026, 9, 11, 13, 30), premium=107.0)
    assert d.action == "HOLD"
    assert t.adaptive_hold_active is True
    assert t.target == 0, "a fixed target would cut the move the hold exists to keep"
    assert t.stop_provenance == "adaptive_hold"
    assert d.stop >= 100.0 + 80.0 / 65, "never below breakeven after costs"


def test_it_releases_the_trade_to_end_of_day_handling(monkeypatch):
    s = _scanner(monkeypatch)
    t = _trade(peak_pnl=65 * 8.0, adaptive_hold_active=True)
    for clock in (datetime(2026, 9, 11, 15, 1), datetime(2026, 9, 11, 15, 15)):
        d = s._adaptive_hold_step(t, latest_time=clock, premium=107.0)
        assert d.action == "NONE", f"adaptive hold still acting at {clock:%H:%M}"
    assert t.adaptive_hold_active is False


def test_futures_are_not_touched(monkeypatch):
    s = _scanner(monkeypatch)
    d = s._adaptive_hold_step(
        _trade(inst_type="FUT"), latest_time=datetime(2026, 9, 11, 13, 40), premium=97.0
    )
    assert d.action == "NONE"


def test_the_exit_loop_consults_the_hold_before_the_profit_exits():
    """Order matters: the hold must run before the runner and profit-lock blocks."""
    import inspect
    from scanner import Scanner

    src = inspect.getsource(ExitCycleService.update_active_trades)
    hold = src.index("scanner._adaptive_hold_step(")
    assert hold < src.index("# ── 4. TARGET TRAILING & RUNNER MODE")
    assert hold < src.index("scanner._profit_lock_floor(")
    for guarded in (
        "if cap_exit and not adaptive_active:",
        "if gb_exit and not adaptive_active:",
        "if should_stagnate and not adaptive_active:",
            "if not adaptive_active and scanner._smart_profit_retention_eligible(",
    ):
        assert guarded in src, guarded
