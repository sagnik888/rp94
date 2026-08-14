"""A shutdown must not strand a signal row in a closed session.

_load_session_trade_candidates only loads the CURRENT session day, and
_force_exit_session_candidates_at_eod only sweeps what is loaded. So a row left
open when the process dies becomes unreachable the moment the date rolls over:
nothing loads it, nothing closes it, and the ledger keeps reporting that trade as
never exited. Replaying that session then reads an open position that in reality
closed hours earlier.

Observed on 2026-08-14. The process was down from before the 15:15 square-off
until after midnight. A BANKNIFTY 57400PE entry stamped 12:37:25.377954 stayed
open with its own exit sitting right beside it in the same ledger -- recorded
under lifecycle timestamp 12:35:00, filled 13:10 at 349.15, OPPOSITE_SIGNAL --
simply never matched to it.

_reconcile_prior_session_open_rows settles these at startup, preferring the
truth already on disk:

  ADOPT   an unclaimed exit for the same contract, re-keyed to the entry it
          belongs to. Claims nothing new -- the fill, price and time are the ones
          the session actually produced.
  SQUARE  only when no exit exists at all: a close at that session's own hard
          square-off, marked STALE_SESSION_RECONCILE so a replay can tell a
          reconstructed close from a real one.
"""
from datetime import datetime, time as dt_time

import pytest
import pytz

from scanner import Scanner

IST = pytz.timezone("Asia/Kolkata")


class _Ledger:
    """Stands in for the SQLite session-signal store."""

    def __init__(self, books):
        self.books = {d: [dict(r) for r in rows] for d, rows in books.items()}
        self.saves = []

    def list_session_signal_dates(self):
        return sorted(self.books)

    def load_session_signals(self, day):
        return [dict(r) for r in self.books.get(day, [])]

    def save_session_signals(self, day, rows):
        self.books[day] = [dict(r) for r in rows]
        self.saves.append(day)


def _scanner(ledger, current_day="2026-08-15"):
    s = Scanner.__new__(Scanner)
    s.mode = "REAL"
    s._session_candidate_day = current_day
    s._get_current_session_day = lambda: current_day
    s._configured_session_time = lambda key, default: dt_time(15, 15)
    s.log_event = lambda *a, **k: None
    return s


def _entry(ts="2026-08-14T12:37:25.377954"):
    return {
        "id": f"SESSION|BANKNIFTY|5min|ENTRY|SHORT|{ts}",
        "action": "ENTRY", "status": "MISSED RE-ENTRY SIGNAL",
        "instrument": "BANKNIFTY", "timeframe": "5min", "direction": "SHORT",
        "trading_symbol": "BANKNIFTY26AUG57400PE",
        "signal_timestamp": ts, "source_signal_timestamp": ts,
        "price": 361.5, "current_price": 328.3, "exit_timestamp": "",
    }


def _exit(source_ts="2026-08-14T12:35:00", filled="2026-08-14T13:10:00", reason="OPPOSITE_SIGNAL"):
    return {
        "id": f"SESSION|BANKNIFTY|5min|EXIT|SHORT|{source_ts}",
        "action": "EXIT", "status": "EXIT SIGNAL",
        "instrument": "BANKNIFTY", "timeframe": "5min", "direction": "SHORT",
        "trading_symbol": "BANKNIFTY26AUG57400PE",
        "signal_timestamp": source_ts, "source_signal_timestamp": source_ts,
        "price": 361.5, "current_price": 349.15,
        "exit_timestamp": filled, "exit_reason": reason,
    }


def _run(monkeypatch, ledger, current_day="2026-08-15", now=datetime(2026, 8, 15, 0, 5)):
    import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "db", ledger)
    return _scanner(ledger, current_day)._reconcile_prior_session_open_rows(IST.localize(now))


# ── adopt the exit that was already there ────────────────────────────────────

def test_the_orphan_is_matched_to_its_own_recorded_exit(monkeypatch):
    """The reported case: the exit existed, it was just keyed 12:35 not 12:37."""
    ledger = _Ledger({"2026-08-14": [_entry(), _exit()]})

    assert _run(monkeypatch, ledger) == 1

    exit_row = next(r for r in ledger.books["2026-08-14"] if r["action"] == "EXIT")
    assert exit_row["source_signal_timestamp"] == "2026-08-14T12:37:25.377954"


def test_adoption_invents_no_price_or_time(monkeypatch):
    """It must report what the session produced, not a reconstruction."""
    ledger = _Ledger({"2026-08-14": [_entry(), _exit()]})

    _run(monkeypatch, ledger)

    exit_row = next(r for r in ledger.books["2026-08-14"] if r["action"] == "EXIT")
    assert exit_row["exit_timestamp"] == "2026-08-14T13:10:00"
    assert exit_row["exit_reason"] == "OPPOSITE_SIGNAL"
    assert exit_row["current_price"] == 349.15
    assert not any(r.get("exit_reason") == "STALE_SESSION_RECONCILE" for r in ledger.books["2026-08-14"])


def test_the_pair_matches_afterwards(monkeypatch):
    """The whole point: the two rows must now share a lifecycle key."""
    ledger = _Ledger({"2026-08-14": [_entry(), _exit()]})

    _run(monkeypatch, ledger)

    rows = ledger.books["2026-08-14"]
    keys = {r["action"]: Scanner._payload_lifecycle_key(r) for r in rows}
    assert keys["ENTRY"] == keys["EXIT"]


