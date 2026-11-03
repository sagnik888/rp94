"""Native paper ownership crosses dates without inventing historical exits/ticks."""
import copy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import scanner as scanner_module
from scanner import Scanner
from engine import ut_ema_lane


TODAY = "2026-10-29"
PREVIOUS = "2026-10-28"


def payload(identity="native-one", **changes):
    state = {"lifecycle_id": identity, "entry_engine": "UT", "side": 1,
        "entry_spot": 1000., "initial_stop": 998., "stop": 1001.,
        "entry_time": PREVIOUS + "T15:20:00", "entry_bar": PREVIOUS + "T15:20:00",
        "premium_risk_pct": 2., "rr": 2.5, "option_delta": 1., "alignment_added": True}
    row = {"id": identity, "lifecycle_id": identity, "instrument": "NIFTY", "timeframe": "5min",
        "direction": "LONG", "action": "ENTRY", "status": "TRADE SIGNAL", "inst_type": "OPT",
        "option_type": "CE", "atm_strike": 1000., "trading_symbol": "NIFTY_TEST_CE", "symbol_token": "123",
        "price": 100., "stop": 98., "target": 105., "lots": 3, "lot_size": 75, "rr": 2.5,
        "current_price": 101., "initial_stop": 98., "entry_spot": 1000., "spot_stop": 998.,
        "signal_timestamp": PREVIOUS + "T15:20:00", "entry_timestamp": PREVIOUS + "T15:20:01",
        "accepted_by_gate": True, "quote_verified": True, "source_engine": "ut_ema",
        "source_lane": "UTEMA", "exit_policy_owner": "UTEMA", "utema_state": state,
        "quality_verdict": {"allowed": True, "checks": {"utema_state": copy.deepcopy(state)}}}
    row.update(changes)
    return row


class Ledger:
    def __init__(self, books):
        self.books = copy.deepcopy(books)
        self.saves = []

    def list_session_signal_dates(self):
        return sorted(self.books)

    def load_session_signals(self, day):
        return copy.deepcopy(self.books.get(day, []))

    def save_session_signals(self, day, rows):
        # Mirror SQLite's date ownership rule without touching any real database.
        assert all(str(row.get("signal_timestamp", day))[:10] == day for row in rows)
        self.books[day] = copy.deepcopy(rows)
        self.saves.append(day)


def host(monkeypatch, books=None):
    ledger = Ledger(books or {})
    monkeypatch.setattr(scanner_module, "db", ledger)
    value = Scanner.__new__(Scanner)
    value.mode = "REAL"
    value._session_candidate_day = TODAY
    value._loaded_session_candidate_day = TODAY
    value._get_current_session_day = lambda: TODAY
    value._process_started_at = datetime(2026, 10, 29, 9, 0)
    value._session_trade_candidates = {}
    value.trades = SimpleNamespace(closed_trades=[], open_trades={})
    value._intracandle_signals_enabled = lambda: True
    value._normalize_loaded_exit_candidate = lambda candidate: None
    value._resubscribe_loaded_candidate_quote = Mock()
    value._repair_same_side_superseded_session_rows = lambda: False
    value._rehydrate_live_diagnostics_from_session = Mock()
    return value, ledger


def rows(value):
    return [row for book in value._session_trade_candidates.values() for row in book.values()]


def test_restart_loads_unexited_native_entry_with_frozen_guards_and_no_tick_claim(monkeypatch):
    value, ledger = host(monkeypatch, {PREVIOUS: [payload()]})
    value._load_session_trade_candidates()
    assert len(rows(value)) == 1
    candidate = rows(value)[0]
    assert candidate.lots == 3
    assert candidate.signal_timestamp == datetime(2026, 10, 28, 15, 20)
    assert ut_ema_lane.state(candidate)["stop"] == 1001.
    assert ut_ema_lane.state(candidate)["premium_risk_pct"] == 2.
    assert ut_ema_lane.state(candidate)["alignment_added"] is True
    assert ut_ema_lane.state(candidate)["live_tick_path_restored"] is False
    assert candidate.live_quote_stale is True
    assert value._candidate_matches_live_session_day(candidate)
    assert value._active_session_entry_candidates("NIFTY") == [candidate]
    assert value._resubscribe_loaded_candidate_quote.call_count == 1
    assert not ledger.saves
    assert all(row.action == "ENTRY" for row in rows(value))


def test_rollover_keeps_latest_in_memory_native_alignment_and_stop(monkeypatch):
    value, ledger = host(monkeypatch, {PREVIOUS: [payload()]})
    candidate = value._candidate_from_payload(payload())
    candidate.lots = 4
    ut_ema_lane.state(candidate)["stop"] = 1002.
    value._session_trade_candidates = {"NIFTY": {"old": candidate}}
    value._session_candidate_day = value._loaded_session_candidate_day = PREVIOUS
    value._ensure_session_candidate_day()
    assert value._session_candidate_day == TODAY
    assert rows(value) == [candidate]
    assert rows(value)[0] is candidate
    assert candidate.lots == 4
    assert ut_ema_lane.state(candidate)["stop"] == 1002.
    assert "live_tick_path_restored" not in ut_ema_lane.state(candidate)


