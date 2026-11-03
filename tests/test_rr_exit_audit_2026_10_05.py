"""RR exit audit: approved geometry must survive a process restart."""

from datetime import datetime
import sqlite3
from unittest.mock import MagicMock

import pytest

from config.settings import get_settings
from engine import s1_lane, uts_lane
from trading.trade_manager import Trade
from tests.test_runner_mode_fixed_trail import setup_mock_scanner, make_mock_update_trade


def _trade(inst_type="FUT", direction="LONG", source_engine="quad_prime", source_lane="QP"):
    return Trade(
        id="rr-audit", instrument="NIFTY", timeframe="5min", direction=direction,
        entry_price=100.0, entry_time=datetime(2026, 10, 5, 10, 0),
        trailing_stop=110.0 if inst_type == "FUT" and direction == "SHORT" else 90.0,
        current_stop=110.0 if inst_type == "FUT" and direction == "SHORT" else 90.0,
        initial_stop=110.0 if inst_type == "FUT" and direction == "SHORT" else 90.0,
        lots=1, lot_size=1, grade="A", target=0.0, rr_ratio=3.0,
        inst_type=inst_type, source_engine=source_engine, source_lane=source_lane,
        entry_spot=24000.0 if inst_type == "OPT" else 100.0,
        instrument_multiplier=(-0.5 if direction == "SHORT" else 0.5) if inst_type == "OPT" else 1.0,
        runner_mode=True,
    )


def _proposal(monkeypatch, trade):
    scanner = setup_mock_scanner(monkeypatch)
    scanner._trade_one_way_cascade_context = MagicMock(return_value={})
    scanner._apply_option_stop_breathing_room = MagicMock(side_effect=lambda trade, stop, **kwargs: (stop, 0.0, False))
    prices = []
    scanner.trades = MagicMock()
    scanner.trades.open_trades = {trade.id: trade}
    scanner.trades.update_trade = make_mock_update_trade(trade, prices)
    if trade.inst_type == "OPT":
        scanner.candles.get_latest_price.return_value = 24100.0 if trade.direction == "LONG" else 23900.0
        scanner._get_live_premium_cached = MagicMock(return_value=150.0)
    else:
        scanner.candles.get_latest_price.return_value = 150.0 if trade.direction == "LONG" else 50.0
    scanner._update_active_trades()
    assert prices, "monitor must propose protection"
    return prices[-1]


def test_runner_approved_target_distance_survives_serialization():
    trade = _trade()
    # Approved entry target was 120: 20 points. The configured RR field stayed 3.
    trade.original_target_distance = 20.0
    restored = Trade.from_dict(trade.to_dict())
    assert getattr(restored, "original_target_distance", 0.0) == pytest.approx(20.0)


@pytest.mark.parametrize("writer", ["save_trade", "save_trades"])
def test_sqlite_writer_preserves_runner_target_distance(tmp_path, writer):
    from data.sqlite_db import DatabaseManager

    ledger = object.__new__(DatabaseManager)
    ledger._init_db(str(tmp_path / "runner.sqlite"))
    try:
        trade = _trade()
        trade.original_target_distance = 20.0
        payload = trade.to_dict()
        getattr(ledger, writer)([payload] if writer == "save_trades" else payload)
        restored = Trade.from_dict(ledger.load_all_trades()[0])
        assert getattr(restored, "original_target_distance", 0.0) == pytest.approx(20.0)
    finally:
        ledger.conn.close()


