"""Expiry-day gamma mode: trade the CURRENT expiring ATM on expiry day instead of
rolling to next week, capturing the gamma/premium multi-fold the T-2 rollover skips.

The decision is a pure function of settings + the bar's date/time, so it is identical
in live and backtest. Everything is gated behind `expiry_day_trade_current_week`
(default OFF) — with the toggle off the override is always None and the unchanged
T-2 rollover applies.
"""

import threading
from datetime import datetime, date

import pandas as pd
import pytest

from config.settings import get_settings
from engine.expiry_manager import ExpiryManager
from trading.trade_manager import IST, Trade, TradeManager


def _opt_master():
    rows = []
    for exp in ("25JUN2026", "02JUL2026"):  # this week (Thu) + next
        rows.append({"name": "NIFTY", "instrumenttype": "OPTIDX",
                     "symbol": f"NIFTY{exp}24000CE", "strike": 2400000, "expiry": exp, "token": exp})
    df = pd.DataFrame(rows)
    return df


CUR = pd.Timestamp("2026-06-25")   # this week's expiry (Thu)
NXT = pd.Timestamp("2026-07-02")   # next week's expiry


@pytest.fixture
def em(monkeypatch):
    mgr = ExpiryManager()
    s = get_settings()
    fields = (
        "expiry_day_trade_current_week", "expiry_day_gamma_cutoff", "expiry_day_include_t1",
        "expiry_day_after_cutoff_mode", "expiry_day_late_window", "expiry_day_late_window_start",
        "expiry_day_late_window_end", "option_rollover_working_days",
    )
    saved = {f: getattr(s, f) for f in fields}
    # start from a known baseline for every test
    s.expiry_day_trade_current_week = False
    s.expiry_day_gamma_cutoff = "13:00"
    s.expiry_day_include_t1 = False
    s.expiry_day_after_cutoff_mode = "rollover"
    s.expiry_day_late_window = True
    s.expiry_day_late_window_start = "14:00"
    s.expiry_day_late_window_end = "15:15"
    s.option_rollover_working_days = 1
    yield mgr, s
    for f, v in saved.items():
        setattr(s, f, v)


def _ov(em, td, now):
    return em.expiry_day_override(CUR, NXT, td, now)


def test_trading_days_until_is_holiday_aware(em):
    # [1] Rollover now counts TRADING sessions (skips weekends + NSE holidays), not
    # calendar days. 2026-06-26 (Fri) is an NSE holiday in the bundled calendar.
    mgr, s = em
    # Thu 2026-06-25 expiry, counted from earlier sessions:
    assert mgr._trading_days_until(date(2026, 6, 25), date(2026, 6, 25)) == 0  # on expiry day
    assert mgr._trading_days_until(date(2026, 6, 24), date(2026, 6, 25)) == 1  # Wed -> 1 session
    assert mgr._trading_days_until(date(2026, 6, 23), date(2026, 6, 25)) == 2  # Tue -> 2 sessions
    # Across a weekend: Fri 2026-06-19 -> Tue 2026-06-23 = Mon+Tue = 2 sessions (Sat/Sun skipped)
    assert mgr._trading_days_until(date(2026, 6, 19), date(2026, 6, 23)) == 2
    # Holiday skipped: Thu 2026-06-25 -> Mon 2026-06-29 skips Fri 06-26 (holiday)+weekend = 1
    assert mgr._trading_days_until(date(2026, 6, 25), date(2026, 6, 29)) == 1


def test_rollover_uses_one_trading_day_window(em):
    # With option_rollover_working_days=1 the dated resolver rolls only on the last trading
    # session before expiry (+ expiry day); two sessions out still uses the current expiry.
    mgr, s = em
    s.option_rollover_working_days = 1
    s.expiry_day_trade_current_week = False  # isolate the base rollover (no gamma override)
    mgr.df = _opt_master()
    mgr._ensure_expiry_dates()
    mgr._ensure_ready = lambda: True

    def resolved(d):
        e = mgr._select_option_expiry_for_date("NIFTY", d, "weekly")
        return e.strftime("%d%b%Y").upper() if e is not None else None

    assert resolved(date(2026, 6, 23)) == "25JUN2026"  # 2 sessions out -> current (was rolled at T-2)
    assert resolved(date(2026, 6, 24)) == "02JUL2026"  # 1 session out  -> rolled
    assert resolved(date(2026, 6, 25)) == "02JUL2026"  # expiry day     -> rolled


def test_disabled_by_default_returns_none(em):
    mgr, s = em
    # toggle OFF -> never overrides, regardless of date/time
    assert _ov(mgr, date(2026, 6, 25), datetime(2026, 6, 25, 10, 0)) is None


def test_expiry_day_morning_uses_current_week(em):
    mgr, s = em
    s.expiry_day_trade_current_week = True
    # T-0 before the 13:00 cutoff -> ride the current-week gamma
    assert _ov(mgr, date(2026, 6, 25), datetime(2026, 6, 25, 9, 30)) == "current"
    assert _ov(mgr, date(2026, 6, 25), datetime(2026, 6, 25, 12, 59)) == "current"


