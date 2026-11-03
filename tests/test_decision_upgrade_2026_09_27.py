from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import time

import pytest

from engine.decision_record import (
    DecisionStatus,
    build_decision_record,
    ensure_decision_record,
    update_decision_record,
)
from engine.call_guard import CallCircuitBreaker, CircuitOpenError
from engine.candidate_selection import rank_entry_candidates
from engine.candidate_pipeline import candidate_size_multipliers, select_contract_strikes
from engine.runtime_mode import RuntimeMode
from engine.signal_processor import _intelligence_stop_pct
from engine.signal_quality_policy import dynamic_rr
from scanner import Scanner
from trading.trade_manager import Trade


def test_dynamic_rr_is_symmetric_for_long_and_short_confirmation(monkeypatch):
    from config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "dynamic_rr_min", 1.5)
    monkeypatch.setattr(settings, "dynamic_rr_max", 12.0)

    long_confirmed = dynamic_rr("B", 0.70, 0.80, direction="LONG")
    short_confirmed = dynamic_rr("B", 0.70, -0.80, direction="SHORT")
    long_opposed = dynamic_rr("B", 0.70, -0.80, direction="LONG")
    short_opposed = dynamic_rr("B", 0.70, 0.80, direction="SHORT")

    assert long_confirmed == short_confirmed
    assert long_opposed == short_opposed
    assert long_confirmed > long_opposed


def test_canonical_intelligence_stop_is_symmetric_by_direction():
    settings = SimpleNamespace(
        dynamic_stop_from_intelligence=True,
        dynamic_stop_intel_max_shift_pct=20.0,
    )
    long_confirmed = _intelligence_stop_pct(
        15.0, 0.8, settings, "LONG", "canonical_signed"
    )
    short_confirmed = _intelligence_stop_pct(
        15.0, -0.8, settings, "SHORT", "canonical_signed"
    )
    long_opposed = _intelligence_stop_pct(
        15.0, -0.8, settings, "LONG", "canonical_signed"
    )
    short_opposed = _intelligence_stop_pct(
        15.0, 0.8, settings, "SHORT", "canonical_signed"
    )

    assert long_confirmed == pytest.approx(short_confirmed)
    assert long_opposed == pytest.approx(short_opposed)
    assert long_confirmed > 15.0 > long_opposed


def _candidate(direction, confidence, grade="B", action="ENTRY", lane=""):
    return SimpleNamespace(
        instrument=f"{direction}-{confidence}",
        timeframe="5min",
        direction=direction,
        confidence=confidence,
        grade=grade,
        action=action,
        signal_timestamp=datetime.now() - timedelta(seconds=5),
        source_lane=lane,
        source_engine="ut_smart" if lane == "UTS" else "quad_prime",
        quality_verdict={"allowed": False},
        decision_record={},
    )


def test_global_execution_priority_has_no_long_first_bias():
    scanner = Scanner.__new__(Scanner)
    weak_long = _candidate("LONG", 0.55)
    strong_short = _candidate("SHORT", 0.90, grade="A")

    ranked = sorted(
        [weak_long, strong_short],
        key=scanner._candidate_execution_priority,
        reverse=True,
    )

    assert ranked == [strong_short, weak_long]


def test_exit_priority_precedes_higher_confidence_entry():
    scanner = Scanner.__new__(Scanner)
    entry = _candidate("LONG", 0.99, grade="A+")
    exit_candidate = _candidate("SHORT", 0.10, grade="C", action="EXIT")

    ranked = sorted(
        [entry, exit_candidate],
        key=scanner._candidate_execution_priority,
        reverse=True,
    )

    assert ranked[0] is exit_candidate


def test_lane_decision_names_policy_owner_and_quality_not_applicable():
    candidate = _candidate("LONG", 0.70, lane="UTS")
    now = datetime.now()
    record = build_decision_record(candidate, rank=1, decided_at=now)

    assert record["policy_owner"] == "UT_SMART_LANE"
    assert record["quality_policy_status"] == DecisionStatus.NOT_APPLICABLE.value
    assert record["rank"] == 1
    assert record["data_age_seconds"] >= 5.0

    accepted = update_decision_record(
        candidate,
        status="ACCEPTED",
        reason="Lane policy admitted candidate",
        source="session_candidate",
        decided_at=now,
    )
    assert accepted["status"] == DecisionStatus.ALLOW.value
    assert accepted["reason"] == "Lane policy admitted candidate"


def test_decision_id_is_stable_for_same_candidate():
    candidate = _candidate("SHORT", 0.70)
    now = datetime.now()
    first = build_decision_record(candidate, rank=1, decided_at=now)
    second = build_decision_record(candidate, rank=2, decided_at=now + timedelta(seconds=1))

    assert first["decision_id"] == second["decision_id"]