def test_existing_sqlite_schema_gains_runner_distance_without_repricing(tmp_path):
    from data.sqlite_db import DatabaseManager

    path = str(tmp_path / "legacy.sqlite")
    ledger = object.__new__(DatabaseManager)
    ledger._init_db(path)
    trade = _trade()
    payload = trade.to_dict()
    payload.pop("original_target_distance", None)
    ledger.save_trade(payload)
    ledger.conn.close()
    # Build the prior schema even after the migration exists; remove only the new
    # column in this isolated fixture. Existing entry/stop/target values stay fixed.
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        if "original_target_distance" in columns:
            conn.execute("ALTER TABLE trades DROP COLUMN original_target_distance")
    restored_ledger = object.__new__(DatabaseManager)
    restored_ledger._init_db(path)
    try:
        columns = {row[1] for row in restored_ledger.conn.execute("PRAGMA table_info(trades)")}
        assert "original_target_distance" in columns
        row = restored_ledger.load_all_trades()[0]
        assert (row["entry_price"], row["initial_stop"], row["target"], row["rr_ratio"]) == (100.0, 90.0, 0.0, 3.0)
        assert float(row.get("original_target_distance") or 0.0) == 0.0
    finally:
        restored_ledger.conn.close()


@pytest.mark.parametrize("inst_type,direction", [("FUT", "LONG"), ("FUT", "SHORT"), ("OPT", "LONG"), ("OPT", "SHORT")])
def test_runner_monitor_proposal_matches_before_and_after_restart(monkeypatch, inst_type, direction):
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime_s1")
    original = _trade(inst_type, direction)
    original.original_target_distance = 20.0
    restored = Trade.from_dict(original.to_dict())

    assert _proposal(monkeypatch, restored) == pytest.approx(_proposal(monkeypatch, original))


@pytest.mark.parametrize("inst_type,direction,evidence,distance", [
    ("OPT", "LONG", "premium_target", 20.0),
    ("OPT", "SHORT", "premium_target", 20.0),
    ("FUT", "LONG", "actual_rr", 20.0),
    ("FUT", "SHORT", "actual_rr", 20.0),
    ("FUT", "LONG", "legacy_rr", 30.0),
    ("FUT", "SHORT", "legacy_rr", 30.0),
])
def test_legacy_runner_recovers_then_freezes_only_contract_geometry(monkeypatch, inst_type, direction, evidence, distance):
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime_s1")
    trade = _trade(inst_type, direction)
    if evidence == "premium_target":
        trade.premium_target = 120.0
        # Policy RR is stale here; the actual approved option target is strongest.
        trade.entry_policy_actual_rr = 3.0
    elif evidence == "actual_rr":
        trade.entry_policy_actual_rr = 2.0
        # The underlying index target must never supply a futures-contract trail.
        trade.underlying_target = trade.spot_target = 24120.0
    # Even if reconciliation replaced the trailing field, initial entry risk stays.
    if evidence != "legacy_rr":
        trade.trailing_stop = 120.0 if inst_type == "FUT" and direction == "SHORT" else 80.0
    restored = Trade.from_dict(trade.to_dict())
    sign = -1 if inst_type == "FUT" and direction == "SHORT" else 1
    expected = 100.0 + sign * (50.0 - distance * 0.15)
    assert _proposal(monkeypatch, restored) == pytest.approx(expected)
    assert getattr(restored, "original_target_distance", 0.0) == pytest.approx(distance)
    # Subsequent edits to mutable stops cannot change the recovered fixed trail.
    restored.trailing_stop = 130.0 if inst_type == "FUT" and direction == "SHORT" else 70.0
    assert _proposal(monkeypatch, restored) == pytest.approx(expected)


@pytest.mark.parametrize("native,owner", [("s1", "S1"), ("ut_smart", "UTS")])
def test_native_runner_does_not_treat_three_r_as_hard_exit(native, owner):
    trade = _trade("OPT", "LONG", native, owner)
    trade.entry_spot = trade.underlying_entry = 24000.0
    trade.underlying_stop = 23990.0
    trade.target = 130.0
    trade.s1_peak_premium = 100.0
    # S1 with-trend runner runs past its normal 1R target to its separate ceiling.
    # UTS always marks its displayed target nominal; chart/stop/profit lock own exit.
    decision = s1_lane.exit_decision(
        trade, spot=24015.0, premium=130.0, trend=1, settings=get_settings(),
    )
    assert not decision.exit
    assert decision.runner
    assert uts_lane.target_rr(get_settings()) > 0
