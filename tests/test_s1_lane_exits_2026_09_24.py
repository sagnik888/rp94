"""S1 lane, 2026-09-24 user spec.

Live finding that drove it: the NIFTY 5m S1 SELL on the 13:40 bar (the same bar the user's
TradingView chart signalled) was produced at 13:45:06, but Auto was OFF after a restart, so
it became a manual/session row. That row was closed after 94s by SMART_PROFIT_LOCK
(+Rs.1,262 of a Rs.1,976 peak); later S1 rows were cut by EARLY_LOSS_CUT - other engines'
guards run by the session-row loop. S1 rows now get the S1 exits only.

Spec: guard 5/10/15% by timeframe, one step tighter after 14:30, one step wider at score
>= 80, capped at 10% in a clear trend regime and 15% otherwise; index SL1 locked to entry
+ 0.7R at T1; profit lock arms at 1R with a 25/10/0% giveback (trend with/neutral/against);
cut early when the trend turns against; runner while the trend is with the trade; no 15m
entries after 14:50 and no 5m/1m entries after 15:10.
"""
from datetime import datetime, time
from types import SimpleNamespace

import pytest

from config.settings import Settings
from engine import s1_lane


def _settings(**overrides):
    s = Settings()
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return s


@pytest.mark.parametrize("tf,score,regime,expected", [
    ("15min", 70, "RANGING", 15.0), ("15min", 70, "VOLATILE", 15.0), ("15min", 70, "UNKNOWN", 15.0),
    ("15min", 70, "TRENDING_UP", 10.0), ("15min", 70, "TRENDING_DOWN", 10.0),
    ("5min", 80, "RANGING", 15.0), ("5min", 79, "RANGING", 10.0), ("5min", 80, "TRENDING_UP", 10.0),
    ("1min", 70, "RANGING", 5.0),
])
def test_guard_is_5_10_15_with_strength_80_and_a_regime_cap(tf, score, regime, expected):
    at = datetime(2026, 9, 24, 10, 0)
    assert s1_lane.premium_guard_pct(tf, at, score, _settings(), regime=regime) == expected


def test_s1_entry_cutoffs_per_timeframe():
    s = _settings()
    # 2026-09-26 user: the system-wide last entries (1m moved 15:10 -> 15:14)
    assert [s1_lane.entry_cutoff(tf, s) for tf in ("15min", "5min", "1min")] == ["14:50", "15:10", "15:14"]


def test_signal_processor_uses_the_s1_cutoff_without_the_eod_clamp():
    from engine.signal_processor import _entry_cutoff

    s = _settings(ut_5min_no_entry_after="15:10", eod_protect_window_minutes=14.0, ut_force_exit_time="15:15")
    assert _entry_cutoff(s, "5min", s1=True) == ("15:10", time(15, 10))
    assert _entry_cutoff(s, "15min", s1=True) == ("14:50", time(14, 50))
    assert _entry_cutoff(s, "5min")[1] <= time(15, 1)      # other engines keep theirs


def _candidate(tf, signal_ts, s1=True):
    return SimpleNamespace(instrument="NIFTY", timeframe=tf, direction="SHORT", signal_timestamp=signal_ts,
                           source_engine="s1" if s1 else "utbot", source_lane="S1" if s1 else "")


@pytest.mark.parametrize("tf,bar_open,now,allowed", [
    ("5min", datetime(2026, 9, 24, 15, 5), datetime(2026, 9, 24, 15, 10, 5), True),    # closes 15:10
    ("5min", datetime(2026, 9, 24, 15, 10), datetime(2026, 9, 24, 15, 15, 5), False),  # closes 15:15
    ("1min", datetime(2026, 9, 24, 15, 9), datetime(2026, 9, 24, 15, 10, 2), True),
    ("1min", datetime(2026, 9, 24, 15, 10), datetime(2026, 9, 24, 15, 11, 2), True),   # 1m last entry 15:14
    ("1min", datetime(2026, 9, 24, 15, 13), datetime(2026, 9, 24, 15, 14, 2), True),
    ("1min", datetime(2026, 9, 24, 15, 14), datetime(2026, 9, 24, 15, 15, 2), False),
    ("15min", datetime(2026, 9, 24, 14, 30), datetime(2026, 9, 24, 14, 45, 5), True),  # closes 14:45
    ("15min", datetime(2026, 9, 24, 14, 45), datetime(2026, 9, 24, 15, 0, 5), False),  # closes 15:00
])
def test_scanner_cutoff_for_s1_candidates(tf, bar_open, now, allowed):
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    sc._get_trade_regime = lambda c, default="UNKNOWN": "RANGING"
    assert sc._entry_cutoff_allows_candidate(_candidate(tf, bar_open), now) is allowed