@pytest.mark.parametrize("closed_day", [PREVIOUS, TODAY])
def test_recorded_native_exit_prevents_carry_even_across_dated_books(monkeypatch, closed_day):
    closed = payload(action="EXIT", status="EXIT SIGNAL", exit_timestamp=closed_day + "T09:17:00")
    books = {PREVIOUS: [payload(), closed]}
    value, ledger = host(monkeypatch, books)
    value._load_session_trade_candidates()
    assert not rows(value)


def test_closed_trade_manager_native_lifecycle_prevents_orphan_paper_carry(monkeypatch):
    value, ledger = host(monkeypatch, {PREVIOUS: [payload()]})
    trade = SimpleNamespace(source_engine="ut_ema", source_lane="UTEMA", utema_state={"lifecycle_id": "native-one"})
    value.trades.closed_trades = [trade]
    value._load_session_trade_candidates()
    assert not rows(value)


def test_generic_previous_session_and_unaccepted_native_rows_do_not_carry(monkeypatch):
    generic = payload("generic", source_engine="ema", source_lane="", exit_policy_owner="GENERIC", utema_state={})
    unaccepted = payload("not-admitted", accepted_by_gate=False)
    generic_owner = payload("frozen-generic", exit_policy_owner="GENERIC")
    value, ledger = host(monkeypatch, {PREVIOUS: [generic, unaccepted, generic_owner]})
    value._load_session_trade_candidates()
    assert not rows(value)
    assert not value._candidate_matches_live_session_day(value._candidate_from_payload(generic))
    assert not value._candidate_matches_live_session_day(value._candidate_from_payload(unaccepted))


def test_carry_match_rejects_future_native_and_admits_observed_overnight_exit(monkeypatch):
    value, ledger = host(monkeypatch)
    future = value._candidate_from_payload(payload(signal_timestamp="2026-10-30T09:20:00"))
    assert not value._candidate_matches_live_session_day(future)
    closed = value._candidate_from_payload(payload(action="EXIT", exit_timestamp=TODAY + "T09:17:00"))
    assert value._candidate_matches_live_session_day(closed)


def test_native_carry_save_merges_original_date_and_restart_does_not_resurrect_exit(monkeypatch):
    generic = payload("generic", source_engine="ema", source_lane="", exit_policy_owner="GENERIC", utema_state={})
    value, ledger = host(monkeypatch, {PREVIOUS: [generic, payload()]})
    updated = payload(lots=4, current_price=102.)
    closed = payload(action="EXIT", status="EXIT SIGNAL", exit_timestamp=TODAY + "T09:17:00", exit_reason="UTEMA_EMA_T2")
    today = payload("today", signal_timestamp=TODAY + "T09:20:00")
    value._save_session_signal_rows(TODAY, [updated, closed, today])
    assert ledger.saves == [PREVIOUS, TODAY]
    assert next(row for row in ledger.books[PREVIOUS] if row["id"] == "generic") == generic
    native_rows = [row for row in ledger.books[PREVIOUS] if row["id"] == "native-one"]
    assert len(native_rows) == 2
    assert next(row for row in native_rows if row["action"] == "ENTRY")["lots"] == 4
    assert next(row for row in native_rows if row["action"] == "EXIT")["exit_timestamp"] == TODAY + "T09:17:00"
    value._load_session_trade_candidates()
    assert [row.lifecycle_id for row in rows(value)] == ["today"]


def test_historical_load_does_not_read_prior_native_ledgers(monkeypatch):
    value, ledger = host(monkeypatch, {PREVIOUS: [payload()]})
    value.mode = "HISTORICAL"
    value.historical_session_cutoff_date = lambda: datetime.fromisoformat(TODAY).date()
    ledger.list_session_signal_dates = Mock(side_effect=AssertionError("Historical session carry"))
    value._load_session_trade_candidates()
    assert not rows(value)
    ledger.list_session_signal_dates.assert_not_called()


def test_profitable_native_stop_survives_ownership_restore(monkeypatch):
    value,ledger=host(monkeypatch,{PREVIOUS:[payload(stop=101.,target=105.)]})
    value._load_session_trade_candidates()
    assert len(rows(value))==1
    candidate=rows(value)[0]
    assert candidate.stop==101.
    assert value._restored_entry_quality_is_current(candidate)
    assert not ut_ema_lane.entry_allowed(candidate)[0]
    assert ut_ema_lane.held_state_valid(candidate)
