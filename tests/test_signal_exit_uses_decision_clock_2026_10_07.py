"""An opposite bar is evidence for an exit, not the time its live mark was seen."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from engine.session_continuity import snapshot_continuity, restore_continuity
from engine.signal_processor import TradeCandidate
from scanner import Scanner


BAR = datetime(2026, 10, 7, 10, 33)
DECISION = BAR + timedelta(seconds=113)


def _entry(direction="LONG", stamp=None):
    return TradeCandidate(
        instrument="SENSEX", direction=direction, price=679.0, stop=600.0,
        target=800.0, lots=1, lot_size=20, grade="A", confidence=0.77,
        timeframe="15min", inst_type="OPT", option_type="CE", atm_strike=72800,
        multiplier=0.5, trading_symbol="SENSEX72800CE", symbol_token="1", rr=1.5,
        score=0.77, signal_timestamp=stamp or datetime(2026, 10, 7, 10, 15),
        current_price=634.5, action="ENTRY", reasons=[],
    )


def _host(mode="REAL"):
    host = Scanner.__new__(Scanner)
    entry = _entry()
    recorded = []
    host.mode = mode
    host.config = {"indices": {"SENSEX": {}}}
    host.trades = SimpleNamespace(open_trades={})
    host._latest_trade_candidates = {"SENSEX": [entry]}
    host._latest_exit_candidates = {}
    host._last_opposite_exit_signal_time = {}
    host._session_trade_candidates = {}
    host._session_squareoff_clock = lambda: DECISION
    host._market_ltp_shock_force_exit_opposite = lambda *_a: False
    host._opposite_exit_confirm_ready = lambda *_a: True
    host._quad_union_opposite_exit_allowed = lambda *_a: True
    host._active_session_entry_candidates = lambda *_a, **_k: [entry]
    host._get_session_trade_candidates = lambda *_a: recorded
    host._get_live_premium_cached = lambda *_a: 634.5
    host._candidate_exit_resolution = lambda prev, px, reason, text: (px, reason, text)
    host._candidate_net_pnl = lambda prev, px: (px - prev.price) * prev.lot_size - 80.0
    host._refresh_trade_candidates = lambda *_a: None
    host._remember_trade_candidates = lambda rows: recorded.extend(rows)
    host._notify = host.log_event = lambda *_a: None
    return host, entry, recorded


@pytest.mark.parametrize("path", ["opposite", "superseded"])
def test_manual_signal_exit_records_decision_time_and_trigger_bar_separately(path):
    host, entry, recorded = _host()
    entry.entry_timestamp = datetime(2026, 10, 7, 10, 30, 33)
    if path == "opposite":
        signal = SimpleNamespace(signal_type="SELL", timestamp=BAR, price=72900.0)
        assert host._handle_opposite_signal_exit("SENSEX", signal, "15min")
    else:
        host._refresh_same_direction_session_lifecycle = lambda *_a: False
        host._should_supersede_session_entry = lambda *_a: True
        host._close_superseded_session_entries([_entry("SHORT", BAR)])
    assert len(recorded) == 1
    row = recorded[0]
    assert row.exit_timestamp == DECISION
    assert row.entry_timestamp == entry.entry_timestamp
    assert row.source_signal_timestamp == entry.signal_timestamp
    assert row.current_price == 634.5
    evidence = row.exit_observation
    assert evidence["decided_at"] == DECISION.isoformat()
    assert evidence["reference_price"] == 634.5
    assert "observed_at" not in evidence
    assert evidence["signal_bar_at"] == BAR.isoformat()
    assert evidence["signal_timeframe"] == "15min"
    assert evidence["broker_fill"] is False
    restored = SimpleNamespace()
    restore_continuity(restored, snapshot_continuity(row))
    assert restored.exit_observation == evidence


@pytest.mark.parametrize("mode,expected", [("REAL", DECISION), ("HISTORICAL", BAR)])
def test_broker_exit_request_uses_live_decision_clock_but_preserves_replay_clock(mode, expected):
    host, entry, _recorded = _host(mode)
    host.trades.open_trades = {"open": entry}
    host._active_session_entry_candidates = lambda *_a, **_k: []
    host._latest_trade_candidates = {}
    calls = []
    host._close_and_record = lambda *a, **k: calls.append((a, k))
    signal = SimpleNamespace(signal_type="SELL", timestamp=BAR, price=72900.0)
    assert host._handle_opposite_signal_exit("SENSEX", signal, "15min")
    assert calls == [(("open", 634.5, "OPPOSITE_SIGNAL"), {"exit_time": expected})]


def test_historical_manual_opposite_exit_does_not_borrow_wall_time():
    host, _entry_row, recorded = _host("HISTORICAL")
    signal = SimpleNamespace(signal_type="SELL", timestamp=BAR, price=72900.0)
    assert host._handle_opposite_signal_exit("SENSEX", signal, "15min")
    assert recorded[0].exit_timestamp == BAR
