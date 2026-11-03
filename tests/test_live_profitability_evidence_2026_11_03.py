"""Keep actual entry mode and alignment costs in manual lifecycle evidence."""
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from scanner import Scanner


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