def test_expiry_day_after_cutoff_rollover_is_safe_default(em):
    mgr, s = em
    s.expiry_day_trade_current_week = True
    s.expiry_day_after_cutoff_mode = "rollover"
    # the midday lull (cutoff..late-window) reverts to the unchanged rolled (next-week) behaviour
    assert _ov(mgr, date(2026, 6, 25), datetime(2026, 6, 25, 13, 0)) is None
    assert _ov(mgr, date(2026, 6, 25), datetime(2026, 6, 25, 13, 45)) is None


def test_after_cutoff_modes_momentum_and_futures(em):
    mgr, s = em
    s.expiry_day_trade_current_week = True
    s.expiry_day_late_window = False  # isolate the lull-window after-cutoff modes
    after = datetime(2026, 6, 25, 13, 45)  # midday lull
    s.expiry_day_after_cutoff_mode = "momentum_current"
    assert _ov(mgr, date(2026, 6, 25), after) == "current"
    s.expiry_day_after_cutoff_mode = "futures"
    assert _ov(mgr, date(2026, 6, 25), after) == "futures"


def test_expiry_day_late_window_re_engages_gamma(em):
    # [2] The big settlement move often comes in the last hour, so 14:00-15:15 on expiry
    # day re-engages current-expiry gamma (in either direction), skipping the midday lull.
    mgr, s = em
    s.expiry_day_trade_current_week = True
    s.expiry_day_late_window = True
    s.expiry_day_after_cutoff_mode = "rollover"  # lull rolls, but late window overrides
    assert _ov(mgr, date(2026, 6, 25), datetime(2026, 6, 25, 13, 45)) is None        # lull -> rolled
    assert _ov(mgr, date(2026, 6, 25), datetime(2026, 6, 25, 14, 0)) == "current"    # late window opens
    assert _ov(mgr, date(2026, 6, 25), datetime(2026, 6, 25, 15, 0)) == "current"    # still engaged
    assert _ov(mgr, date(2026, 6, 25), datetime(2026, 6, 25, 15, 15)) is None        # closes at 15:15
    # late window overrides even "futures" after-cutoff mode (it's a settlement-move grab)
    s.expiry_day_after_cutoff_mode = "futures"
    assert _ov(mgr, date(2026, 6, 25), datetime(2026, 6, 25, 14, 30)) == "current"
    # toggle the late window off -> 14:00 falls back to the after-cutoff mode
    s.expiry_day_late_window = False
    assert _ov(mgr, date(2026, 6, 25), datetime(2026, 6, 25, 14, 30)) == "futures"


def test_custom_cutoff_is_respected(em):
    mgr, s = em
    s.expiry_day_trade_current_week = True
    s.expiry_day_gamma_cutoff = "11:30"
    assert _ov(mgr, date(2026, 6, 25), datetime(2026, 6, 25, 11, 0)) == "current"
    assert _ov(mgr, date(2026, 6, 25), datetime(2026, 6, 25, 11, 30)) is None  # at cutoff -> rollover


def test_t1_only_when_enabled(em):
    mgr, s = em
    s.expiry_day_trade_current_week = True
    t1 = date(2026, 6, 24)  # day before expiry
    assert _ov(mgr, t1, datetime(2026, 6, 24, 14, 0)) is None       # include_t1 off
    s.expiry_day_include_t1 = True
    # T-1 rides gamma all session (no intraday cutoff the day before expiry)
    assert _ov(mgr, t1, datetime(2026, 6, 24, 9, 30)) == "current"
    assert _ov(mgr, t1, datetime(2026, 6, 24, 14, 0)) == "current"


def test_outside_window_returns_none(em):
    mgr, s = em
    s.expiry_day_trade_current_week = True
    s.expiry_day_include_t1 = True
    # T-2 and earlier are outside the gamma window even with everything enabled
    assert _ov(mgr, date(2026, 6, 23), datetime(2026, 6, 23, 10, 0)) is None
    assert _ov(mgr, date(2026, 6, 22), datetime(2026, 6, 22, 10, 0)) is None


def test_no_override_when_current_equals_next(em):
    mgr, s = em
    s.expiry_day_trade_current_week = True
    # if there is no distinct next expiry there is nothing to roll to / override
    assert mgr.expiry_day_override(CUR, CUR, date(2026, 6, 25), datetime(2026, 6, 25, 10, 0)) is None


def test_missing_time_defaults_to_gamma(em):
    # When no timestamp is supplied (now_dt=None) on expiry day, default to the
    # gamma choice rather than silently rolling.
    mgr, s = em
    s.expiry_day_trade_current_week = True
    assert _ov(mgr, date(2026, 6, 25), None) == "current"


# ─── theta-crush risk controls ──────────────────────────────────────────────