def test_an_exit_already_owned_by_another_entry_is_not_stolen(monkeypatch):
    """A correctly paired exit belongs to its entry and must stay there."""
    paired_entry = _entry(ts="2026-08-14T11:00:00")
    paired_exit = _exit(source_ts="2026-08-14T11:00:00", filled="2026-08-14T11:30:00")
    ledger = _Ledger({"2026-08-14": [paired_entry, paired_exit, _entry()]})

    _run(monkeypatch, ledger)

    still_paired = [r for r in ledger.books["2026-08-14"]
                    if r["action"] == "EXIT" and r["source_signal_timestamp"] == "2026-08-14T11:00:00"]
    assert len(still_paired) == 1, "a matched exit was re-keyed away from its own entry"


def test_an_exit_that_predates_the_entry_is_not_adopted(monkeypatch):
    """An exit filled before the entry existed cannot be that entry's exit."""
    ledger = _Ledger({"2026-08-14": [_entry(), _exit(filled="2026-08-14T09:30:00")]})

    _run(monkeypatch, ledger)

    synthetic = [r for r in ledger.books["2026-08-14"] if r.get("exit_reason") == "STALE_SESSION_RECONCILE"]
    assert len(synthetic) == 1, "should have squared off rather than adopted a pre-dated exit"


def test_a_different_contract_is_not_adopted(monkeypatch):
    """Same index, different strike: not the same position."""
    other = _exit()
    other["trading_symbol"] = "BANKNIFTY26AUG57500PE"
    ledger = _Ledger({"2026-08-14": [_entry(), other]})

    _run(monkeypatch, ledger)

    assert any(r.get("exit_reason") == "STALE_SESSION_RECONCILE" for r in ledger.books["2026-08-14"])


# ── square off only when there is no exit at all ─────────────────────────────

def test_a_row_with_no_exit_is_squared_off_at_its_own_session_cutoff(monkeypatch):
    ledger = _Ledger({"2026-08-14": [_entry()]})

    assert _run(monkeypatch, ledger) == 1

    synthetic = next(r for r in ledger.books["2026-08-14"] if r["action"] == "EXIT")
    assert synthetic["exit_timestamp"] == "2026-08-14T15:15:00"
    assert synthetic["exit_reason"] == "STALE_SESSION_RECONCILE"
    assert synthetic["exit_price"] == 328.3


def test_a_reconstructed_close_is_labelled_so_replay_can_tell(monkeypatch):
    ledger = _Ledger({"2026-08-14": [_entry()]})

    _run(monkeypatch, ledger)

    assert all(
        r.get("exit_reason") != "OPPOSITE_SIGNAL"
        for r in ledger.books["2026-08-14"] if r["action"] == "EXIT"
    )


# ── boundaries ───────────────────────────────────────────────────────────────

def test_the_current_session_is_left_alone(monkeypatch):
    """Today's open rows are live signals; the EOD sweep owns them, not this."""
    ledger = _Ledger({"2026-08-15": [_entry(ts="2026-08-15T12:37:00")]})

    assert _run(monkeypatch, ledger) == 0
    assert ledger.saves == []


def test_a_fully_paired_day_is_not_rewritten(monkeypatch):
    ledger = _Ledger({"2026-08-14": [_entry(), _exit(source_ts="2026-08-14T12:37:25.377954")]})

    assert _run(monkeypatch, ledger) == 0
    assert ledger.saves == []


def test_it_is_idempotent(monkeypatch):
    """Every restart runs this; the second pass must find nothing to do."""
    ledger = _Ledger({"2026-08-14": [_entry(), _exit()]})

    assert _run(monkeypatch, ledger) == 1
    assert _run(monkeypatch, ledger) == 0


def test_historical_mode_never_rewrites_the_ledger(monkeypatch):
    import scanner as scanner_module

    ledger = _Ledger({"2026-08-14": [_entry()]})
    monkeypatch.setattr(scanner_module, "db", ledger)
    s = _scanner(ledger)
    s.mode = "HISTORICAL"

    assert s._reconcile_prior_session_open_rows(IST.localize(datetime(2026, 8, 15, 0, 5))) == 0
    assert ledger.saves == []


def test_an_unreadable_ledger_does_not_stop_startup(monkeypatch):
    import scanner as scanner_module

    class _Broken:
        def list_session_signal_dates(self):
            raise RuntimeError("db locked")

    monkeypatch.setattr(scanner_module, "db", _Broken())

    assert _scanner(None)._reconcile_prior_session_open_rows(
        IST.localize(datetime(2026, 8, 15, 0, 5))
    ) == 0


def test_one_bad_day_does_not_abandon_the_others(monkeypatch):
    import scanner as scanner_module

    ledger = _Ledger({"2026-08-13": [_entry()], "2026-08-14": [_entry(), _exit()]})
    original = ledger.load_session_signals

    def _flaky(day):
        if day == "2026-08-13":
            raise RuntimeError("corrupt row")
        return original(day)

    ledger.load_session_signals = _flaky
    monkeypatch.setattr(scanner_module, "db", ledger)

    assert _scanner(ledger)._reconcile_prior_session_open_rows(
        IST.localize(datetime(2026, 8, 15, 0, 5))
    ) == 1


def test_the_scan_cycle_runs_it_once_at_startup():
    """Pinned: it must be wired, and must not re-run every cycle."""
    import inspect

    src = inspect.getsource(Scanner._scan_cycle)

    assert "_reconcile_prior_session_open_rows(" in src, "reconcile not wired into startup"
    assert "_prior_session_reconciled" in src, "must run once per process, not every cycle"
