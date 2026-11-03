"""Native admission ownership and outcome units are explicit study evidence."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from intelligence.rtracker import PNL_BASIS_GROSS, PNL_BASIS_NET, RegimeTracker
from intelligence.signal_learning import freeze_decision, valid_evidence


def candidate(**changes):
    values = dict(instrument="NIFTY", timeframe="5min", direction="LONG", source_lane="S1", source_engine="s1",
                  source_engines=["s1"], grade="B", confidence=.45, setup_score=.45, price=100., stop=95., target=105.,
                  signal_timestamp=datetime(2026, 10, 5, 10), entry_timestamp=datetime(2026, 10, 5, 10, 5),
                  lifecycle_id="native", action="ENTRY", status="TRADE SIGNAL", accepted_by_gate=True,
                  inst_type="OPT", lots=1, lot_size=10,
                  quality_verdict={"allowed": False, "reason_code": "BELOW_BPLUS_ELIGIBILITY"},
                  decision_record={"policy_owner": "S1_LANE", "status": "ALLOW", "quality_policy_status": "NOT_APPLICABLE",
                                   "reason": "native lane and execution safety passed", "accepted_at": "2026-10-05T10:05:00"},
                  entry_policy_verdict={"allowed": True, "actual_rr": 1.}, grade_pillars={}, grading_inputs={})
    values.update(changes)
    return SimpleNamespace(**values)


def event(holder, decision):
    return RegimeTracker.__new__(RegimeTracker)._event_from_candidate(
        holder, decision=decision, reason="native", source="session_candidate", extra=None)


def test_native_authority_is_frozen_separately_from_nonbinding_generic_quality():
    holder = candidate()
    evidence = freeze_decision(holder, "ACCEPTED", datetime(2026, 10, 5, 10, 5))
    assert evidence["eligibility"] == holder.quality_verdict  # Compatibility contract.
    assert evidence["admission"]["allowed"] is True
    assert evidence["admission"]["policy_owner"] == "S1_LANE"
    assert evidence["admission"]["generic_quality_status"] == "NOT_APPLICABLE"
    assert evidence["admission"]["execution_policy"] == holder.entry_policy_verdict
    holder.decision_record["status"] = "BLOCK"
    holder.entry_policy_verdict["allowed"] = False
    assert evidence["admission"]["allowed"] is True and evidence["admission"]["execution_policy"]["allowed"] is True
    assert valid_evidence(evidence)


def test_binding_generic_rejection_remains_rejected_and_uncalibrated():
    holder = candidate(source_lane="QP", source_engine="quad_prime", accepted_by_gate=False,
                       decision_record={"policy_owner": "QUALITY_POLICY", "status": "BLOCK", "quality_policy_status": "BLOCK"})
    evidence = freeze_decision(holder, "REJECTED", datetime(2026, 10, 5, 10, 5))
    assert evidence["admission"]["allowed"] is False
    assert evidence["eligibility"]["allowed"] is False
    assert evidence["admission"]["generic_quality_status"] == "BLOCK"
    assert "calibrated_win_probability" not in evidence


def test_absent_canonical_record_does_not_invent_native_safety_approval():
    evidence = freeze_decision(candidate(decision_record={}, accepted_by_gate=False), "REJECTED", datetime(2026, 10, 5, 10, 5))
    assert evidence["admission"]["allowed"] is None
    assert evidence["admission"]["generic_quality_status"] == "NOT_APPLICABLE"


def test_rtracker_records_canonical_owner_without_turning_generic_quality_into_native_policy():
    holder = candidate()
    row = event(holder, "ACCEPTED")
    assert row["quality"]["allowed"] is False
    assert row["decision_record"] == holder.decision_record
    assert row["admission"]["allowed"] is True
    assert row["admission"]["policy_owner"] == "S1_LANE"


def test_terminal_and_dedup_rows_retain_original_acceptance_record():
    holder = candidate()
    accepted = event(holder, "ACCEPTED")
    original = deepcopy(accepted["decision_record"])
    holder.decision_record = {**original, "status": "NOT_APPLICABLE", "reason": "already accepted"}
    repeated = event(holder, "DEDUPED")
    assert repeated["accepted_decision_record"] == original
    assert repeated["decision_record"]["status"] == "NOT_APPLICABLE"
    assert repeated["admission"]["allowed"] is True


@pytest.mark.parametrize("basis,peak,expected_peak,expected_gross,expected_net", [
    (PNL_BASIS_NET, 598., 598., 678., 598.),
    (PNL_BASIS_GROSS, 540., 540., 540., 460.),
])
def test_peak_basis_is_preserved_and_both_units_are_reported(basis, peak, expected_peak, expected_gross, expected_net):
    holder = candidate(action="EXIT", pnl=40., exit_price=112., round_trip_cost=80., peak_pnl=peak,
                       peak_pnl_basis=basis, max_drawdown=-100., max_drawdown_basis=PNL_BASIS_NET,
                       pnl_path=[(0., 0.), (5., 120.)], pnl_path_basis=PNL_BASIS_GROSS)
    row = event(holder, "EXIT")
    assert row["peak_pnl"] == expected_peak
    assert row["peak_pnl_basis"] == basis
    assert row["max_drawdown_basis"] == PNL_BASIS_NET
    assert row["pnl_path_basis"] == PNL_BASIS_GROSS
    assert row["peak_pnl_gross"] == expected_gross
    assert row["peak_pnl_net"] == expected_net
    assert holder.peak_pnl == peak


def test_realized_peak_is_folded_in_declared_net_basis_without_double_subtracting_cost():
    row = event(candidate(action="EXIT", pnl=200., exit_price=128., round_trip_cost=80., peak_pnl=100.,
                          peak_pnl_basis=PNL_BASIS_NET, pnl_path_basis=PNL_BASIS_GROSS), "EXIT")
    assert row["peak_pnl"] == 200.
    assert row["tracked_peak_pnl"] == 100.
    assert row["peak_pnl_gross"] == 280.
    assert row["peak_pnl_net"] == 200.


def test_exact_sampled_gross_peak_prevents_clamped_zero_net_from_inventing_profit():
    row = event(candidate(action="EXIT", pnl=-100., exit_price=98., round_trip_cost=80., peak_pnl=0.,
                          peak_pnl_basis=PNL_BASIS_NET, peak_pnl_gross=20., pnl_path_basis=PNL_BASIS_GROSS), "EXIT")
    assert row["peak_pnl"] == 0. and row["peak_pnl_gross"] == 20.
    assert row["peak_pnl_net"] == 0.


def test_missing_gross_peak_remains_unknown_when_net_peak_was_zero_clamped():
    row = event(candidate(action="EXIT", pnl=-100., exit_price=98., round_trip_cost=80., peak_pnl=0.,
                          peak_pnl_basis=PNL_BASIS_NET, pnl_path_basis=PNL_BASIS_GROSS), "EXIT")
    assert row["peak_pnl"] == 0. and row["peak_pnl_gross"] is None
