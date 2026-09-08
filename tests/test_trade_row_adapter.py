from datetime import datetime

from config.settings import get_settings
from engine.trade_row_adapter import (
    is_simulated_history_row,
    reconcile_legacy_trade_row,
    trade_row_event_seconds,
    trade_row_session_date,
    trade_row_to_signal,
)
from trading.trade_manager import IST


def test_trade_row_timestamp_parsing_accepts_seconds_milliseconds_and_iso():
    timestamp = IST.localize(datetime(2026, 6, 12, 10, 30)).timestamp()

    assert trade_row_event_seconds({"entry_timestamp": timestamp}) == timestamp
    assert trade_row_event_seconds({"entry_timestamp": timestamp * 1000}) == timestamp
    assert trade_row_event_seconds({"timestamp": "2026-06-12T10:30:00+05:30"}) == timestamp
    assert trade_row_session_date({"entry_timestamp": timestamp}).isoformat() == "2026-06-12"
    assert trade_row_session_date({"timestamp": "2026-06-12T10:30:00+05:30"}).isoformat() == "2026-06-12"


def test_reconcile_legacy_trade_row_clamps_exit_and_does_not_mutate(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "fut_cost", 200.0)
    original = {
        "instrument": "NIFTY",
        "inst_type": "FUT",
        "direction": "LONG",
        "entry_price": 100.0,
        "current_price": 95.0,
        "trailing_stop": 98.0,
        "exit_reason": "STOP_HIT",
        "lots": 1,
        "lot_size": 50,
    }

    repaired = reconcile_legacy_trade_row(original)

    assert repaired["exit_price"] == 98.0
    assert repaired["pnl"] == -300.0
    assert "exit_price" not in original


def test_reconcile_repairs_broker_fill_row_from_actual_fill_pair(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "opt_cost", 80.0)
    original = {
        "instrument": "BANKNIFTY 57200 PE",
        "inst_type": "OPT",
        "direction": "SHORT",
        "entry_price": 575.75,
        "exit_price": 576.0,
        "current_price": 0.0,
        "pnl": -183.66,
        "broker_order_id": "ENTRY-ORDER-1",
        "exit_order_id": "EXIT-ORDER-1",
        "lots": 1,
        "lot_size": 30,
        "exit_reason": "BREAKEVEN_STOP",
    }

    repaired = reconcile_legacy_trade_row(original)

    assert repaired["pnl"] == -72.5
    assert repaired["exit_price"] == 576.0
    assert repaired["current_price"] == 0.0
    assert original["pnl"] == -183.66


def test_simulated_history_row_and_signal_conversion():
    timestamp = IST.localize(datetime(2026, 6, 12, 10, 30)).timestamp()
    row = {
        "id": "H_NIFTY_5min_1",
        "instrument": "NIFTY",
        "direction": "LONG",
        "entry_price": 100.0,
        "exit_price": 105.0,
        "entry_timestamp": timestamp,
        "exit_timestamp": timestamp + 300,
        "status": "CLOSED",
        "rr_ratio": 2.0,
    }

    signal = trade_row_to_signal(row)

    assert is_simulated_history_row(row)
    assert signal["action"] == "EXIT"
    assert signal["status"] == "EXIT SIGNAL"
    assert signal["timestamp"].startswith("2026-06-12T10:30:00")
    assert signal["rr"] == 2.0