@pytest.mark.parametrize(
    ("raw", "canonical", "legacy"),
    [
        ("HISTORICAL", RuntimeMode.REPLAY, "HISTORICAL"),
        ("REPLAY", RuntimeMode.REPLAY, "HISTORICAL"),
        ("PAPER", RuntimeMode.LIVE_PAPER, "PAPER"),
        ("LIVE_PAPER", RuntimeMode.LIVE_PAPER, "PAPER"),
        ("REAL", RuntimeMode.LIVE_REAL, "REAL"),
        ("LIVE_REAL", RuntimeMode.LIVE_REAL, "REAL"),
    ],
)
def test_runtime_modes_have_backward_compatible_aliases(raw, canonical, legacy):
    parsed = RuntimeMode.parse(raw)
    assert parsed is canonical
    assert parsed.legacy_scanner_mode == legacy


def test_replay_disables_live_streams_and_orders():
    assert RuntimeMode.REPLAY.uses_live_market_data is False
    assert RuntimeMode.REPLAY.permits_live_orders is False
    assert RuntimeMode.LIVE_PAPER.uses_live_market_data is True
    assert RuntimeMode.LIVE_PAPER.permits_live_orders is False
    assert RuntimeMode.LIVE_REAL.permits_live_orders is True


def test_event_gate_runs_only_when_signature_or_explicit_generation_changes(monkeypatch):
    from config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "event_driven_scan_enabled", True)
    scanner = Scanner.__new__(Scanner)
    scanner._initial_setup_completed = True
    scanner._pending_live_signals = {}
    scanner._last_analysis_signature = ("same",)
    scanner._analysis_recompute_reason = ""

    assert scanner._analysis_recompute_due(("same",), False) == (False, "inputs unchanged")
    due, reason = scanner._analysis_recompute_due(("new",), False)
    assert due is True
    assert reason == "input changed"

    scanner._analysis_event_generation = 0
    assert scanner.request_analysis_recompute("explicit replay step") == 1
    assert scanner._analysis_recompute_reason == "explicit replay step"


def test_candidate_selection_prefers_quality_then_recency():
    older = SimpleNamespace(timestamp=datetime(2026, 9, 27, 10, 0), volume_spike_entry=True)
    newer = SimpleNamespace(timestamp=datetime(2026, 9, 27, 10, 1))
    same_time_stronger = SimpleNamespace(timestamp=datetime(2026, 9, 27, 10, 1))
    rows = [
        {"sig": older, "tf": "5min", "grade": "A", "confidence": 0.99, "reasons": []},
        {"sig": newer, "tf": "5min", "grade": "B", "confidence": 0.55, "reasons": []},
        {"sig": same_time_stronger, "tf": "15min", "grade": "A", "confidence": 0.80, "reasons": []},
    ]

    ranked = rank_entry_candidates(rows, lambda _signal: "")

    assert ranked[0][1] is older
    assert ranked[1][1] is same_time_stronger
    assert ranked[2][1] is newer


def test_external_call_deadline_opens_circuit():
    breaker = CallCircuitBreaker(failure_threshold=1, recovery_seconds=60.0)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(TimeoutError, match="deadline"):
            breaker.call(
                executor,
                lambda: time.sleep(0.20),
                timeout_seconds=0.01,
                label="test call",
            )
        assert breaker.state == "OPEN"
        with pytest.raises(CircuitOpenError, match="circuit is open"):
            breaker.call(executor, lambda: None, timeout_seconds=0.01, label="test call")


def test_decision_record_survives_trade_persistence_round_trip():
    trade = Trade(
        id="decision-roundtrip",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime.now(),
        trailing_stop=90.0,
        current_stop=90.0,
        lots=1,
        lot_size=1,
        grade="A",
        decision_record={
            "schema_version": "scanner_decision.v1",
            "decision_id": "stable-id",
            "policy_owner": "UT_SMART_LANE",
            "status": "ALLOW",
        },
    )

    restored = Trade.from_dict(trade.to_dict())

    assert restored.decision_record == trade.decision_record


def test_ensure_decision_record_closes_missing_candidate_coverage():
    candidate = _candidate("LONG", 0.75, grade="A")
    candidate.decision_record = {}
    record = ensure_decision_record(
        candidate,
        rank=1,
        decided_at=datetime(2026, 9, 27, 10, 0),
    )
    assert record["schema_version"] == "scanner_decision.v1"
    assert record["decision_id"]
    assert record["status"] == DecisionStatus.NOT_APPLICABLE.value
    assert candidate.decision_record == record


def test_contract_selection_phase_is_direction_symmetric():
    call = select_contract_strikes(
        inst_type="OPT",
        signal_type="BUY",
        spot=24_876,
        strike_interval=50,
        strike_selection="BOTH",
    )
    put = select_contract_strikes(
        inst_type="OPT",
        signal_type="SELL",
        spot=24_876,
        strike_interval=50,
        strike_selection="BOTH",
    )

    assert call == ("CE", 24_900.0, 24_850.0, [24_900.0, 24_850.0])
    assert put == ("PE", 24_900.0, 24_950.0, [24_900.0, 24_950.0])


def test_sizing_phase_preserves_quality_and_rr_factors():
    quality, rr_factor, total = candidate_size_multipliers(
        confidence=0.70,
        grade="A",
        final_rr=1.5,
        rr_scaling_enabled=True,
        rr_reference=2.5,
        rr_floor=0.5,
    )

    assert quality == pytest.approx(0.80)
    assert rr_factor == pytest.approx(0.60)
    assert total == pytest.approx(0.48)
