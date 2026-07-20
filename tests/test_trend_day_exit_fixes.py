"""
Trend-day exit-logic fixes (2026-06-24).

The system scalped +Rs.174-694 winners on a +1-1.6% trending day while an older build
rode the same names for +Rs.2,400-3,240. Five premature-exit causes were fixed:

  [1] let_winners_run re-wired into _profit_lock_floor (was declared-but-unused): the
      profit-lock no longer arms until peak >= profit_lock_min_r_multiple x initial risk.
  [2] a 15m-aligned entry in a trending regime is NOT forced into scalp_lock_mode.
  [3] a repainted signal is NOT force-exited while the position is in profit.
  [4] a contrarian OI/PCR reversal does NOT exit a position aligned with a trending regime.
  [5] B+/A/A+ 5m trend-aligned option trades defer SMART_PROFIT_LOCK on normal
      premium pullbacks unless giveback is severe or the index trend flips.
"""

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from config.settings import get_settings
from scanner import Scanner


@pytest.fixture
def settings():
    s = get_settings()
    s.let_winners_run = True
    s.profit_lock_min_r_multiple = 1.0
    s.scalp_profit_lock_min_r_multiple = 0.50
    s.profit_lock_abs_floor_rs = 450.0
    s.trend_entry_skip_scalp_lock = True
    s.repaint_abort_skip_when_profitable = True
    s.oi_pcr_reversal_skip_in_trend = True
    return s


def _bind(method_name, **attrs):
    st = SimpleNamespace(_setting_enabled=Scanner._setting_enabled, **attrs)
    setattr(st, method_name, getattr(Scanner, method_name).__get__(st, Scanner))
    return st, getattr(st, method_name)


# ── [1] let_winners_run gates the profit-lock arming ─────────────────
def test_let_winners_run_delays_lock_until_r_multiple(settings):
    settings.let_winners_run = True
    settings.profit_lock_min_r_multiple = 2.0
    _, floor = _bind("_profit_lock_floor")
    # risk Rs.1000, r_mult 2.0 -> lock must not arm until peak >= Rs.2000.
    assert floor(100.0, "LONG", 10, 1500.0, False, inst_mult=1.0, initial_risk_rs=1000.0) == (0.0, "", 0.0)
    stop, reason, _ = floor(100.0, "LONG", 10, 2500.0, False, inst_mult=1.0, initial_risk_rs=1000.0)
    assert reason == "SMART_PROFIT_LOCK" and stop > 100.0


def test_let_winners_run_off_arms_at_flat_500(settings):
    settings.let_winners_run = False
    _, floor = _bind("_profit_lock_floor")
    # Legacy behaviour: arms at peak >= Rs.500 regardless of risk size.
    _, reason, _ = floor(100.0, "LONG", 10, 600.0, False, inst_mult=1.0, initial_risk_rs=5000.0)
    assert reason == "SMART_PROFIT_LOCK"


def test_scalp_mode_uses_r_aware_arming(settings):
    settings.let_winners_run = True
    settings.profit_lock_min_r_multiple = 5.0
    settings.scalp_profit_lock_min_r_multiple = 0.50
    _, floor = _bind("_profit_lock_floor")
    # Genuine scalps still arm earlier than runners, but not at a flat Rs.500
    # when the configured initial risk is much larger.
    assert floor(100.0, "LONG", 10, 600.0, True, inst_mult=1.0, initial_risk_rs=5000.0) == (0.0, "", 0.0)
    _, reason, _ = floor(100.0, "LONG", 10, 2600.0, True, inst_mult=1.0, initial_risk_rs=5000.0)
    assert reason == "SMART_PROFIT_LOCK"


# ── [2] trend-aligned entry is not forced into scalp_lock_mode ───────
def _declining_1min_df():
    closes = [20100 - i * 3 for i in range(30)]  # steady decline -> price below EMAs
    rows = [{"open": c + 1, "high": c + 2, "low": c - 2, "close": c, "volume": 1000} for c in closes]
    return pd.DataFrame(rows, index=pd.date_range("2026-06-24 09:15", periods=30, freq="1min"))


def _location_scanner(regime, trend_agrees):
    df = _declining_1min_df()
    st = SimpleNamespace(
        mode="REAL",
        latest_regimes={"NIFTY": regime},
        candles=SimpleNamespace(get_candles=lambda inst, tf: df),
        _setting_enabled=Scanner._setting_enabled,
    )
    st._candidate_15m_trend_agrees = lambda c: trend_agrees
    st._market_location_entry_gate = Scanner._market_location_entry_gate.__get__(st, Scanner)
    return st


def _long_candidate():
    return SimpleNamespace(
        instrument="NIFTY", direction="LONG", timeframe="5min", inst_type="FUT",
        reasons=[], intelligence_score=0.0, adx_value=20.0,
        impulse_confirmed=False, is_explosive_bypass=False, signal_timestamp=None,
    )


