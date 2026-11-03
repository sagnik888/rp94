"""An actual broker fill must not display the original quote's planned RR."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from tests.test_rr_display_audit_2026_10_05 import render
from trading.trade_dashboard import build_dashboard_rows
from trading.trade_manager import IST, Trade, TradeManager


def filled_trade(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "scanner_engine", "quad_prime_s1")
    monkeypatch.setattr(settings, "entry_policy_min_rr", 1.5)
    manager = TradeManager.__new__(TradeManager)
    manager.fresh_entry_policy_guard = lambda trade: (True, "")
    trade = Trade(
        id="T-real-fill-rr", instrument="NIFTY", timeframe="5min",
        direction="LONG", entry_price=107.5, entry_time=datetime.now(IST),
        trailing_stop=100., current_stop=100., initial_stop=100.,
        target=130., rr_ratio=3., lots=1, lot_size=65, grade="A",
        inst_type="FUT", source_lane="QP", source_engine="multi",
        broker_order_id="FILLED-ORDER", execution_status="OPEN",
    )
    assert (trade.target - trade.entry_price) / (trade.entry_price - trade.initial_stop) == 3.
    # The broker fills above the accepted quote; the approved stop and target
    # remain fixed. The guard evaluates those levels against the actual fill.
    trade.entry_price = 110.
    allowed, reason = manager._actual_fill_entry_policy_verdict(trade)
    assert allowed, reason
    assert trade.entry_policy_actual_rr == 2.
    assert (trade.target - trade.entry_price) / (trade.entry_price - trade.initial_stop) == 2.
    return trade


@pytest.mark.parametrize("current_stop", [100., 115.])
def test_dashboard_rr_uses_frozen_actual_fill_geometry_after_trailing(monkeypatch, current_stop):
    trade = filled_trade(monkeypatch)
    trade.current_stop = current_stop
    trade.trailing_stop = current_stop
    manager = SimpleNamespace(options_sl_pct=15., orphan_positions={})
    row = build_dashboard_rows(manager, [trade], [], include_orphans=False)[0]
    assert row["initial_stop"] == 100.
    result = render(row)
    assert result["resolved"] == 2.
    assert result["formatted"] in {"1:2.0", "1:2.00"}


def test_actual_fill_rr_survives_trade_storage_round_trip(monkeypatch):
    trade = filled_trade(monkeypatch)
    restored = Trade.from_dict(trade.to_dict())
    assert restored.entry_policy_actual_rr == 2.
    assert restored.initial_stop == 100.
