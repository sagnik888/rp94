"""Production writer/reader, monitor isolation, and common option reservations."""
from datetime import datetime, timedelta, time
from types import SimpleNamespace
import threading

import pytest
import pytz

from config.settings import get_settings
from engine import option_exposure, s1_lane
from scanner import Scanner
from tests.test_signal_exit_uses_decision_clock_2026_10_07 import _entry, _host

IST = pytz.timezone("Asia/Kolkata")
NOW = datetime(2026, 10, 8, 14, 2, 47)


@pytest.mark.parametrize("writer", ["opposite", "superseded"])
def test_aware_exit_writers_freeze_extrema_and_decisions_before_entry_keeps_marking(writer):
    host, entry, recorded = _host()
    entry.accepted_by_gate = True
    entry.peak_pnl, entry.max_drawdown = 1452., -402.
    entry.peak_pnl_basis = entry.max_drawdown_basis = entry.pnl_path_basis = "GROSS_BEFORE_ROUND_TRIP_COST"
    entry.pnl_path = [(1., -402.), (2., 1452.)]
    entry.round_trip_cost = 80.
    host._session_squareoff_clock = lambda: IST.localize(NOW)
    host._get_session_trade_candidates = lambda *_a: [entry] + recorded
    del host.__dict__["_active_session_entry_candidates"]
    bar = NOW - timedelta(minutes=1)
    if writer == "opposite":
        assert host._handle_opposite_signal_exit("SENSEX", SimpleNamespace(signal_type="SELL", timestamp=bar, price=72900.), "15min")
    else:
        host._refresh_same_direction_session_lifecycle = lambda *_a: False
        host._should_supersede_session_entry = lambda *_a: True
        host._close_superseded_session_entries([_entry("SHORT", bar)])
    row = recorded[0]
    assert row.exit_timestamp == NOW and row.exit_timestamp.tzinfo is None
    assert (row.peak_pnl, row.max_drawdown) == (1452., -402.)
    assert row.peak_pnl_basis == entry.peak_pnl_basis
    assert row.exit_decision_record["peak_pnl"] == 1452.
    assert row.exit_decision_record["max_drawdown"] == -402.
    assert row.exit_decision_record["decided_at"] == NOW.isoformat()
    assert host._active_session_entry_candidates("SENSEX", as_of=IST.localize(NOW)) == []
    entry.peak_pnl = 9000.
    entry.pnl_path.append((3., 9000.))
    # Restoring entry identity must never overwrite the frozen exit excursion.
    host._inherit_exit_entry_metadata(row, entry)
    assert row.peak_pnl == 1452.
    assert row.pnl_path == [(1., -402.), (2., 1452.)]


@pytest.mark.parametrize("broken", ["ledger", "refresh", "breach"])
def test_bad_instrument_does_not_starve_other_manual_stops(broken):
    sc = Scanner.__new__(Scanner)
    sc.mode, sc.auto_mode = "REAL", False
    sc.config = {"indices": {}}
    sc._loaded_session_is_current_day = lambda: True
    sc._session_breach_lock = threading.RLock()
    sc._session_trade_candidates = {"NIFTY": {}, "BANKNIFTY": {}}
    checked = []
    def active(inst):
        if broken == "ledger" and inst == "NIFTY":
            raise ValueError("bad legacy row")
        return [SimpleNamespace(instrument=inst)]
    def refresh(rows, *_a, **_k):
        if broken == "refresh" and rows[0].instrument == "NIFTY":
            raise ValueError("bad quote")
    def breach(inst):
        if broken == "breach" and inst == "NIFTY":
            raise ValueError("bad state")
        checked.append(inst)
    sc._active_session_entry_candidates = active
    sc._refresh_trade_candidates = refresh
    sc._check_session_candidates_breaches_locked = breach
    sc._poll_manual_signal_protection()
    assert checked == ["BANKNIFTY"]
    assert any(v["instrument"] == "NIFTY" for v in sc._session_protection_faults.values())
    sc._active_session_entry_candidates = lambda inst: [SimpleNamespace(instrument=inst)]
    sc._refresh_trade_candidates = lambda *_a, **_k: None
    sc._check_session_candidates_breaches_locked = checked.append
    sc._poll_manual_signal_protection()
    assert sc._session_protection_faults == {}


