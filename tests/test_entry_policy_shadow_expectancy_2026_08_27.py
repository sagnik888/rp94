"""Executable accepted/rejected shadow outcomes and advisory expectancy tests."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import data.market_data as market_data_module
import scanner as scanner_module
from data.market_data import MarketDataProvider
from engine.entry_policy import MODEL_SOURCE, MODEL_VERSION
from intelligence.entry_policy_evaluation import evaluate_entry_shadow_walk_forward
from intelligence.rtracker import RegimeTracker
from scanner import Scanner
from trading.trade_manager import IST


def _shadow_event(day: int, decision: str, pnl: float, *, available: bool = True):
    timestamp = datetime(2026, 8, day, 10, 30, tzinfo=IST)
    lifecycle = f"L{day}-{decision}"
    return {
        "id": f"{lifecycle}-30",
        "timestamp": timestamp.isoformat(),
        "date": timestamp.date().isoformat(),
        "lifecycle_id": lifecycle,
        "instrument": "NIFTY",
        "timeframe": "5min",
        "direction": "LONG",
        "decision": "SHADOW_OUTCOME",
        "source_lane": "ordinary",
        "regime": {"smoothed_regime": "TRENDING_UP"},
        "expected_move": {
            "probability_meeting_floor": 0.7,
            "floor_pct": 0.2,
            "expected_favorable_move_pct": 0.3,
            "model_version": MODEL_VERSION,
        },
        "entry_policy": {"actual_rr": 1.5, "min_rr": 1.5},
        "shadow_outcome": {
            "decision_group": decision,
            "session_date": timestamp.date().isoformat(),
            "completed_at": timestamp.isoformat(),
            "horizon_minutes": 30,
            "outcome_available": available,
            "unavailable_reason": "" if available else "exit_quote_stale",
            "after_cost_pnl_rs": pnl if available else None,
            "gross_pnl_rs": pnl + 80 if available else None,
            "mfe_gross_rs": max(0, pnl + 100) if available else None,
            "mae_gross_rs": min(0, pnl - 50) if available else None,
            "directional_mfe_pct": 0.25 if pnl > 0 else 0.1,
            "underlying_observation_count": 1,
            "underlying_last_observed_at": timestamp.isoformat(),
            "underlying_last_source": "kite_ws",
            "entry_slippage_rs": 10.0,
            "entry_spread_cost_rs": 5.0,
            "round_trip_cost_rs": 80.0,
        },
    }


def test_walk_forward_compares_after_cost_expectancy_not_only_win_rate():
    events = []
    accepted = [100.0, -50.0, 120.0, -40.0, 80.0, 60.0]
    rejected = [-100.0, 20.0, -80.0, 10.0, -70.0, 5.0]
    for offset, (taken, passed) in enumerate(zip(accepted, rejected), start=1):
        events.append(_shadow_event(offset, "ACCEPTED", taken))
        events.append(_shadow_event(offset, "REJECTED", passed))
    events.append(_shadow_event(7, "REJECTED", 0.0, available=False))

    report = evaluate_entry_shadow_walk_forward(
        events,
        horizon_minutes=30,
        min_train_sessions=3,
        test_sessions=1,
    )

    assert report["mode"] == "ADVISORY_ONLY"
    assert report["live_settings_changed"] is False
    assert report["by_decision"]["accepted"]["after_cost_expectancy_rs"] == pytest.approx(45.0)
    assert report["by_decision"]["accepted"]["profit_factor"] == pytest.approx(4.0)
    assert report["by_decision"]["rejected"]["after_cost_expectancy_rs"] < 0
    assert report["accepted_minus_rejected_expectancy_rs"] > 0
    assert report["by_decision"]["accepted"]["max_drawdown_rs"] == pytest.approx(50.0)
    assert report["by_decision"]["accepted"]["calibration"]["samples"] == 6
    assert report["unavailable_reasons"] == {"exit_quote_stale": 1}
    assert len(report["walk_forward"]["folds"]) == 4
    assert report["by_index_timeframe_regime_lane_decision"]


def test_walk_forward_aggregates_decision_to_fill_telemetry_separately():
    accepted = _shadow_event(1, "ACCEPTED", 20.0)
    accepted["shadow_outcome"]["accepted_decision_to_fill_delta_rs"] = 6.0
    rejected = _shadow_event(2, "REJECTED", -10.0)
    # A rejected signal has no broker fill. Legacy archives may contain this field,
    # but it must not enter execution telemetry.
    rejected["shadow_outcome"]["decision_to_fill_delta_rs"] = -2.0

    report = evaluate_entry_shadow_walk_forward(
        [accepted, rejected], horizon_minutes=30, min_train_sessions=1
    )
    metrics = report["by_decision"]["accepted"]
    assert metrics["decision_to_fill_samples"] == 1
    assert metrics["accepted_decision_to_fill_samples"] == 1
    assert metrics["avg_decision_to_fill_delta_rs"] == pytest.approx(6.0)
    assert metrics["p95_accepted_decision_to_fill_delta_rs"] == pytest.approx(6.0)
    all_metrics = report["overall"]
    assert all_metrics["decision_to_fill_samples"] == 1
    assert all_metrics["avg_decision_to_fill_delta_rs"] == pytest.approx(6.0)


def test_rejected_legacy_fill_fields_never_enter_fill_slippage_metrics():
    rejected = _shadow_event(1, "REJECTED", -10.0)
    rejected["shadow_outcome"].update(
        {
            "accepted_actual_fill_slippage_rs": 999.0,
            "entry_slippage_rs": 999.0,
            "entry_slippage_basis": "executable_touch_to_actual_fill",
        }
    )

    report = evaluate_entry_shadow_walk_forward(
        [rejected], horizon_minutes=30, min_train_sessions=1
    )

    assert report["overall"]["entry_slippage_samples"] == 0
    assert report["overall"]["avg_entry_slippage_rs"] is None
    assert report["by_decision"]["rejected"][
        "accepted_actual_fill_slippage_samples"
    ] == 0


def test_valid_entry_telemetry_survives_an_unavailable_horizon_exit():
    accepted = _shadow_event(1, "ACCEPTED", 0.0, available=False)
    accepted["shadow_outcome"].update(
        {
            "accepted_actual_fill_slippage_rs": 6.0,
            "entry_slippage_rs": 6.0,
            "entry_slippage_basis": "executable_touch_to_actual_fill",
            "decision_to_executable_delta_rs": 4.0,
            "entry_spread_cost_rs": 3.0,
            "entry_quote_age_seconds": 0.2,
        }
    )

    report = evaluate_entry_shadow_walk_forward(
        [accepted], horizon_minutes=30, min_train_sessions=1
    )
    metrics = report["by_decision"]["accepted"]

    assert metrics["outcomes"] == 0
    assert metrics["entry_slippage_samples"] == 1
    assert metrics["avg_entry_slippage_rs"] == pytest.approx(6.0)
    assert metrics["decision_to_executable_samples"] == 1
    assert metrics["entry_spread_samples"] == 1
    assert metrics["entry_quote_age_samples"] == 1


def test_calibration_requires_a_real_underlying_observation():
    missing = _shadow_event(1, "REJECTED", -10.0)
    missing["shadow_outcome"].pop("underlying_observation_count")
    observed = _shadow_event(2, "REJECTED", 10.0)

    report = evaluate_entry_shadow_walk_forward(
        [missing, observed], horizon_minutes=30, min_train_sessions=1
    )

    assert report["overall"]["calibration"]["samples"] == 1


def test_weekly_rtracker_report_integrates_advisory_entry_expectancy(tmp_path):
    tracker = RegimeTracker(root_dir=tmp_path / "rtracker")
    for day in range(1, 7):
        assert tracker.record_event(_shadow_event(day, "ACCEPTED", 100.0 - day))
        assert tracker.record_event(_shadow_event(day, "REJECTED", -50.0 + day))

    result = tracker.generate_weekly_reports(
        weeks=2,
        as_of=datetime(2026, 8, 8, 16, 0, tzinfo=IST),
    )

    report_path = Path(result["entry_policy_expectancy_json"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["mode"] == "ADVISORY_ONLY"
    assert report["live_settings_changed"] is False
    assert report["horizons"]["30"]["rows"] == 12
    assert report["horizons"]["30"]["walk_forward"]["folds"]


class _Tracker:
    def __init__(self):
        self.events = []

    def record_event(self, event):
        self.events.append(event)
        return True


class _ShadowData:
    def __init__(self, spot=25_050.0):
        self.subscribed = []
        self.spot = spot

    def subscribe_tokens(self, tokens, exchange_type=2):
        self.subscribed.append((list(tokens), exchange_type))

    def get_cached_contract_touch_quote(self, exchange, symbol, token, max_age_seconds=15):
        return {
            "bid": 103.0,
            "ask": 104.0,
            "source": "ws_touch",
            "age_seconds": 0.1,
            "usable": True,
            "reason": "ok",
        }

    def get_cached_index_quote_snapshot(self, instrument):
        return {
            "spot_price": self.spot,
            "quote_age_seconds": 0.1,
            "is_fresh": True,
            "source": "kite_ws",
            "source_role": "primary",
        }


class _MutableAgeShadowData(_ShadowData):
    def __init__(self, age_seconds):
        super().__init__()
        self.age_seconds = age_seconds

    def get_cached_contract_touch_quote(self, exchange, symbol, token, max_age_seconds=15):
        quote = super().get_cached_contract_touch_quote(
            exchange, symbol, token, max_age_seconds=max_age_seconds
        )
        quote["age_seconds"] = self.age_seconds
        return quote


def _shadow_settings(tmp_path):
    return SimpleNamespace(
        entry_shadow_outcomes_enabled=True,
        entry_shadow_horizons_minutes=[15],
        entry_shadow_update_interval_seconds=5.0,
        entry_shadow_quote_max_age_seconds=10.0,
        entry_shadow_entry_quote_grace_seconds=30.0,
        entry_shadow_exit_quote_grace_seconds=180.0,
        entry_shadow_max_watches=20,
        entry_shadow_update_batch_size=20,
        entry_shadow_state_file=str(tmp_path / "pending.json"),
        opt_cost=80.0,
        fut_cost=200.0,
    )


def _shadow_candidate(**changes):
    values = dict(
        action="ENTRY",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        inst_type="OPT",
        trading_symbol="NIFTY25000CE",
        symbol_token="123",
        multiplier=0.5,
        lots=1,
        lot_size=10,
        price=99.5,
        underlying_entry=25_000.0,
        underlying_stop=24_975.0,
        underlying_target=25_050.0,
        premium_entry=100.0,
        premium_stop=96.0,
        premium_target=106.0,
        geometry_observed_at=datetime.now(IST).isoformat(),
        geometry_source="signal_processor.dual_geometry",
        geometry_verified=True,
        geometry_schema="dual_execution_geometry.v1",
        execution_bid_price=99.0,
        execution_ask_price=100.0,
        execution_touch_usable=True,
        execution_touch_source="kite_rest_touch",
        execution_touch_age_seconds=0.1,
        expected_move_forecast={
            "probability_meeting_floor": 0.7,
            "floor_pct": 0.2,
            "expected_favorable_move_pct": 0.3,
            "model_version": MODEL_VERSION,
            "source": MODEL_SOURCE,
        },
        entry_policy_verdict={"allowed": True, "actual_rr": 1.5, "min_rr": 1.5},
        market_regime="TRENDING_UP",
        source_lane="ordinary",
        is_recovery=False,
        is_ghost=False,
        signal_timestamp=datetime.now(IST) - timedelta(minutes=1),
        actual_fill_price=0.0,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def _make_shadow_scanner(monkeypatch, tmp_path, lifecycle_id):
    settings = _shadow_settings(tmp_path)
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.is_warmup = False
    scanner.data = _ShadowData()
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 25_050.0)
    scanner.rtracker = _Tracker()
    scanner._entry_shadow_book = {}
    scanner._entry_shadow_pending_fills = {}
    scanner._entry_shadow_lock = threading.RLock()
    scanner._entry_shadow_cursor = 0
    scanner._entry_shadow_last_persist = 0.0
    scanner._entry_shadow_persist_requested = False
    scanner._candidate_lifecycle_key = lambda candidate: str(
        getattr(candidate, "lifecycle_id", "") or lifecycle_id
    )
    return scanner


def test_accepted_registration_never_labels_a_requested_price_as_a_fill(
    monkeypatch, tmp_path
):
    scanner = _make_shadow_scanner(monkeypatch, tmp_path, "L-ack-only")

    scanner._register_entry_shadow_outcome(
        _shadow_candidate(actual_fill_price=123.45), "ACCEPTED"
    )

    watch = scanner._entry_shadow_book["L-ack-only|ACCEPTED"]
    assert watch["accepted_actual_fill_price"] == 0.0
    assert watch["accepted_actual_fill_quantity"] == 0
    assert watch["accepted_actual_fill_at"] == ""


def test_broker_fill_arriving_before_watch_registration_is_merged(
    monkeypatch, tmp_path
):
    scanner = _make_shadow_scanner(monkeypatch, tmp_path, "L-fill-first")
    trade = SimpleNamespace(lifecycle_id="L-fill-first")

    scanner._update_accepted_entry_shadow_fill(trade, 101.0, 4)

    assert scanner._entry_shadow_pending_fills[
        "L-fill-first|ACCEPTED"
    ]["accepted_actual_fill_price"] == pytest.approx(101.0)
    scanner._register_entry_shadow_outcome(_shadow_candidate(), "ACCEPTED")
    watch = scanner._entry_shadow_book["L-fill-first|ACCEPTED"]
    assert watch["accepted_actual_fill_price"] == pytest.approx(101.0)
    assert watch["accepted_actual_fill_quantity"] == 4
    assert watch["accepted_actual_fill_at"]
    assert scanner._entry_shadow_pending_fills == {}


def test_fill_arriving_after_worker_snapshot_is_included_in_due_outcome(
    monkeypatch, tmp_path
):
    scanner = _make_shadow_scanner(monkeypatch, tmp_path, "L-worker-race")
    scanner._register_entry_shadow_outcome(_shadow_candidate(), "ACCEPTED")
    watch = scanner._entry_shadow_book["L-worker-race|ACCEPTED"]
    watch["registered_at"] = (
        datetime.now(IST) - timedelta(minutes=15, seconds=1)
    ).isoformat()

    def quote_after_fill(_watch_copy):
        scanner._update_accepted_entry_shadow_fill(
            SimpleNamespace(lifecycle_id="L-worker-race"), 101.0, 4
        )
        return {
            "bid": 103.0,
            "ask": 104.0,
            "source": "ws_touch",
            "age_seconds": 0.1,
            "usable": True,
            "reason": "ok",
        }

    scanner._entry_shadow_cached_quote = quote_after_fill

    assert scanner._update_entry_shadow_outcomes() == 1
    outcome = scanner.rtracker.events[0]["shadow_outcome"]
    assert outcome["accepted_actual_fill_price"] == pytest.approx(101.0)
    assert outcome["accepted_actual_fill_quantity"] == 4
    assert outcome["accepted_actual_fill_slippage_rs"] == pytest.approx(4.0)


def test_fill_arriving_after_worker_snapshot_is_not_overwritten_before_horizon(
    monkeypatch, tmp_path
):
    scanner = _make_shadow_scanner(monkeypatch, tmp_path, "L-worker-keep")
    scanner._register_entry_shadow_outcome(_shadow_candidate(), "ACCEPTED")

    def quote_after_fill(_watch_copy):
        scanner._update_accepted_entry_shadow_fill(
            SimpleNamespace(lifecycle_id="L-worker-keep"), 101.0, 4
        )
        return {
            "bid": 103.0,
            "ask": 104.0,
            "source": "ws_touch",
            "age_seconds": 0.1,
            "usable": True,
            "reason": "ok",
        }

    scanner._entry_shadow_cached_quote = quote_after_fill

    assert scanner._update_entry_shadow_outcomes() == 0
    watch = scanner._entry_shadow_book["L-worker-keep|ACCEPTED"]
    assert watch["accepted_actual_fill_price"] == pytest.approx(101.0)
    assert watch["accepted_actual_fill_quantity"] == 4


def test_option_cross_geometry_is_recovered_or_marked_unavailable(
    monkeypatch, tmp_path
):
    scanner = _make_shadow_scanner(monkeypatch, tmp_path, "unused")
    recovered = _shadow_candidate(
        lifecycle_id="L-geometry-recovered",
        price=25_000.0,
        underlying_entry=25_000.0,
        premium_entry=100.0,
    )
    unavailable = _shadow_candidate(
        lifecycle_id="L-geometry-unavailable",
        price=25_000.0,
        underlying_entry=25_000.0,
        premium_entry=0.0,
    )

    scanner._register_entry_shadow_outcome(recovered, "REJECTED")
    scanner._register_entry_shadow_outcome(unavailable, "REJECTED")

    recovered_watch = scanner._entry_shadow_book[
        "L-geometry-recovered|REJECTED"
    ]
    unavailable_watch = scanner._entry_shadow_book[
        "L-geometry-unavailable|REJECTED"
    ]
    assert recovered_watch["decision_price"] == pytest.approx(100.0)
    assert recovered_watch["decision_price_basis"] == "premium_geometry_recovered"
    assert unavailable_watch["decision_price"] == 0.0
    assert unavailable_watch["decision_price_basis"] == "cross_geometry_unavailable"


def test_effective_underlying_fallback_is_persisted_in_geometry(monkeypatch, tmp_path):
    scanner = _make_shadow_scanner(monkeypatch, tmp_path, "L-geometry-fallback")

    scanner._register_entry_shadow_outcome(
        _shadow_candidate(underlying_entry=0.0), "REJECTED"
    )

    watch = scanner._entry_shadow_book["L-geometry-fallback|REJECTED"]
    assert watch["underlying_entry"] == pytest.approx(25_050.0)
    assert watch["geometry"]["underlying_entry"] == pytest.approx(25_050.0)
    assert (
        watch["geometry"]["underlying_entry_basis"]
        == "fresh_index_quote_fallback"
    )


def test_scanner_records_executable_bid_ask_shadow_for_an_accepted_candidate(
    monkeypatch, tmp_path
):
    settings = _shadow_settings(tmp_path)
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.is_warmup = False
    scanner.data = _ShadowData()
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 25_050.0)
    scanner.rtracker = _Tracker()
    scanner._entry_shadow_book = {}
    scanner._entry_shadow_lock = threading.RLock()
    scanner._entry_shadow_cursor = 0
    scanner._entry_shadow_last_persist = 0.0
    scanner._candidate_lifecycle_key = lambda candidate: "L-accepted"

    candidate = _shadow_candidate()
    scanner._register_entry_shadow_outcome(candidate, "ACCEPTED")
    scanner._update_accepted_entry_shadow_fill(
        SimpleNamespace(lifecycle_id="L-accepted"),
        101.0,
        4,
    )
    watch = scanner._entry_shadow_book["L-accepted|ACCEPTED"]
    watch["registered_at"] = (datetime.now(IST) - timedelta(minutes=16)).isoformat()
    scanner._entry_shadow_book[watch["key"]] = watch

    completed = scanner._update_entry_shadow_outcomes()

    assert completed == 1
    assert scanner._entry_shadow_book == {}
    assert scanner.data.subscribed == [(["123"], 2)]
    event = scanner.rtracker.events[0]
    assert event["decision"] == "SHADOW_OUTCOME"
    assert event["shadow_outcome"]["decision_group"] == "ACCEPTED"
    assert event["shadow_outcome"]["entry_executable_price"] == 100.0
    assert event["shadow_outcome"]["exit_executable_price"] == 103.0
    assert event["shadow_outcome"]["gross_pnl_rs"] == pytest.approx(30.0)
    assert event["shadow_outcome"]["after_cost_pnl_rs"] == pytest.approx(-50.0)
    assert event["shadow_outcome"]["directional_mfe_pct"] == pytest.approx(0.2)
    assert event["shadow_outcome"]["accepted_actual_fill_price"] == pytest.approx(101.0)
    assert event["shadow_outcome"]["accepted_actual_fill_quantity"] == 4
    # Actual fill slippage is measured from the executable touch (100 -> 101),
    # while the decision-to-fill movement remains available as a separate metric.
    assert event["shadow_outcome"]["accepted_actual_fill_slippage_rs"] == pytest.approx(4.0)
    assert event["shadow_outcome"]["entry_slippage_rs"] == pytest.approx(4.0)
    assert event["shadow_outcome"]["entry_slippage_basis"] == "executable_touch_to_actual_fill"
    assert event["shadow_outcome"]["decision_to_executable_delta_rs"] == pytest.approx(5.0)
    assert event["shadow_outcome"]["accepted_decision_to_fill_delta_rs"] == pytest.approx(6.0)
    assert event["shadow_outcome"]["decision_to_fill_delta_rs"] == pytest.approx(6.0)
    assert event["shadow_outcome"]["exit_quote_age_seconds"] == pytest.approx(0.1)
    assert event["geometry"]["geometry_conversion_ratio"] == pytest.approx(0.5)


def test_shadow_entry_quote_arriving_after_grace_is_not_backfilled(monkeypatch, tmp_path):
    settings = _shadow_settings(tmp_path)
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.is_warmup = False
    scanner.data = _ShadowData()
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 25_000.0)
    scanner.rtracker = _Tracker()
    scanner._entry_shadow_book = {}
    scanner._entry_shadow_lock = threading.RLock()
    scanner._entry_shadow_cursor = 0
    scanner._entry_shadow_last_persist = 0.0
    scanner._candidate_lifecycle_key = lambda candidate: "L-late-entry"

    scanner._register_entry_shadow_outcome(
        _shadow_candidate(
            execution_bid_price=0.0,
            execution_ask_price=0.0,
            execution_touch_usable=False,
            actual_fill_price=0.0,
        ),
        "REJECTED",
    )
    watch = scanner._entry_shadow_book["L-late-entry|REJECTED"]
    watch["registered_at"] = (datetime.now(IST) - timedelta(seconds=31)).isoformat()

    assert scanner._update_entry_shadow_outcomes() == 1
    assert scanner._entry_shadow_book == {}
    outcome = scanner.rtracker.events[0]["shadow_outcome"]
    assert outcome["outcome_available"] is False
    assert outcome["unavailable_reason"] == "entry_quote_deadline_missed"
    assert outcome["entry_executable_price"] == 0.0


def test_shadow_entry_backfill_waits_for_a_quote_observed_after_registration(
    monkeypatch, tmp_path
):
    settings = _shadow_settings(tmp_path)
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.is_warmup = False
    scanner.data = _MutableAgeShadowData(age_seconds=10.0)
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 25_000.0)
    scanner.rtracker = _Tracker()
    scanner._entry_shadow_book = {}
    scanner._entry_shadow_lock = threading.RLock()
    scanner._entry_shadow_cursor = 0
    scanner._entry_shadow_last_persist = 0.0
    scanner._candidate_lifecycle_key = lambda candidate: "L-predecision-entry"

    scanner._register_entry_shadow_outcome(
        _shadow_candidate(
            execution_bid_price=0.0,
            execution_ask_price=0.0,
            execution_touch_usable=False,
            actual_fill_price=0.0,
        ),
        "REJECTED",
    )
    watch = scanner._entry_shadow_book["L-predecision-entry|REJECTED"]
    watch["registered_at"] = (datetime.now(IST) - timedelta(seconds=5)).isoformat()

    assert scanner._update_entry_shadow_outcomes() == 0
    watch = scanner._entry_shadow_book["L-predecision-entry|REJECTED"]
    assert watch["entry_executable_price"] == 0.0

    scanner.data.age_seconds = 0.1
    assert scanner._update_entry_shadow_outcomes() == 0
    watch = scanner._entry_shadow_book["L-predecision-entry|REJECTED"]
    assert watch["entry_executable_price"] == 104.0
    assert scanner._entry_shadow_time(watch["entry_quote_captured_at"]) >= (
        scanner._entry_shadow_time(watch["registered_at"])
    )


def test_shadow_exit_quote_after_horizon_deadline_is_unavailable(monkeypatch, tmp_path):
    settings = _shadow_settings(tmp_path)
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.is_warmup = False
    scanner.data = _ShadowData()
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 25_050.0)
    scanner.rtracker = _Tracker()
    scanner._entry_shadow_book = {}
    scanner._entry_shadow_lock = threading.RLock()
    scanner._entry_shadow_cursor = 0
    scanner._entry_shadow_last_persist = 0.0
    scanner._candidate_lifecycle_key = lambda candidate: "L-late-exit"

    scanner._register_entry_shadow_outcome(_shadow_candidate(), "ACCEPTED")
    watch = scanner._entry_shadow_book["L-late-exit|ACCEPTED"]
    watch["registered_at"] = (
        datetime.now(IST) - timedelta(minutes=18, seconds=1)
    ).isoformat()

    assert scanner._update_entry_shadow_outcomes() == 1
    outcome = scanner.rtracker.events[0]["shadow_outcome"]
    assert outcome["outcome_available"] is False
    assert outcome["unavailable_reason"] == "exit_quote_deadline_missed"
    assert outcome["exit_executable_price"] == 0.0


def test_shadow_does_not_use_a_cached_quote_observed_before_the_horizon(
    monkeypatch, tmp_path
):
    settings = _shadow_settings(tmp_path)
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.is_warmup = False
    scanner.data = _MutableAgeShadowData(age_seconds=5.0)
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 25_050.0)
    scanner.rtracker = _Tracker()
    scanner._entry_shadow_book = {}
    scanner._entry_shadow_lock = threading.RLock()
    scanner._entry_shadow_cursor = 0
    scanner._entry_shadow_last_persist = 0.0
    scanner._candidate_lifecycle_key = lambda candidate: "L-horizon-ordering"

    scanner._register_entry_shadow_outcome(_shadow_candidate(), "ACCEPTED")
    watch = scanner._entry_shadow_book["L-horizon-ordering|ACCEPTED"]
    watch["registered_at"] = (
        datetime.now(IST) - timedelta(minutes=15, seconds=1)
    ).isoformat()

    assert scanner._update_entry_shadow_outcomes() == 0
    assert scanner.rtracker.events == []
    assert "L-horizon-ordering|ACCEPTED" in scanner._entry_shadow_book

    scanner.data.age_seconds = 0.1
    assert scanner._update_entry_shadow_outcomes() == 1
    outcome = scanner.rtracker.events[0]["shadow_outcome"]
    assert outcome["outcome_available"] is True
    assert outcome["exit_quote_delay_seconds"] >= 0.0


@pytest.mark.parametrize(("now", "expected_age"), [(103.0, 7.0), (99.0, 4.0)])
def test_cached_touch_preserves_source_age_and_never_gets_younger(
    monkeypatch, now, expected_age
):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._touch_quote_cache = {
        "NFO|NIFTY25000CE|123": {
            "time": 100.0,
            "quote": {
                "bid": 99.0,
                "ask": 100.0,
                "age_seconds": 4.0,
                "source": "ws_touch",
                "usable": True,
                "reason": "ok",
            },
        }
    }
    provider._ws_cache = {}
    monkeypatch.setattr(market_data_module.time, "time", lambda: now)

    quote = provider.get_cached_contract_touch_quote(
        "NFO", "NIFTY25000CE", "123", max_age_seconds=10.0
    )

    assert quote["usable"] is True
    assert quote["age_seconds"] == pytest.approx(expected_age)


def test_real_execution_touch_cache_also_adds_source_age_to_cache_residence(monkeypatch):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._touch_quote_cache = {
        "NFO|NIFTY25000CE|123": {
            "time": 100.0,
            "quote": {
                "bid": 99.0,
                "ask": 100.0,
                "age_seconds": 1.4,
                "source": "ws_touch",
                "usable": True,
                "reason": "ok",
            },
        }
    }
    provider._ws_cache = {}
    provider.kite = None
    provider.fyers = None
    monkeypatch.setattr(market_data_module.time, "time", lambda: 100.2)

    quote = provider.get_contract_touch_quote(
        "NFO", "NIFTY25000CE", "123", max_age_seconds=1.5
    )

    assert quote["usable"] is False
    assert quote["reason"] == "unavailable"


@pytest.mark.parametrize("bad_age", [float("nan"), float("inf"), -1.0])
def test_nonfinite_or_negative_cached_touch_age_never_becomes_fresh(monkeypatch, bad_age):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._touch_quote_cache = {
        "NFO|NIFTY25000CE|123": {
            "time": 100.0,
            "quote": {
                "bid": 99.0,
                "ask": 100.0,
                "age_seconds": bad_age,
                "source": "ws_touch",
                "usable": True,
                "reason": "ok",
            },
        }
    }
    provider._ws_cache = {}
    provider.kite = None
    provider.fyers = None
    monkeypatch.setattr(market_data_module.time, "time", lambda: 100.1)

    quote = provider.get_contract_touch_quote(
        "NFO", "NIFTY25000CE", "123", max_age_seconds=1.5
    )

    assert quote["usable"] is False


def test_future_dated_touch_clock_is_not_fresh_execution_evidence():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    quote = provider._touch_quote_from_payload(
        {
            "bid": 100.0,
            "ask": 101.0,
            "time": 130.0,
        },
        "kite_ws",
        100.0,
        1.5,
    )

    assert quote["usable"] is False
    assert quote["reason"] == "stale"
    assert quote["age_seconds"] == float("inf")


def test_real_mode_cannot_disable_the_final_entry_policy(monkeypatch):
    now = datetime(2026, 8, 27, 10, 0)
    settings = SimpleNamespace(
        entry_policy_enabled=False,
        entry_policy_min_rr=1.5,
        entry_policy_expected_move_floor_pct_by_index={"NIFTY": 0.2},
        entry_policy_underlying_stop_max_pct_by_index={"NIFTY": 0.15},
        entry_policy_forecast_max_age_seconds=90.0,
        entry_policy_geometry_max_age_seconds=10.0,
    )
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._entry_policy_clock = lambda holder, phase: now
    from engine.entry_policy import ExecutionGeometry, ExpectedMoveForecast

    scanner._entry_execution_geometry = lambda *args, **kwargs: ExecutionGeometry(
        instrument="NIFTY",
        direction="LONG",
        inst_type="OPT",
        underlying_entry=25_000.0,
        underlying_stop=24_975.0,
        underlying_target=25_050.0,
        premium_entry=100.0,
        premium_stop=96.0,
        premium_target=106.0,
        contract_entry=100.0,
        contract_stop=96.0,
        contract_target=106.0,
        observed_at=now.isoformat(),
        source="signal_processor.dual_geometry|broker_queue",
        verified=True,
        conversion_ratio=0.5,
    )
    scanner._expected_move_forecast_for_entry = lambda *args, **kwargs: ExpectedMoveForecast(
        instrument="NIFTY",
        direction="LONG",
        timeframe="5min",
        horizon_minutes=30,
        horizon_bars=6,
        expected_favorable_move_pct=0.25,
        q10_pct=0.1,
        q25_pct=0.25,
        q50_pct=0.4,
        uncertainty_pct=0.3,
        probability_meeting_floor=0.7,
        floor_pct=0.2,
        sample_count=100,
        generated_at=now.isoformat(),
        data_as_of=(now - timedelta(seconds=30)).isoformat(),
        model_version=MODEL_VERSION,
        source=MODEL_SOURCE,
        available=True,
        reason="",
    )
    holder = SimpleNamespace(source_lane="ordinary", market_regime="TRENDING_UP")

    verdict = scanner._final_entry_policy_verdict(
        holder,
        execution_levels=(100.0, 96.0, 106.0),
        phase="broker_queue",
    )

    assert verdict.allowed is True
    assert dict(verdict.checks)["execution_rr"] is True
    assert verdict.reason != "entry policy disabled"
