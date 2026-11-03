"""Restart/exit evidence and accounting regression for native session rows."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config.settings import Settings, get_settings
from engine import s1_lane
from engine.signal_processor import TradeCandidate
from scanner import Scanner

GROSS = "GROSS_BEFORE_ROUND_TRIP_COST"
NET = "NET_AFTER_ROUND_TRIP_COST"


def candidate(**changes):
    values = dict(instrument="NIFTY", direction="LONG", price=150.0, stop=135.0,
                  target=210.0, lots=1, lot_size=65, grade="C", confidence=0.45,
                  timeframe="5min", inst_type="OPT", option_type="CE", atm_strike=1000,
                  multiplier=0.5, trading_symbol="NIFTYTESTCE", symbol_token="1",
                  rr=4.0, score=0.0, reasons=[], signal_timestamp=datetime(2026, 10, 5, 10),
                  current_price=150.0, entry_spot=1000.0, underlying_entry=1000.0,
                  underlying_stop=990.0, underlying_target=1040.0,
                  source_engine="s1", source_lane="S1", quote_verified=True)
    values.update(changes)
    row = TradeCandidate(**values)
    row.s1_score = 98.0
    row.s1_setup = "Confluence"
    row.s1_guard_pct = 15.0
    row.s1_targets = [1020.0, 1030.0, 1040.0]
    row.s1_chart_targets = [1020.0, 1030.0, 1040.0]
    row.s1_sl_pct = 1.0
    row.s1_sl1 = 990.0
    row.entry_timestamp = datetime(2026, 10, 5, 10, 5, 2)
    row.initial_stop = 135.0
    row.accepted_decision_record = {"status": "ACCEPTED", "policy_owner": "S1_LANE",
                                    "checks": {"score": 98.0}}
    return row


def codec():
    sc = Scanner.__new__(Scanner)
    sc._candidate_history_key = lambda row: "test"
    sc._candidate_lifecycle_key = lambda row: "test-lifecycle"
    sc._manual_signal_exit_state = lambda row: {}
    return sc


def restarted(sc, row):
    payload = json.loads(json.dumps(sc._serialize_trade_candidate(row), allow_nan=False))
    restored = sc._candidate_from_payload(payload)
    assert restored is not None
    return restored, payload


@pytest.mark.parametrize("direction,best,spot,stop", [
    ("LONG", 1025.0, 1006.0, 990.0),
    ("SHORT", 975.0, 994.0, 1010.0),
])
def test_restart_preserves_t1_protection_and_same_exit(direction, best, spot, stop):
    row = candidate(direction=direction, underlying_stop=stop)
    row.s1_best_spot, row.s1_t1_locked = best, True
    restored, payload = restarted(codec(), row)
    original = s1_lane.exit_decision(row, spot, 150.0, settings=Settings())
    after = s1_lane.exit_decision(restored, spot, 150.0, settings=Settings())
    assert original.reason == "S1_T1_LOCK"
    assert after == original
    assert payload["s1_best_spot"] == best
    assert payload["s1_t1_locked"] is True


def test_restart_keeps_seed_strength_guard_geometry_and_admission():
    row = candidate()
    row.missed_reentry_recorded_at = datetime(2026, 10, 5, 10, 7)
    row.missed_reentry_seed_reason = "provider quote missing"
    row.source_signal_price = 1000.0
    restored, _ = restarted(codec(), row)
    state = {"s1_bull_score": 80.0, "s1_bear_score": 20.0,
             "s1_state_bar_time": datetime(2026, 10, 5, 10, 10)}
    assert s1_lane.reentry_strength(row, state, Settings())[0] is False
    assert s1_lane.reentry_strength(restored, state, Settings())[0] is False
    for name in ("s1_score", "s1_setup", "s1_guard_pct", "s1_targets", "s1_chart_targets",
                 "s1_sl_pct", "s1_sl1", "source_signal_price", "missed_reentry_seed_reason",
                 "missed_reentry_recorded_at", "accepted_decision_record"):
        assert getattr(restored, name) == getattr(row, name)
    restored.s1_targets.append(1050.0)
    restored.accepted_decision_record["checks"]["score"] = 20
    assert row.s1_targets == [1020.0, 1030.0, 1040.0]
    assert row.accepted_decision_record["checks"]["score"] == 98.0


def test_exit_carries_native_protection_and_exact_accounting_units():
    sc, row, exit_row = codec(), candidate(), candidate()
    row.s1_best_spot, row.s1_t1_locked, row.s1_peak_premium = 1025.0, True, 180.0
    row.peak_pnl_basis, row.max_drawdown_basis = NET, NET
    row.pnl_basis, row.pnl_path_basis = NET, GROSS
    row.peak_pnl_gross, row.max_drawdown_gross = 1950.0, -65.0
    row.peak_pnl_net, row.max_drawdown_net, row.round_trip_cost = 1870.0, -145.0, 80.0
    sc._inherit_exit_entry_metadata(exit_row, row)
    for key in ("s1_best_spot", "s1_t1_locked", "s1_peak_premium", "s1_score",
                "accepted_decision_record", "peak_pnl_basis", "max_drawdown_basis",
                "pnl_basis", "pnl_path_basis", "peak_pnl_gross", "max_drawdown_gross",
                "peak_pnl_net", "max_drawdown_net", "round_trip_cost"):
        assert getattr(exit_row, key) == getattr(row, key)
    exit_row.accepted_decision_record["checks"]["score"] = 10
    assert row.accepted_decision_record["checks"]["score"] == 98.0
    restored, _ = restarted(sc, exit_row)
    assert restored.peak_pnl_gross == 1950.0
    assert restored.pnl_path_basis == GROSS


def monitor(row, spot, exits, persists):
    sc = codec()
    sc.mode, sc.auto_mode = "REAL", False
    sc._loaded_session_is_current_day = lambda: True
    sc._active_session_entry_candidates = lambda _: [row]
    sc.candles = SimpleNamespace(get_latest_price=lambda _: spot)
    sc._get_trade_regime = lambda *a, **k: "RANGING"
    sc._entry_intel_snapshot = lambda _: {"regime": "RANGING", "score": 50}
    sc.mtf = SimpleNamespace(_result_cache={})
    sc._diag_inc = lambda *a: None
    sc.log_event = lambda *a, **k: None
    sc._notify = lambda *a, **k: None
    sc._maybe_arm_reversion_from_exit = lambda *a, **k: None
    sc._remember_trade_candidates = lambda rows: exits.extend(rows)
    sc._latest_exit_candidates = {}
    sc._persist_session_trade_candidates = lambda: persists.append(True)
    return sc


@pytest.mark.parametrize("engine,lane", [("s1", "S1"), ("ut_smart", "UTS")])
def test_native_path_samples_gross_and_cost_is_charged_once(monkeypatch, engine, lane):
    settings = get_settings()
    monkeypatch.setattr(settings, "opt_cost", 80.0)
    monkeypatch.setattr(settings, "pnl_path_logging_enabled", True)
    monkeypatch.setattr(settings, "intel_path_logging_enabled", True, raising=False)
    row, exits, persists = candidate(source_engine=engine, source_lane=lane), [], []
    row.uts_confirmed = True
    row.entry_timestamp = datetime.now() - timedelta(minutes=2)
    row.current_price = 160.0
    sc = monitor(row, 1005.0, exits, persists)
    sc._check_session_candidates_breaches_locked("NIFTY")
    assert exits == []
    assert row.pnl == 570.0
    assert row.peak_pnl == 570.0
    assert row.peak_pnl_basis == NET and row.max_drawdown_basis == NET
    assert row.pnl_path_basis == GROSS and row.pnl_basis == NET
    assert row.pnl_path[-1][1] == 650.0
    assert row.peak_pnl_gross == 650.0 and row.max_drawdown_gross == 0.0
    assert row.round_trip_cost == 80.0
    assert row.intel_path
    row.current_price = 149.0
    sc._check_session_candidates_breaches_locked("NIFTY")
    assert row.pnl == -145.0
    assert row.max_drawdown == -145.0
    assert row.max_drawdown_gross == -65.0
    assert row.pnl_path[-1][1] == -65.0
    assert row.round_trip_cost == 80.0


def test_bookkeeping_only_update_is_persisted():
    row, exits, persists = candidate(), [], []
    row.stop = 149.0
    sc = monitor(row, 1004.0, exits, persists)
    row.s1_peak_premium = 150.0
    row.peak_pnl, row.max_drawdown = 0.0, -80.0
    row.pnl, row.round_trip_cost = -80.0, 80.0
    row.pnl_basis, row.pnl_path_basis = NET, GROSS
    row.peak_pnl_basis, row.max_drawdown_basis = NET, NET
    row.peak_pnl_net, row.max_drawdown_net = 0.0, -80.0
    row.peak_pnl_gross, row.max_drawdown_gross = 0.0, 0.0
    row.entry_timestamp = None
    # Isolate native bookkeeping from the independently tested continuity timer.
    sc._observe_manual_signal_continuity = lambda *a: False
    sc._check_session_candidates_breaches_locked("NIFTY")
    assert row.s1_best_spot == 1004.0
    assert persists


def test_legacy_rows_do_not_invent_native_protection_or_admission():
    sc = codec()
    payload = sc._serialize_trade_candidate(candidate())
    for key in list(payload):
        if key.startswith("s1_") or key == "accepted_decision_record":
            payload.pop(key)
    restored = sc._candidate_from_payload(payload)
    assert restored is not None
    assert getattr(restored, "s1_best_spot", None) is None
    assert getattr(restored, "s1_t1_locked", False) is False
    assert getattr(restored, "accepted_decision_record", None) is None


@pytest.mark.parametrize("field,value", [
    ("s1_best_spot", "bad"), ("s1_best_spot", 0.0), ("s1_score", float("nan")),
    ("s1_peak_premium", "bad"), ("s1_peak_premium", float("inf")),
    ("s1_t1_locked", "false"), ("s1_targets", [1020.0, "bad"]),
    ("missed_reentry_recorded_at", "bad"),
])
def test_corrupt_native_diagnostics_do_not_drop_restored_position(field, value):
    sc = codec()
    payload = sc._serialize_trade_candidate(candidate())
    payload[field] = value
    restored = sc._candidate_from_payload(payload)
    assert restored is not None
    assert not hasattr(restored, field)


def test_native_exit_carries_last_gross_path_sample_and_canonical_net_outcome(monkeypatch):
    monkeypatch.setattr(get_settings(), "opt_cost", 80.0)
    monkeypatch.setattr(get_settings(), "pnl_path_logging_enabled", True)
    # Mark-priced outcome; executable (ask/bid) pricing has its own 2026-10-08 test.
    monkeypatch.setattr(get_settings(), "tracker_executable_pricing_enabled", False, raising=False)
    row, exits, persists = candidate(), [], []
    row.entry_timestamp = datetime.now() - timedelta(minutes=2)
    row.s1_best_spot, row.s1_t1_locked = 1025.0, True
    row.current_price = 148.0
    sc = monitor(row, 1006.0, exits, persists)
    sc._check_session_candidates_breaches_locked("NIFTY")
    assert len(exits) == 1
    exit_row = exits[0]
    assert exit_row.exit_reason == "S1_T1_LOCK"
    assert exit_row.pnl == -210.0
    assert exit_row.pnl_basis == NET and exit_row.pnl_path_basis == GROSS
    assert exit_row.pnl_path[-1][1] == -130.0
    assert exit_row.round_trip_cost == 80.0
    assert exit_row.accepted_decision_record["status"] == "ACCEPTED"
    row.pnl_path.append((200, 10000))
    assert exit_row.pnl_path[-1][1] == -130.0