def option(symbol="BANK26OCT55000PE", lane="S1", tf="5min", **changes):
    data = dict(instrument="BANKNIFTY", inst_type="OPT", trading_symbol=symbol,
                symbol_token=symbol, exchange="NFO", source_lane=lane, timeframe=tf,
                direction="SHORT", price=751.25, entry_price=751.25, stop=690.47807,
                lots=1, lot_size=30, multiplier=-.5, accepted_by_gate=True,
                signal_timestamp=datetime(2026, 10, 8, 13, 55), action="ENTRY")
    data.update(changes)
    return SimpleNamespace(**data)


def test_pe_risk_uses_full_quantity_and_actual_stop():
    assert option_exposure.stop_reservation(option(), 80.) == pytest.approx(1903.1579)


def test_same_contract_block_crosses_owner_and_timeframe():
    result = option_exposure.decide(option(tf="15min"), [option(lane="QP")],
        realized_net=0., loss_budget=45000., cost=80., forbid_same_contract=True)
    assert not result["allowed"] and "same option contract" in result["reason"]
    # Distinct expiry is a distinct contract, even if a bad input reuses its token.
    assert not option_exposure.same_contract(option(), option(symbol="BANK26NOV55000PE", symbol_token="BANK26OCT55000PE"))


def test_different_contract_stops_and_realized_losses_consume_one_budget():
    result = option_exposure.decide(option(), [option(symbol="BANK26OCT54900PE")],
        realized_net=-2000., loss_budget=5000., cost=80., forbid_same_contract=True)
    assert not result["allowed"] and "shared option loss budget" in result["reason"]
    assert result["projected_net"] == pytest.approx(-5806.3158)
    assert option_exposure.decide(option(), [], realized_net=-2000., loss_budget=5000.,
                                 cost=80., forbid_same_contract=True)["allowed"]


def test_unverifiable_option_risk_refuses_entry():
    assert not option_exposure.decide(option(stop=float("nan")), [], realized_net=0.,
                    loss_budget=5000., cost=80., forbid_same_contract=True)["allowed"]


def exposure_scanner(rows, broker=(), closed=()):
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc._session_squareoff_clock = lambda: IST.localize(NOW)
    sc._session_trade_candidates = {"BANKNIFTY": {}}
    sc._get_session_trade_candidates = lambda *_a: rows
    sc.trades = SimpleNamespace(open_trades={str(i): v for i, v in enumerate(broker)}, closed_trades=list(closed))
    return sc


def test_broker_and_manual_twins_reserve_once_and_pending_orders_still_count(monkeypatch):
    monkeypatch.setattr(get_settings(), "forbid_concurrent_same_contract_real", False)
    row = option()
    broker = option(signal_time=row.signal_timestamp, execution_status="ENTRY_PENDING", broker_quantity=0)
    sc = exposure_scanner([row], [broker])
    candidate = option(symbol="OTHER", tf="15min")
    result = sc._option_exposure_decision(candidate)
    assert result["allowed"]
    assert result["active_positions"] == 1
    assert result["existing_stop_risk"] == pytest.approx(1903.1579)


def test_manual_submit_converts_own_reservation_but_cannot_repeat_a_broker_entry():
    row = option()
    sc = exposure_scanner([row])
    assert sc._option_exposure_decision(row, execution_levels=(751.25, 690.47807, 805.9))["active_positions"] == 0
    sc.trades.open_trades["live"] = option(signal_time=row.signal_timestamp, broker_quantity=30)
    result = sc._option_exposure_decision(row, execution_levels=(751.25, 690.47807, 805.9))
    assert not result["allowed"] and "same option contract" in result["reason"]


def test_closed_option_loss_is_not_counted_twice_across_books(monkeypatch):
    monkeypatch.setattr(get_settings(), "forbid_concurrent_same_contract_real", False)
    row = option(action="EXIT", exit_timestamp=NOW - timedelta(minutes=1), pnl=-1700.)
    broker = option(signal_time=row.signal_timestamp, exit_time=row.exit_timestamp, pnl=-1800.)
    sc = exposure_scanner([row], closed=[broker])
    result = sc._option_exposure_decision(option(symbol="OTHER", tf="15min"))
    assert result["realized_net"] == -1800.
    assert result["active_positions"] == 0