def test_trend_aligned_pullback_skips_scalp_lock(settings):
    st = _location_scanner("TRENDING_UP", trend_agrees=True)
    cand = _long_candidate()
    _, risk_reason = st._market_location_entry_gate(cand)
    assert risk_reason  # poor location flagged
    assert getattr(cand, "scalp_lock_mode") is False  # but NOT scalped: it's a trend pullback


def test_volatile_location_risk_still_scalps(settings):
    st = _location_scanner("VOLATILE", trend_agrees=True)
    cand = _long_candidate()
    st._market_location_entry_gate(cand)
    assert getattr(cand, "scalp_lock_mode") is True  # non-trending -> cautious scalp lock


def test_trend_disagreement_still_scalps(settings):
    st = _location_scanner("TRENDING_UP", trend_agrees=False)
    cand = _long_candidate()
    st._market_location_entry_gate(cand)
    assert getattr(cand, "scalp_lock_mode") is True


def test_skip_scalp_toggle_off_restores_scalp(settings):
    settings.trend_entry_skip_scalp_lock = False
    st = _location_scanner("TRENDING_UP", trend_agrees=True)
    cand = _long_candidate()
    st._market_location_entry_gate(cand)
    assert getattr(cand, "scalp_lock_mode") is True


# ── [3] repaint-abort is skipped while in profit ─────────────────────
def test_repaint_held_when_in_profit(settings):
    _, held = _bind("_repaint_abort_held_for_profit", _candidate_gross_pnl=lambda c, p: 250.0)
    assert held(SimpleNamespace(), 100.0) is True


def test_repaint_aborts_when_losing(settings):
    _, held = _bind("_repaint_abort_held_for_profit", _candidate_gross_pnl=lambda c, p: -50.0)
    assert held(SimpleNamespace(), 100.0) is False


def test_repaint_hold_toggle_off(settings):
    settings.repaint_abort_skip_when_profitable = False
    _, held = _bind("_repaint_abort_held_for_profit", _candidate_gross_pnl=lambda c, p: 250.0)
    assert held(SimpleNamespace(), 100.0) is False


# ── [4] OI/PCR reversal is ignored in an aligned trend ───────────────
def test_oi_pcr_skip_in_aligned_trend(settings):
    _, blocked = _bind("_intel_exit_blocked_by_trend")
    assert blocked(SimpleNamespace(direction="LONG"), "TRENDING_UP") is True
    assert blocked(SimpleNamespace(direction="SHORT"), "TRENDING_DOWN") is True
    assert blocked(SimpleNamespace(direction="LONG"), "BREAKOUT_UP") is True


def test_oi_pcr_fires_when_not_aligned_or_not_trending(settings):
    _, blocked = _bind("_intel_exit_blocked_by_trend")
    assert blocked(SimpleNamespace(direction="LONG"), "TRENDING_DOWN") is False  # counter-trend
    assert blocked(SimpleNamespace(direction="LONG"), "VOLATILE") is False        # not trending
    assert blocked(SimpleNamespace(direction="LONG"), "RANGING") is False


def test_oi_pcr_skip_toggle_off(settings):
    settings.oi_pcr_reversal_skip_in_trend = False
    _, blocked = _bind("_intel_exit_blocked_by_trend")
    assert blocked(SimpleNamespace(direction="LONG"), "TRENDING_UP") is False


# ── [5] 5m trend option profit-lock breathes before exiting ──────────
def _trend_profit_lock_scanner(context=None):
    st = SimpleNamespace(
        _setting_enabled=Scanner._setting_enabled,
        latest_regimes={"NIFTY": "BREAKOUT_DOWN"},
        candles=SimpleNamespace(get_latest_price=lambda _instrument: 23891.0),
        _option_breathing_distance=lambda *_args, **_kwargs: 20.0,
    )
    st._profit_guard_reversal_context = lambda *_args, **_kwargs: context or {
        "has_context": True,
        "fast_reversal": False,
        "trend_supportive": True,
        "trend_votes": 3,
        "reason": "same-trend pullback",
    }
    st._should_defer_trend_option_profit_lock = Scanner._should_defer_trend_option_profit_lock.__get__(st, Scanner)
    return st


def _trend_option_candidate(grade="B+", timeframe="5min"):
    return SimpleNamespace(
        instrument="NIFTY",
        direction="SHORT",
        timeframe=timeframe,
        inst_type="OPT",
        grade=grade,
        price=190.55,
        entry_spot=23897.7,
        market_regime="BREAKOUT_DOWN",
    )


def test_trend_option_profit_lock_defers_bplus_a_aplus_winner(settings):
    settings.trend_option_profit_lock_defer = True
    settings.trend_option_profit_lock_severe_retention = 0.50
    scanner = _trend_profit_lock_scanner()

    for grade in ("B+", "A", "A+"):
        defer, reason = scanner._should_defer_trend_option_profit_lock(
            _trend_option_candidate(grade),
            current_price=199.18,
            lock_stop=199.18,
            current_pnl=561.0,
            peak_pnl=903.0,
            latest_time=datetime(2026, 6, 30, 9, 31, 24),
            regime="BREAKOUT_DOWN",
        )
        assert defer is True
        assert "trend option profit-lock deferred" in reason


