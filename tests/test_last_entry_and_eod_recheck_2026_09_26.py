"""2026-09-26 user: normal trading runs from 09:15 to the 15:15 hard exit. The last new
entry is 15m 14:50 (entries stop at 14:51), 5m 15:10 and 1m 15:14 on every engine, and
every exit is rechecked by 15:15:30.

The other engines' 5m/1m entries still end when their own EOD protection starts (15:01),
because a position opened inside that window is closed on the next tick.
"""
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from config.settings import Settings
from scanner import Scanner
from trading.trade_manager import IST, TradeManager


def _scanner(monkeypatch, **overrides):
    s = Settings()
    for key, value in overrides.items():
        object.__setattr__(s, key, value)
    monkeypatch.setattr(scanner_module, "get_settings", lambda: s)
    sc = Scanner.__new__(Scanner)
    sc._live_signal_stabilization_requirements = lambda c: (0.0, 1, False)
    sc._get_trade_regime = lambda c, default="UNKNOWN": "RANGING"
    return sc


def _at(hhmm, sec=0):
    hh, mm = map(int, hhmm.split(":"))
    return datetime(2026, 9, 24, hh, mm, sec)


@pytest.mark.parametrize("engine,lane,tf,bar,now,allowed", [
    # the lanes run to the system-wide last entries
    ("ut_smart", "UTS", "5min", "15:05", ("15:10", 5), True),
    ("s1", "S1", "1min", "15:13", ("15:14", 2), True),
    ("s1", "S1", "15min", "14:45", ("15:00", 5), False),
    # the other engines: 15m to 14:51, 5m/1m capped at their 15:01 EOD-protect start
    ("utbot", "", "15min", "14:30", ("14:45", 5), True),
    ("utbot", "", "15min", "14:45", ("14:50", 30), True),       # formed by 14:50, 60s grace
    ("utbot", "", "15min", "14:45", ("14:51", 5), False),
    ("utbot", "", "5min", "14:55", ("15:00", 5), True),         # closes 15:00 < 15:01
    ("utbot", "", "5min", "15:00", ("15:05", 5), False),        # closes inside EOD protection
    ("utbot", "", "1min", "15:05", ("15:06", 2), False),
])
def test_the_last_entry_for_every_engine(monkeypatch, engine, lane, tf, bar, now, allowed):
    sc = _scanner(monkeypatch)
    cand = SimpleNamespace(instrument="NIFTY", direction="LONG", timeframe=tf, source_engine=engine,
                           source_lane=lane, signal_timestamp=_at(bar))
    assert sc._entry_cutoff_allows_candidate(cand, IST.localize(_at(*now))) is allowed


def test_the_1m_last_entry_is_its_own_setting():
    s = Settings()
    assert (s.ut_no_entry_after, s.ut_5min_no_entry_after, s.ut_1min_no_entry_after) == ("14:50", "15:10", "15:14")
    assert s.live_15min_entry_cutoff_grace_seconds == 60.0


# ---------------------------------------------------------------- 15:15:30 exit recheck


def _manager(open_trades=None, orphans=None, live=True):
    tm = TradeManager.__new__(TradeManager)
    tm._lock = threading.RLock()
    tm.open_trades = dict(open_trades or {})
    tm.orphan_positions = list(orphans or [])
    tm.reconciles, tm.alerts = [], []
    tm._live_orders_permitted = lambda: live
    tm._session_is_a_trading_day = lambda now: True
    tm.reconcile_broker_positions = lambda: tm.reconciles.append(1) or {"checked": True}
    tm._log_ui = lambda message, level="info": tm.alerts.append((level, message))
    return tm


def test_the_recheck_runs_once_at_151530_and_names_what_is_still_open(monkeypatch):
    s = Settings()
    object.__setattr__(s, "ut_force_exit_time", "15:15")
    monkeypatch.setattr("config.settings.get_settings", lambda: s)
    tm = _manager({"T1": SimpleNamespace(trading_symbol="NIFTY26SEP23100PE")})
    assert tm.eod_exit_recheck(IST.localize(_at("15:15", 20))) is False and tm.reconciles == []
    assert tm.eod_exit_recheck(IST.localize(_at("15:15", 31))) is True
    assert tm.reconciles == [1]
    assert tm.alerts and tm.alerts[0][0] == "error" and "NIFTY26SEP23100PE" in tm.alerts[0][1]
    assert tm.eod_exit_recheck(IST.localize(_at("15:16", 0))) is False        # once a day
    assert tm.reconciles == [1]


def test_a_flat_book_is_still_reconciled_and_raises_no_alert(monkeypatch):
    s = Settings()
    object.__setattr__(s, "ut_force_exit_time", "15:15")
    monkeypatch.setattr("config.settings.get_settings", lambda: s)
    tm = _manager()
    assert tm.eod_exit_recheck(IST.localize(_at("15:15", 45))) is True
    assert tm.reconciles == [1] and tm.alerts == []
    paper = _manager({"T1": SimpleNamespace(trading_symbol="X")}, live=False)
    assert paper.eod_exit_recheck(IST.localize(_at("15:15", 45))) is True
    assert paper.reconciles == [] and paper.alerts                         # no broker call outside REAL


def test_the_scan_loop_calls_the_recheck_every_pass():
    import inspect

    src = inspect.getsource(Scanner)
    i = src.index("self._force_exit_session_candidates_at_eod(session_now)")
    assert "self.trades.eod_exit_recheck(session_now)" in src[i:i + 600]
