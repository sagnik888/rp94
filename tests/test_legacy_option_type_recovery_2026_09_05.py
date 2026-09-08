"""Legacy option rows must not silently rehydrate as futures."""
from __future__ import annotations

from engine.trade_row_adapter import reconcile_legacy_trade_row, trade_row_to_signal
from trading.trade_manager import Trade


def _legacy_pe_row():
    return {
        "id": "legacy-pe", "instrument": "NIFTY 24500 PE",
        "trading_symbol": "NIFTY24500PE", "option_type": "PE",
        "direction": "SHORT", "entry_price": 100.0, "exit_price": 120.0,
        "trailing_stop": 90.0, "target": 120.0, "lots": 1, "lot_size": 65,
        "entry_timestamp": 0, "status": "CLOSED", "exit_reason": "TARGET_HIT",
    }


def test_legacy_ce_pe_trade_rehydrates_as_option_not_short_future():
    trade = Trade.from_dict(_legacy_pe_row())
    assert trade.inst_type == "OPT"
    assert trade.direction == "SHORT"  # bearish underlying view; contract still bought PE


def test_legacy_option_accounting_and_signal_preserve_option_semantics():
    row = _legacy_pe_row()
    reconciled = reconcile_legacy_trade_row(row)
    assert reconciled["pnl"] > 0  # premium rose from 100 to 120 on bought PE
    assert trade_row_to_signal(row)["inst_type"] == "OPT"


def test_plain_legacy_row_without_option_evidence_remains_future():
    row = {"instrument": "NIFTY", "direction": "LONG", "entry_price": 100.0}
    assert Trade.from_dict(row).inst_type == "FUT"
    assert trade_row_to_signal(row)["inst_type"] == "FUT"
