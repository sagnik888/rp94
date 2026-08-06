"""Four thresholds that stopped meaning what they say.

Req 6  — one TTL governed both the option-chain network fetch and the analysis stacked on
         top of it, so cheap candle math inherited an expensive round trip's staleness.
Req 5d — the profit ladder ran to a lock ratio of 1.00, which puts the floor ON the peak
         price and turns a trailing lock into a fixed take-profit.
Req 7  — the lock's arming cap is a flat Rs.3000 whatever the size, so a 10-lot position
         starts tightening a tenth of the way (in R) through the move a 1-lot one does.
Req 22 — a range measured on the INDEX was divided by an OPTION PREMIUM to get bps, which
         pinned the "volatility-adjusted" stagnation exit to its ceiling on every option.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import scanner as scanner_module
from scanner import IST, Scanner


@pytest.fixture
def settings():
    return scanner_module.get_settings()


# ── Req 6: the analysis TTL is not the network TTL ───────────────────────────

def test_the_analysis_ttl_is_shorter_than_the_chain_fetch_budget(settings):
    """The chain is a broker round trip; the analysis on top is candle math.

    Measured at 48ms per instrument-timeframe with a 40-strike chain, so refreshing it
    on its own budget costs well under 1% duty. It used to inherit the chain's 90s.
    """
    s = Scanner.__new__(Scanner)
    assert s._intel_analysis_ttl_seconds() < float(settings.intelligence_cache_ttl_seconds)


def test_an_unset_analysis_ttl_falls_back_to_the_old_behaviour(monkeypatch):
    """An older .env must not silently get a busier scanner."""
    s = Scanner.__new__(Scanner)
    base = scanner_module.get_settings()
    monkeypatch.setattr(
        scanner_module, "get_settings",
        lambda: SimpleNamespace(
            intelligence_analysis_ttl_seconds=0.0,
            intelligence_cache_ttl_seconds=90.0,
        ),
    )
    assert s._intel_analysis_ttl_seconds() == 90.0


def test_a_junk_analysis_ttl_does_not_crash_the_scan(monkeypatch):
    s = Scanner.__new__(Scanner)
    monkeypatch.setattr(
        scanner_module, "get_settings",
        lambda: SimpleNamespace(
            intelligence_analysis_ttl_seconds="soon",
            intelligence_cache_ttl_seconds=90.0,
        ),
    )
    assert s._intel_analysis_ttl_seconds() == 90.0


# ── Req 5d: a lock ratio of 1.00 is not a lock ───────────────────────────────

def _ratio(peak, qty=10):
    return Scanner.__new__(Scanner)._profit_lock_floor(100.0, "LONG", qty, peak)[2]


def test_the_ladder_never_locks_the_entire_peak():
    """At 1.00 the floor sits on the peak, which the next tick breaches by definition."""
    assert _ratio(5000.0) < 1.0
    assert _ratio(50_000.0) < 1.0
    assert _ratio(1_000_000.0) < 1.0


def test_the_ceiling_still_protects_a_major_win():
    assert _ratio(5000.0) >= 0.90


def test_the_lower_rungs_are_untouched():
    assert _ratio(500.0) == 0.50
    assert _ratio(1000.0) == 0.70
    assert _ratio(2000.0) == 0.80
    assert _ratio(3000.0) == 0.90


def test_the_floor_price_leaves_room_to_breathe():
    """The consequence in price terms, not just the ratio."""
    s = Scanner.__new__(Scanner)
    entry, qty = 100.0, 10
    stop, _reason, ratio = s._profit_lock_floor(entry, "LONG", qty, 5000.0)
    peak_price = entry + 5000.0 / qty
    assert stop < peak_price, "the protective floor landed on the peak price itself"
    assert ratio < 1.0


def test_a_short_gets_the_same_room():
    s = Scanner.__new__(Scanner)
    entry, qty = 100.0, 10
    stop, _r, _ratio = s._profit_lock_floor(entry, "SHORT", qty, 5000.0)
    assert stop > entry - 5000.0 / qty


def test_the_ceiling_is_bounded_and_cannot_be_set_to_one(monkeypatch):
    s = Scanner.__new__(Scanner)
    for value, expected in ((1.0, 0.99), (5.0, 0.99), (0.1, 0.50), ("x", 0.95), (0.80, 0.80)):
        monkeypatch.setattr(
            scanner_module, "get_settings",
            lambda v=value: SimpleNamespace(profit_lock_max_ratio=v),
        )
        assert s._profit_lock_ratio_ceiling() == expected


# ── Req 7: the arming cap did not scale with size ────────────────────────────

def _trade(lots=1, lot_size=75):
    return SimpleNamespace(lots=lots, lot_size=lot_size, quantity=lots * lot_size)


def test_the_arming_cap_is_flat_by_default():
    """Default off, like the ladder switch it rides on."""
    s = Scanner.__new__(Scanner)
    assert s._profit_lock_arm_cap_rs(_trade(1)) == 3000.0
    assert s._profit_lock_arm_cap_rs(_trade(10)) == 3000.0


def test_the_arming_cap_scales_under_the_per_lot_switch(monkeypatch):
    """One flag, one basis: the tier and the threshold that feeds it must agree."""
    s = Scanner.__new__(Scanner)
    base = scanner_module.get_settings()
    monkeypatch.setattr(
        scanner_module, "get_settings",
        lambda: SimpleNamespace(**{**vars(base), "profit_lock_per_lot_basis": True}),
    )
    assert s._profit_lock_arm_cap_rs(_trade(1)) == 3000.0
    assert s._profit_lock_arm_cap_rs(_trade(10)) == 30_000.0


def test_a_missing_trade_never_scales_below_the_base(monkeypatch):
    s = Scanner.__new__(Scanner)
    base = scanner_module.get_settings()
    monkeypatch.setattr(
        scanner_module, "get_settings",
        lambda: SimpleNamespace(**{**vars(base), "profit_lock_per_lot_basis": True}),
    )
    assert s._profit_lock_arm_cap_rs(None) == 3000.0


def test_a_partial_scanner_can_still_read_the_cap():
    """Partial scanners are a real pattern here; four regressions have come from
    assuming otherwise."""
    stub = SimpleNamespace()
    assert Scanner._profit_lock_arm_cap_rs(stub, _trade(1)) == 3000.0


# ── Req 22: index points divided by an option premium ────────────────────────

def _index_frame(n=20, base=24500.0, rng_points=20.0):
    idx = pd.date_range("2026-08-05 11:00", periods=n, freq="1min")
    close = np.full(n, base)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + rng_points / 2,
            "low": close - rng_points / 2,
            "close": close,
            "volume": np.full(n, 700000.0),
        },
        index=idx,
    )


def _stagnation_scanner(frame):
    s = Scanner.__new__(Scanner)
    s.candles = SimpleNamespace(get_candles=lambda i, tf: frame.copy())
    s._trade_like_initial_risk_rs = lambda t, qty=0, inst_mult=1.0: 2000.0
    return s


def _option_trade(premium=217.50, minutes_held=45):
    entry = datetime(2026, 8, 5, 11, 20) - timedelta(minutes=minutes_held)
    return SimpleNamespace(
        instrument="NIFTY",
        timeframe="1min",
        inst_type="OPT",
        price=premium,
        entry_price=premium,
        current_price=premium * 0.90,
        entry_time=entry,
        lots=1,
        lot_size=75,
        entry_spot=24500.0,
    )


def test_the_reversal_context_states_what_its_range_was_measured_against():
    s = _stagnation_scanner(_index_frame())
    ctx = s._profit_guard_reversal_context(
        _option_trade(), 217.50, datetime(2026, 8, 5, 11, 20), timeframe="1min"
    )
    assert ctx.get("reference_price") == pytest.approx(24500.0), (
        "median_range is measured on the index; without its reference price a caller "
        "has no way to turn it into a fraction correctly"
    )


def _required_minutes(range_points, trade=None, held=18):
    """Read the volatility adjustment off the age-gate message.

    base_minutes 20 + extra_minutes, where extra scales with volatility_ratio. A young
    trade stops at the age gate, which prints the adjusted requirement; an older one
    runs on into the trend branch and never reports it.
    """
    s = _stagnation_scanner(_index_frame(rng_points=range_points))
    reason = s._stagnation_exit_decision(
        trade or _option_trade(minutes_held=held),
        current_pnl=1000.0, peak_pnl=2000.0,
        latest_time=datetime(2026, 8, 5, 11, 20),
    )[1]
    assert "volatility-adjusted" in reason, reason
    return float(reason.split("volatility-adjusted ")[1].rstrip("m"))


def test_an_option_no_longer_pins_the_volatility_ratio_to_its_ceiling():
    """20 index points over a Rs.217 premium read as 919 bps against a 4 bps baseline.

    volatility_ratio clamps at 3.0, so EVERY option trade got the full 10 extra minutes
    (20 + 10 = 30) and the loosest 68% retention floor — the adjustment adjusted nothing.
    """
    assert _required_minutes(20.0) < 30.0, (
        "still pinned to the ceiling: the full extra allowance on an ordinary tape"
    )


def test_a_quiet_index_and_a_busy_index_now_read_differently():
    """The whole point of the adjustment: two tapes must not score identically."""
    assert _required_minutes(6.0) < _required_minutes(40.0)


def test_a_genuinely_wild_tape_still_reaches_the_ceiling():
    """The clamp is right when the market earns it."""
    assert _required_minutes(120.0) == 30.0


def test_a_futures_trade_is_unaffected():
    """Its current_price WAS the index, so this path was already correct for FUT."""
    fut = _option_trade(minutes_held=18)
    fut.inst_type = "FUT"
    fut.price = fut.entry_price = 24500.0
    fut.current_price = 24480.0
    assert _required_minutes(20.0, trade=fut) == pytest.approx(
        _required_minutes(20.0), abs=0.2
    )


def test_no_reference_price_falls_back_rather_than_crashing():
    s = Scanner.__new__(Scanner)
    s.candles = SimpleNamespace(get_candles=lambda i, tf: None)
    s._trade_like_initial_risk_rs = lambda t, qty=0, inst_mult=1.0: 2000.0
    exit_now, reason = s._stagnation_exit_decision(
        _option_trade(), current_pnl=1000.0, peak_pnl=2000.0,
        latest_time=datetime(2026, 8, 5, 11, 20),
    )
    assert isinstance(exit_now, bool) and isinstance(reason, str)


def test_the_intel_cache_actually_expires_on_the_analysis_ttl(monkeypatch):
    """Drive _get_timeframe_intel, not just the helper.

    Testing the TTL function in isolation proves nothing about whether the cache reads
    it — the same trap that made two earlier fixes here non-load-bearing. This asserts
    the observable: a bundle 40s old is REUSED under the old 90s budget and RECOMPUTED
    under the 30s one.
    """
    base = scanner_module.get_settings()
    monkeypatch.setattr(
        scanner_module, "get_settings",
        lambda: SimpleNamespace(
            **{**vars(base), "intelligence_analysis_ttl_seconds": 30.0,
               "intelligence_cache_ttl_seconds": 90.0}
        ),
    )
    s = Scanner.__new__(Scanner)
    cached = {"score": 61.0, "regime": "TRENDING_UP"}
    s._cached_timeframe_intel = lambda n, tf, allow_fallback=True: cached
    s._intel_result_cacheable = lambda r: True
    s._timeframe_intel_key = lambda n, tf: f"{n}_{tf}"
    s._intel_cache_time = {"NIFTY_5min": 1000.0}
    s.config = {"indices": {"NIFTY": {}}}
    s.candles = SimpleNamespace(get_candles=lambda i, tf: None, get_latest_price=lambda i: 24500.0)
    s._live_analysis_window = lambda df, tf: None
    s._regime_history_ready = lambda df: False

    # 20s old: inside 30s, served from cache.
    assert s._get_timeframe_intel("NIFTY", "5min", now=1020.0) is cached
    # 40s old: past 30s but inside the OLD 90s budget. It must no longer be reused —
    # with no candles the recompute bails to the fallback, which is the observable.
    s._cached_timeframe_intel = lambda n, tf, allow_fallback=True: (
        {"score": 99.0} if allow_fallback else cached
    )
    assert s._get_timeframe_intel("NIFTY", "5min", now=1040.0) == {"score": 99.0}, (
        "a 40-second-old bundle was still served: the cache is reading the chain's "
        "network budget, not the analysis budget"
    )


# ── the noise FLOORS were on the wrong basis too ─────────────────────────────
#
# _profit_guard_reversal_context computes
#     fast_body_threshold  = max(median_range * 1.20, price * 0.00025)
#     fast_close_threshold = max(median_range * 1.50, price * 0.00035)
# and compares them against INDEX candle bodies. The second term is an index-noise
# floor: "an adverse candle under ~2.5-3.5 bps of the index is not a reversal."
#
# Fed an option premium it collapses to nothing. I looked at this when fixing the trend
# votes and called it harmless, reasoning that max() picks the median_range term anyway.
# That holds only when the tape is BUSY. In quiet tape the floor is meant to WIN, and a
# premium-derived floor never can:
#   NIFTY 24500, PE at 217.50, 3.0-point median range
#     intended  max(3.60, 24500*0.00025 = 6.125) = 6.125
#     premium   max(3.60,   217.50*0.00025 = 0.054) = 3.60
# so the guard fires on exactly the wiggles the floor exists to ignore.

def _quiet_tape(range_points=3.0, base=24500.0, n=20):
    idx = pd.date_range("2026-08-07 12:30", periods=n, freq="1min")
    close = np.full(n, base)
    return pd.DataFrame(
        {"open": close, "high": close + range_points / 2, "low": close - range_points / 2,
         "close": close, "volume": np.full(n, 700000.0)},
        index=idx,
    )


def _thresholds(price, frame=None):
    s = Scanner.__new__(Scanner)
    s.candles = SimpleNamespace(get_candles=lambda i, tf: (frame if frame is not None else _quiet_tape()).copy())
    trade = SimpleNamespace(
        instrument="NIFTY", timeframe="1min", inst_type="OPT", direction="SHORT",
        entry_price=217.50, current_price=price, entry_spot=24500.0,
    )
    ctx = s._profit_guard_reversal_context(trade, price, datetime(2026, 8, 7, 12, 50), timeframe="1min")
    return ctx.get("fast_body_threshold", 0.0), ctx.get("fast_close_threshold", 0.0)


def test_the_noise_floor_survives_being_handed_a_premium():
    """The live shape: an OPT trade whose caller passes the premium."""
    body, close = _thresholds(217.50)
    assert body == pytest.approx(6.125), (
        f"body floor collapsed to {body:.3f}; the index floor is 6.125 and a 3.60 "
        "threshold fires on ordinary lunchtime noise"
    )
    assert close == pytest.approx(8.575)


def test_premium_and_spot_give_the_same_floor():
    """Whichever basis the caller happens to pass, the answer is about the index."""
    assert _thresholds(217.50) == _thresholds(24500.0)


def test_a_busy_tape_is_still_driven_by_the_measured_range():
    """The floor must not become a ceiling — a real range still wins."""
    busy = _quiet_tape(range_points=40.0)
    body, close = _thresholds(217.50, frame=busy)
    assert body == pytest.approx(40.0 * 1.20)
    assert close == pytest.approx(40.0 * 1.50)


def test_the_median_range_fallback_is_on_the_index_basis_too():
    """A degenerate frame falls back to price*0.0002; that price must be the index."""
    flat = _quiet_tape(range_points=0.0)
    body, _close = _thresholds(217.50, frame=flat)
    # index fallback 24500*0.0002 = 4.90 -> body = max(4.90*1.20, 6.125) = 6.125
    # premium fallback 217.50*0.0002 = 0.0435 -> body would be max(0.052, 0.054) = 0.054
    assert body > 1.0, f"fallback range came from the premium: body floor {body:.4f}"