def test_normal_first_bar_wait_and_engine_outage_have_distinct_evidence():
    row = option(signal_timestamp=datetime(2026, 10, 8, 13, 55))
    result = {"state": {"s1_state_bar_time": row.signal_timestamp, "s1_bull_score": 20., "s1_bear_score": 90.}}
    waiting = s1_lane.closed_bar_trend_evidence(row, result, now=datetime(2026, 10, 8, 14, 0, 30))
    stalled = s1_lane.closed_bar_trend_evidence(row, result, now=datetime(2026, 10, 8, 14, 15, 45))
    assert waiting.source == "no_closed_bar_after_signal"
    assert stalled.source == "post_signal_bar_overdue" and stalled.age_seconds == 945.
    assert not waiting.known and not stalled.known  # Diagnostic repair does not widen or tighten exits.


def test_legacy_iso_clocks_close_the_lifecycle_and_corrupt_clocks_are_reported():
    row = option()
    out = option(action="EXIT", exit_timestamp=IST.localize(NOW).isoformat())
    sc = exposure_scanner([row, out])
    assert sc._active_session_entry_candidates("BANKNIFTY") == []
    out.exit_timestamp = "corrupt"
    assert sc._session_entries_for_protection("BANKNIFTY") == []
    assert "Unparseable session exit" in sc._session_protection_faults["BANKNIFTY:ledger"]["error"]
    assert not sc._option_exposure_decision(option(symbol="OTHER", tf="15min"))["allowed"]


def test_exit_event_retains_frozen_tracking_decision_and_option_reservation():
    from tests.test_session_admission_evidence_2026_10_05 import candidate, event
    holder = candidate(action="EXIT", exit_price=112., pnl=40., peak_pnl=1452.,
                       exit_decision_record={"schema": "exit_decision.v1", "peak_pnl": 1452.},
                       option_exposure_verdict={"schema": "option_exposure.v1", "allowed": True})
    recorded = event(holder, "EXIT")
    assert recorded["exit_decision_record"]["peak_pnl"] == 1452.
    assert recorded["option_exposure"]["allowed"] is True


def test_eod_sweep_closes_healthy_instrument_when_another_ledger_is_broken():
    sc, entry, recorded = _host()
    entry.signal_timestamp = datetime(2026, 10, 8, 10, 15)
    entry.accepted_by_gate = True
    entry.peak_pnl = 1452.
    sc._session_trade_candidates = {"NIFTY": {}, "SENSEX": {}}
    sc._configured_session_time = lambda *_a: time(15, 15)
    def active(inst):
        if inst == "NIFTY":
            raise ValueError("corrupt ledger")
        return [entry]
    sc._active_session_entry_candidates = active
    assert sc._force_exit_session_candidates_at_eod(IST.localize(datetime(2026, 10, 8, 15, 30))) == 1
    assert recorded[0].instrument == "SENSEX"
    assert recorded[0].peak_pnl == 1452.
    assert recorded[0].exit_decision_record["controlling_reason"] == "FORCE_EOD_KILL"
    assert sc._session_protection_faults["NIFTY:ledger"]


def test_exposure_refusal_cannot_fall_through_to_broker_scale_in():
    import ast
    from tests.test_cascade_add_respects_manual_mode_2026_08_07 import _concurrency_block
    body = _concurrency_block().body
    wrapper = ast.parse("def run(scanner, sig, is_warmup=False):\n    for once in [0]:\n        pass\n")
    wrapper.body[0].body[0].body = body
    namespace = {}
    exec(compile(ast.fix_missing_locations(wrapper), "production_concurrency_refusal", "exec"), namespace)
    offers, rejects = [], []
    sc = SimpleNamespace(auto_mode=True,
        _offer_cascade_add=lambda sig: (offers.append(sig) or True, "added"),
        log_signal_decision_once=lambda *_a: None,
        _rtracker_record_candidate=lambda *_a, **kw: rejects.append(kw))
    sig = option(option_exposure_blocked=True, concurrency_block_reason="shared option loss budget exhausted")
    namespace["run"](sc, sig)
    assert offers == [] and rejects[0]["decision"] == "REJECTED"
    sig.option_exposure_blocked = False
    namespace["run"](sc, sig)
    assert offers == [sig]  # Ordinary permitted cascade behavior remains available.


def test_exit_with_no_samples_does_not_acquire_post_exit_samples_on_restore():
    host, entry, recorded = _host()
    entry.pnl_path, entry.intel_path = [], []
    assert host._handle_opposite_signal_exit("SENSEX",
        SimpleNamespace(signal_type="SELL", timestamp=NOW, price=72900.), "15min")
    row = recorded[0]
    entry.pnl_path.append((3., 9000.))
    entry.intel_path.append((3., {"score": 90.}))
    host._inherit_exit_entry_metadata(row, entry)
    assert row.pnl_path == [] and row.intel_path == []