# ---------------------------------------------------------------- session rows (runtime)


def _row(**kw):
    base = dict(instrument="NIFTY", direction="SHORT", price=150.0, stop=135.0, target=210.0, lots=1, lot_size=65,
                grade="C", confidence=0.5, timeframe="5min", inst_type="OPT", option_type="PE", atm_strike=23100,
                multiplier=-0.5, trading_symbol="NIFTY26SEP23100PE", symbol_token="1", rr=4.0, score=0.0,
                reasons=[], spot_stop=23123.1, spot_target=23007.6, signal_timestamp=datetime(2026, 9, 24, 13, 40),
                current_price=150.0, pnl=0.0, status="OPEN", action="ENTRY", exit_reason="",
                entry_spot=23100.0, underlying_entry=23100.0, underlying_stop=23123.1, quote_verified=True,
                source_engine="s1", source_lane="S1", peak_pnl=0.0, max_drawdown=0.0, s1_score=98.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _run_rows(monkeypatch, row, spot, premium, engine_state=None):
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    exits, other = [], []
    row.current_price = premium
    sc._loaded_session_is_current_day = lambda: True
    sc._active_session_entry_candidates = lambda inst: [row]
    sc.candles = SimpleNamespace(get_latest_price=lambda inst: spot)
    sc._ensure_trade_like_initial_stop = lambda c: None
    sc._get_trade_regime = lambda c, default="UNKNOWN": "RANGING"
    sc._candidate_net_pnl = lambda c, px: (float(c.price) - float(px)) * -65.0 * -1 if False else (float(px) - float(c.price)) * 65.0
    sc._trade_like_initial_risk_rs = lambda c: 15.0 * 65
    sc.mtf = SimpleNamespace(_result_cache={"NIFTY_5min": {"signals": [], "state": engine_state or {}}})
    sc._diag_inc = lambda *a: None
    sc._inherit_exit_entry_metadata = lambda e, p: None
    sc.log_event = lambda *a, **k: None
    sc._notify = lambda *a, **k: None
    sc._maybe_arm_reversion_from_exit = lambda *a, **k: None
    sc._remember_trade_candidates = lambda items: exits.extend(items)
    sc._latest_exit_candidates = {}
    sc._persist_session_trade_candidates = lambda: None

    def other_guard(*a, **k):
        other.append("tf15")
        return False, ""

    sc._fifteen_min_one_min_support_exit_decision = other_guard
    try:
        sc._check_session_candidates_breaches_locked("NIFTY")
    except Exception:
        if row.source_engine == "s1":
            raise
    return [e.exit_reason for e in exits], other


def test_an_s1_session_row_exits_on_its_index_stop_and_skips_the_other_guards(monkeypatch):
    reasons, other = _run_rows(monkeypatch, _row(), spot=23124.0, premium=138.0)
    assert reasons == ["S1_SL1"] and other == []


def test_an_s1_session_row_holds_and_trails_its_premium_stop(monkeypatch):
    row = _row(s1_peak_premium=180.0)
    reasons, other = _run_rows(monkeypatch, row, spot=23060.0, premium=178.0)
    assert reasons == [] and other == []
    assert row.stop == pytest.approx(180.0 * 0.85)   # 5m guard, +1 step for score 98, RANGING cap 15%


def test_an_s1_session_row_is_cut_when_the_trend_turns_against_it(monkeypatch):
    state = {"s1_bull_score": 80.0, "s1_bear_score": 20.0, "s1_state_bar_time": datetime(2026, 9, 24, 13, 50)}
    reasons, _ = _run_rows(monkeypatch, _row(), spot=23105.0, premium=147.0, engine_state=state)
    assert reasons == ["S1_TREND_AGAINST"]


def test_other_engines_rows_keep_their_guard_stack(monkeypatch):
    _, other = _run_rows(monkeypatch, _row(source_engine="utbot", source_lane=""), spot=23124.0, premium=138.0)
    assert other == ["tf15"]


def test_trend_on_the_signal_bar_itself_is_ignored():
    state = {"s1_bull_score": 10.0, "s1_bear_score": 90.0, "s1_state_bar_time": datetime(2026, 9, 24, 13, 40)}
    assert s1_lane.closed_bar_trend(_row(), {"state": state}) == 0
    later = dict(state, s1_state_bar_time=datetime(2026, 9, 24, 13, 45))
    assert s1_lane.closed_bar_trend(_row(), {"state": later}) == 1      # bearish scores, SHORT trade


# Re-entry and lot sizing for S1 changed on 2026-09-25: tests/test_s1_lane_2026_09_25.py.
