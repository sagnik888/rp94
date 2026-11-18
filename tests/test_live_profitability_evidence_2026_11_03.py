"""Keep actual entry mode and alignment costs in manual lifecycle evidence."""
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from scanner import Scanner
from engine.candidate_normalization import normalize_loaded_exit_candidate
from engine.trade_row_adapter import reconcile_legacy_trade_row


def candidate(**changes):
    fields = dict(instrument="NIFTY", inst_type="OPT", direction="LONG", action="ENTRY",
                  price=100., stop=85., target=130., lots=2, lot_size=65,
                  source_engine="ut_ema", source_lane="UTEMA", exit_policy_owner="UTEMA",
                  utema_state={"alignment_added": False}, execution_ask_price=0.,
                  signal_timestamp=datetime(2026, 11, 3, 10, 0),
                  entry_timestamp=datetime(2026, 11, 3, 10, 5, 1))
    fields.update(changes)
    return NS(**fields)


@pytest.mark.parametrize("mode,intrabar", [("CANDLE_CLOSE", False), ("INTRACANDLE", True)])
def test_exit_inherits_actual_entry_fire_mode(mode, intrabar):
    scanner = Scanner.__new__(Scanner)
    entry = candidate(signal_fire_mode=mode, entered_intracandle=intrabar)
    exit_row = candidate(action="EXIT")
    scanner._inherit_exit_entry_metadata(exit_row, entry)
    assert exit_row.signal_fire_mode == mode
    assert exit_row.entered_intracandle is intrabar
    assert exit_row.entry_timestamp == entry.entry_timestamp


@pytest.mark.parametrize("aligned", [False, True])
@pytest.mark.parametrize("executable", [False, True])
def test_executable_exit_keeps_same_native_transaction_charges(monkeypatch, aligned, executable):
    settings = NS(opt_cost=80., fut_cost=200., tracker_executable_pricing_enabled=executable)
    monkeypatch.setattr("scanner.get_settings", lambda: settings)
    monkeypatch.setattr(Scanner, "_TRACKER_HALF_SPREAD", {"NIFTY": .001})
    scanner = Scanner.__new__(Scanner)
    entry = candidate(utema_state={"alignment_added": aligned})
    # An alignment addition is another transaction; its configured half-round-trip
    # fee must survive the switch from advisory marks to executable pricing.
    charge = 120. if aligned else 80.
    expected_exit = 110*.999 if executable else 110.
    assert scanner._candidate_exit_net_pnl(entry, 110.) == pytest.approx(
        round((expected_exit-100.)*130-charge, 2))


def test_executable_exit_keeps_acceptance_ask_and_charges_separate(monkeypatch):
    monkeypatch.setattr("scanner.get_settings", lambda: NS(
        opt_cost=80., fut_cost=200., tracker_executable_pricing_enabled=True))
    monkeypatch.setattr(Scanner, "_TRACKER_HALF_SPREAD", {"NIFTY": .001})
    scanner = Scanner.__new__(Scanner)
    entry = candidate(utema_state={"alignment_added": True}, execution_ask_price=100.2)
    assert scanner._candidate_exit_net_pnl(entry, 110.) == pytest.approx(
        round((110*.999-100.2)*130-120., 2))


def frozen_exit(**changes):
    row = candidate(action="EXIT", data_provenance="LIVE", accepted_by_gate=True,
                    entry_price=100., current_price=110., exit_price=110., pnl=1205.7,
                    exit_timestamp=datetime(2026, 11, 3, 10, 10), exit_quote_verified=True,
                    exit_reason="UTEMA_PREMIUM_TARGET", exit_decision_record={
                        "schema": "exit_decision.v1", "path": "manual", "owner": "UTEMA",
                        "controlling_reason": "UTEMA_PREMIUM_TARGET", "mark": 110.,
                        "accounting": {"basis": "RECORDED_MARK_GROSS_TO_EXECUTABLE_NET",
                                       "gross_pnl": 1300., "round_trip_cost": 94.3, "net_pnl": 1205.7}})
    for key, value in changes.items():
        setattr(row, key, value)
    return row


@pytest.mark.parametrize("owner", ["UTEMA", "GENERIC"])
@pytest.mark.parametrize("provenance", ["LIVE", ""])
def test_loading_closed_observation_does_not_apply_new_fee_model(owner, provenance):
    row = frozen_exit(exit_policy_owner=owner, data_provenance=provenance)
    row.exit_decision_record["owner"] = owner
    recalculate = Mock(return_value=-999.)
    changed = normalize_loaded_exit_candidate(row, futures_sl_pct=.2, options_sl_pct=15.,
        candidate_exit_resolution=Mock(), candidate_net_pnl=recalculate)
    assert not changed
    assert row.pnl == 1205.7
    recalculate.assert_not_called()


def test_dashboard_uses_frozen_exit_bill_after_settings_change(monkeypatch):
    monkeypatch.setattr("engine.trade_row_adapter.get_settings", lambda: NS(opt_cost=300., fut_cost=200.))
    original = vars(frozen_exit())
    result = reconcile_legacy_trade_row(original)
    assert result["pnl"] == 1205.7
    assert result["exit_price"] == original["exit_price"] == 110.
    assert original["pnl"] == 1205.7


@pytest.mark.parametrize("corruption", ["quantity", "mark", "gross", "net", "cost", "provenance", "quote"])
def test_inconsistent_frozen_record_cannot_override_current_row(corruption):
    row = frozen_exit()
    if corruption == "quantity":
        row.lots = 3
    elif corruption == "mark":
        row.exit_decision_record["mark"] = 111.
    elif corruption == "provenance":
        row.data_provenance = "BACKTEST"
    elif corruption == "quote":
        row.exit_quote_verified = False
    else:
        field = {"gross": "gross_pnl", "net": "net_pnl", "cost": "round_trip_cost"}[corruption]
        row.exit_decision_record["accounting"][field] = -1. if corruption == "cost" else 9999.
    recalculate = Mock(return_value=900.)
    normalize_loaded_exit_candidate(row, futures_sl_pct=.2, options_sl_pct=15.,
        candidate_exit_resolution=Mock(), candidate_net_pnl=recalculate)
    if corruption != "quote":
        recalculate.assert_called_once()
        assert row.pnl == 900.