def test_15min_trend_option_profit_lock_uses_15min_context(settings):
    settings.trend_option_profit_lock_defer = True
    scanner = _trend_profit_lock_scanner(
        {
            "has_context": True,
            "fast_reversal": False,
            "trend_supportive": True,
            "trend_votes": 3,
            "reason": "15min same-trend pullback",
        }
    )

    defer, reason = scanner._should_defer_trend_option_profit_lock(
        _trend_option_candidate("A", timeframe="15min"),
        current_price=199.18,
        lock_stop=199.18,
        current_pnl=561.0,
        peak_pnl=903.0,
        latest_time=datetime(2026, 6, 30, 9, 31, 24),
        regime="BREAKOUT_DOWN",
    )

    assert defer is True
    assert "15min" in reason


def test_trend_option_profit_lock_allows_severe_giveback(settings):
    settings.trend_option_profit_lock_defer = True
    settings.trend_option_profit_lock_severe_retention = 0.50
    scanner = _trend_profit_lock_scanner()

    defer, reason = scanner._should_defer_trend_option_profit_lock(
        _trend_option_candidate("A+"),
        current_price=196.00,
        lock_stop=199.18,
        current_pnl=420.0,
        peak_pnl=903.0,
        latest_time=datetime(2026, 6, 30, 9, 31, 24),
        regime="BREAKOUT_DOWN",
    )

    assert defer is False
    assert "severe giveback" in reason


def test_trend_option_profit_lock_allows_fast_reversal(settings):
    settings.trend_option_profit_lock_defer = True
    scanner = _trend_profit_lock_scanner(
        {
            "has_context": True,
            "fast_reversal": True,
            "trend_supportive": False,
            "trend_votes": 0,
            "reason": "fast adverse reversal",
        }
    )

    defer, reason = scanner._should_defer_trend_option_profit_lock(
        _trend_option_candidate("A+"),
        current_price=199.18,
        lock_stop=199.18,
        current_pnl=561.0,
        peak_pnl=903.0,
        latest_time=datetime(2026, 6, 30, 9, 31, 24),
        regime="BREAKOUT_DOWN",
    )

    assert defer is False
    assert "fast adverse reversal" in reason


def test_15min_runner_major_win_uses_15min_structure_not_1min_noise(settings):
    scanner = Scanner.__new__(Scanner)

    def frame(closes, freq):
        return pd.DataFrame(
            [
                {
                    "open": close - 2.0,
                    "high": close + 4.0,
                    "low": close - 4.0,
                    "close": close,
                    "volume": 1000,
                }
                for close in closes
            ],
            index=pd.date_range("2026-06-30 09:15", periods=len(closes), freq=freq),
        )

    one_min_noise = frame([120.0 - i * 1.5 for i in range(30)], "1min")
    fifteen_min_trend = frame([100.0 + i * 3.0 for i in range(35)], "15min")
    scanner.candles = SimpleNamespace(
        get_candles=lambda _instrument, timeframe: (
            fifteen_min_trend if timeframe == "15min" else one_min_noise
        )
    )
    trade = SimpleNamespace(
        instrument="NIFTY",
        timeframe="15min",
        direction="LONG",
    )

    should_exit, reason = scanner._should_exit_major_win_guard(
        trade,
        current_price=float(fifteen_min_trend["close"].iloc[-1]),
        peak_pnl=5000.0,
        current_pnl=3500.0,
        latest_time=fifteen_min_trend.index[-1].to_pydatetime(),
    )

    assert should_exit is False
    assert "15min" in reason


def test_15min_runner_still_exits_on_1min_emergency_reversal(settings):
    scanner = Scanner.__new__(Scanner)

    def frame(closes, freq):
        return pd.DataFrame(
            [
                {
                    "open": open_,
                    "high": max(open_, close) + 3.0,
                    "low": min(open_, close) - 3.0,
                    "close": close,
                    "volume": 1000,
                }
                for open_, close in closes
            ],
            index=pd.date_range("2026-06-30 09:15", periods=len(closes), freq=freq),
        )

    fifteen_min_trend = frame([(100.0 + i * 3.0, 101.0 + i * 3.0) for i in range(35)], "15min")
    one_min_rows = [(180.0 + i * 0.2, 180.1 + i * 0.2) for i in range(29)]
    one_min_rows.append((190.0, 160.0))
    one_min_break = frame(one_min_rows, "1min")
    scanner.candles = SimpleNamespace(
        get_candles=lambda _instrument, timeframe: (
            fifteen_min_trend if timeframe == "15min" else one_min_break
        )
    )
    trade = SimpleNamespace(
        instrument="NIFTY",
        timeframe="15min",
        direction="LONG",
    )

    should_exit, reason = scanner._should_exit_major_win_guard(
        trade,
        current_price=160.0,
        peak_pnl=5000.0,
        current_pnl=3500.0,
        latest_time=fifteen_min_trend.index[-1].to_pydatetime(),
    )

    assert should_exit is True
    assert "1min emergency adverse reversal" in reason