def test_in_gamma_window_tracks_toggle_and_cutoff(em):
    mgr, s = em
    mgr.df = _opt_master()
    mgr._ensure_expiry_dates()
    # toggle OFF -> always False (zero-cost short-circuit)
    assert mgr.in_gamma_window("NIFTY", datetime(2026, 6, 25, 10, 0)) is False
    s.expiry_day_trade_current_week = True
    assert mgr.in_gamma_window("NIFTY", datetime(2026, 6, 25, 10, 0)) is True   # expiry-day morning
    assert mgr.in_gamma_window("NIFTY", datetime(2026, 6, 25, 13, 45)) is False # midday lull
    assert mgr.in_gamma_window("NIFTY", datetime(2026, 6, 25, 14, 30)) is True  # late settlement window
    assert mgr.in_gamma_window("NIFTY", datetime(2026, 6, 24, 10, 0)) is False  # not expiry day


def _gamma_manager(monkeypatch):
    mgr = TradeManager.__new__(TradeManager)
    mgr.open_trades = {}
    mgr._lock = threading.RLock()
    closed = []
    mgr.close_trade = lambda tid, price, reason, exit_time=None: closed.append((tid, reason))
    return mgr, closed


def _opt_trade(tid, entry_dt, inst="OPT"):
    return Trade(
        id=tid, instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=100.0, entry_time=IST.localize(entry_dt), trailing_stop=90.0,
        current_stop=90.0, lots=1, lot_size=75, grade="A", inst_type=inst,
        current_price=100.0,
    )


def test_gamma_time_exit_closes_only_genuine_gamma_trades(em, monkeypatch):
    mgr, s = em
    s.expiry_day_trade_current_week = True
    s.expiry_day_gamma_cutoff = "13:00"
    s.expiry_day_gamma_time_exit = "13:30"
    tm, closed = _gamma_manager(monkeypatch)
    # 2026-06-25 is a Thursday -> NIFTY expiry day
    tm.open_trades = {
        "gamma":  _opt_trade("gamma", datetime(2026, 6, 25, 10, 0)),           # OPT, expiry-day, pre-cutoff -> exit
        "fut":    _opt_trade("fut",   datetime(2026, 6, 25, 10, 0), inst="FUT"),  # not OPT -> keep
        "late":   _opt_trade("late",  datetime(2026, 6, 25, 13, 15)),          # entered post-cutoff -> not gamma
        "nonexp": _opt_trade("nonexp", datetime(2026, 6, 24, 10, 0)),          # not an expiry day -> keep
    }
    # before the time-exit: nothing closes
    tm.check_expiry_gamma_time_exit(IST.localize(datetime(2026, 6, 25, 13, 0)))
    assert closed == []
    # at/after the time-exit: only the genuine gamma trade squares off
    tm.check_expiry_gamma_time_exit(IST.localize(datetime(2026, 6, 25, 13, 30)))
    assert closed == [("gamma", "EXPIRY_GAMMA_TIME_EXIT")]


def test_live_get_token_path_is_gamma_aware(em):
    # Integration guard: the LIVE entry path (scanner.py:8823 / signal_processor:3308)
    # resolves options via get_token (no trade_date), so get_token MUST honour gamma mode
    # using wall-clock time — otherwise gamma works in backtest but silently NOT live.
    from freezegun import freeze_time
    mgr, s = em
    mgr.df = _opt_master()
    mgr._ensure_expiry_dates()
    mgr._ensure_ready = lambda: True
    # daily-computed expiries: T-2 rollover already pushed active_weekly to next week
    mgr.expiries = {"NIFTY": {"weekly": "25JUN2026", "active_weekly": "02JUL2026"}}

    def tok():
        mgr._token_cache.clear()
        r = mgr.get_token("NIFTY", 24000, "CE", "active_weekly")
        return r["expiry"] if r else None

    s.expiry_day_trade_current_week = False
    with freeze_time("2026-06-25 10:00:00"):
        assert tok() == "02JUL2026"           # OFF -> rolled (unchanged live behaviour)
    s.expiry_day_trade_current_week = True
    with freeze_time("2026-06-25 10:00:00"):
        assert tok() == "25JUN2026"           # ON morning -> CURRENT expiring (gamma) live
    with freeze_time("2026-06-25 13:45:00"):
        assert tok() == "02JUL2026"           # ON midday lull (rollover) -> rolled back
    with freeze_time("2026-06-25 14:30:00"):
        assert tok() == "25JUN2026"           # ON late window -> current expiring again


def test_gamma_time_exit_noop_when_toggle_off(em, monkeypatch):
    mgr, s = em
    s.expiry_day_trade_current_week = False
    tm, closed = _gamma_manager(monkeypatch)
    tm.open_trades = {"gamma": _opt_trade("gamma", datetime(2026, 6, 25, 10, 0))}
    tm.check_expiry_gamma_time_exit(IST.localize(datetime(2026, 6, 25, 14, 0)))
    assert closed == []  # gated off -> never touches trades
