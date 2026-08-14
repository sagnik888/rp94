"""A signal row must not outlive its own session's square-off.

Observed 2026-08-15 00:05, on a running scanner: the dashboard was still showing
a BANKNIFTY 57400PE ENTRY from 12:37 the previous day as a live intraday signal,
marking P&L (-Rs 1,076) against a position that had been closed at 13:10.

Both halves are real. The row was orphaned first -- its OPPOSITE_SIGNAL exit was
recorded under lifecycle timestamp 12:35:00 while the missed-re-entry ENTRY
carried 12:37:25.377954, so _candidate_lifecycle_key never paired them and
_active_session_entry_candidates kept reporting it active. That alone should have
been harmless: _force_exit_session_candidates_at_eod is the net that closes any
row still active at the hard square-off.

The net had a window. It returned early unless now.time() >= ut_force_exit_time,
then skipped any row whose date was not today's:

    if now.time() < trigger_time:                          # 00:05 < 15:15 -> return
        return 0
    exit_ts = IST.localize(datetime.combine(now.date(), exit_time))
    ...
    if prev_ist.date() != now.date() or prev_ist >= exit_ts:   # 14th != 15th -> skip

So it could only ever act between square-off and midnight, on the row's own day.
The process was down across that whole window on 2026-08-14 -- a crash -- and
after midnight neither condition can be satisfied again for that row. It was
unclosable for as long as it stayed in the ledger.

Now each row is closed at ITS OWN session's square-off, which keeps the intraday
guarantee (a row from today is untouched until today's cutoff passes) and adds
the missing one (a row from any earlier session is closed at the first scan after
restart, whenever that is).
"""
from datetime import datetime, time as dt_time

import pytest
import pytz

from engine.signal_processor import TradeCandidate
from scanner import Scanner

IST = pytz.timezone("Asia/Kolkata")


def _scanner():
    s = Scanner.__new__(Scanner)
    s.mode = "REAL"
    s.config = {"indices": {"BANKNIFTY": {}}}
    s._session_trade_candidates = {}
    s._latest_exit_candidates = {}
    s._last_opposite_exit_signal_time = {}
    s._configured_session_time = lambda key, default: dt_time(15, 15)
    s._get_live_premium_cached = lambda candidate: getattr(candidate, "current_price", 0.0)
    s._candidate_exit_resolution = lambda prev, px, code, text: (float(px), code, text)
    s._candidate_net_pnl = lambda prev, px: 0.0
    s._inherit_exit_entry_metadata = lambda exit_candidate, previous: None
    s._refresh_trade_candidates = lambda candidates, cfg: None
    s._remember_trade_candidates = lambda candidates: None
    s._persist_session_trade_candidates = lambda: None
    s._ensure_session_candidate_day = lambda: None
    s._normalize_loaded_exit_candidate = lambda candidate: None
    s._candidate_matches_live_session_day = lambda candidate: True
    s.log_event = lambda *a, **k: None
    return s


def _entry(ts, instrument="BANKNIFTY"):
    return TradeCandidate(
        instrument=instrument,
        direction="SHORT",
        price=361.5,
        stop=400.0,
        target=300.0,
        lots=1,
        lot_size=30,
        grade="B",
        confidence=0.97,
        timeframe="5min",
        inst_type="OPT",
        option_type="PE",
        atm_strike=57400.0,
        multiplier=0.5,
        trading_symbol="BANKNIFTY26AUG57400PE",
        symbol_token="57400",
        signal_timestamp=ts,
        current_price=328.3,
        action="ENTRY",
        status="MISSED RE-ENTRY SIGNAL",
    )


def _load(s, *rows):
    book = {}
    for row in rows:
        book[s._candidate_history_key(row)] = row
    s._session_trade_candidates = {"BANKNIFTY": book}


# ── the reported failure ─────────────────────────────────────────────────────

def test_a_row_from_a_previous_session_is_closed_after_midnight():
    """The regression: at 00:05 the sweep returned 0 and the row stayed live."""
    s = _scanner()
    _load(s, _entry(datetime(2026, 8, 14, 12, 37, 25, 377954)))

    closed = s._force_exit_session_candidates_at_eod(IST.localize(datetime(2026, 8, 15, 0, 5)))

    assert closed == 1


def test_the_exit_is_stamped_at_that_rows_own_squareoff_not_todays():
    """A row from the 14th must close at 15:15 on the 14th, not on the 15th."""
    s = _scanner()
    _load(s, _entry(datetime(2026, 8, 14, 12, 37, 25, 377954)))
    captured = []
    s._remember_trade_candidates = lambda rows: captured.extend(rows)

    s._force_exit_session_candidates_at_eod(IST.localize(datetime(2026, 8, 15, 0, 5)))

    assert captured[0].exit_timestamp == datetime(2026, 8, 14, 15, 15)


def test_a_restart_days_later_still_closes_it():
    """The window used to be one evening; a weekend outage must not defeat it."""
    s = _scanner()
    _load(s, _entry(datetime(2026, 8, 14, 12, 37)))

    assert s._force_exit_session_candidates_at_eod(IST.localize(datetime(2026, 8, 18, 9, 30))) == 1


# ── the intraday guarantee is unchanged ──────────────────────────────────────

def test_a_live_row_is_not_closed_before_its_own_squareoff():
    """Mid-session, today's signal must keep running -- this is the whole point."""
    s = _scanner()
    _load(s, _entry(datetime(2026, 8, 14, 12, 37)))

    assert s._force_exit_session_candidates_at_eod(IST.localize(datetime(2026, 8, 14, 13, 0))) == 0


def test_todays_row_closes_once_todays_squareoff_passes():
    s = _scanner()
    _load(s, _entry(datetime(2026, 8, 14, 12, 37)))

    assert s._force_exit_session_candidates_at_eod(IST.localize(datetime(2026, 8, 14, 15, 30))) == 1


def test_a_signal_fired_after_the_cutoff_is_not_force_closed():
    """prev_ist >= exit_ts: it never traded into the square-off window."""
    s = _scanner()
    _load(s, _entry(datetime(2026, 8, 14, 15, 20)))

    assert s._force_exit_session_candidates_at_eod(IST.localize(datetime(2026, 8, 14, 15, 30))) == 0


# ── it stays idempotent ──────────────────────────────────────────────────────

def test_the_row_is_not_closed_twice():
    """The dedupe key is stamped with the row's own square-off, so it still holds."""
    s = _scanner()
    _load(s, _entry(datetime(2026, 8, 14, 12, 37)))
    now = IST.localize(datetime(2026, 8, 15, 0, 5))

    assert s._force_exit_session_candidates_at_eod(now) == 1
    assert s._force_exit_session_candidates_at_eod(now) == 0


def test_historical_mode_is_untouched():
    s = _scanner()
    s.mode = "HISTORICAL"
    _load(s, _entry(datetime(2026, 8, 14, 12, 37)))

    assert s._force_exit_session_candidates_at_eod(IST.localize(datetime(2026, 8, 15, 0, 5))) == 0


def test_an_empty_ledger_is_a_no_op():
    assert _scanner()._force_exit_session_candidates_at_eod(
        IST.localize(datetime(2026, 8, 15, 0, 5))
    ) == 0
