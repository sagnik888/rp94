"""2026-10-07 14:02:48: one tz-aware EXIT stamp stopped every scan cycle.

An OPPOSITE_SIGNAL session exit was stamped with the aware square-off clock while every
other ledger row is naive IST; _active_session_entry_candidates then raised
"can't compare offset-naive and offset-aware datetimes" on every cycle until restart.
"""
from datetime import datetime
from types import SimpleNamespace

import pytz

from scanner import Scanner

IST = pytz.timezone("Asia/Kolkata")


def _row(action, signal_ts, exit_ts=None, key="MIDCP|5min|SHORT"):
    return SimpleNamespace(action=action, timeframe="5min", signal_timestamp=signal_ts,
                           exit_timestamp=exit_ts, accepted_by_gate=True, _key=key)


def _scanner(rows):
    sc = Scanner.__new__(Scanner)
    sc._get_session_trade_candidates = lambda instrument: list(rows)
    sc._candidate_lifecycle_key = lambda row: row._key
    return sc


def test_an_aware_exit_stamp_no_longer_breaks_the_ledger_filter():
    entry = _row("ENTRY", datetime(2026, 10, 7, 13, 50))
    aware_exit = _row("EXIT", datetime(2026, 10, 7, 13, 50),
                      IST.localize(datetime(2026, 10, 7, 14, 2, 47)))
    other = _row("ENTRY", datetime(2026, 10, 7, 14, 10), key="NIFTY|5min|LONG")
    sc = _scanner([entry, aware_exit, other])
    assert sc._active_session_entry_candidates("MIDCPNIFTY") == [other]
    assert sc._active_session_entry_candidates(
        "MIDCPNIFTY", as_of=IST.localize(datetime(2026, 10, 7, 14, 30))) == [other]


def test_session_exit_stamps_are_written_naive():
    import inspect

    src = inspect.getsource(Scanner._handle_opposite_signal_exit)
    assert "exit_time = Scanner._naive_ist(" in src
    sup = inspect.getsource(Scanner._close_superseded_session_entries)
    assert "Scanner._naive_ist(self._session_squareoff_clock())" in sup
