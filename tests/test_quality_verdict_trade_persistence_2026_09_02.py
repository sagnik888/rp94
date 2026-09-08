"""Quality evidence must survive restarts and remain attached to lifecycle events."""

from datetime import datetime
import json
import time

import pytest

from data.sqlite_db import TRADE_PERSISTED_COLUMNS, db
from trading.trade_manager import IST, Trade, TradeManager


def _trade(suffix: str = "BASE") -> Trade:
    return Trade(
        id=f"T-QUALITY-{suffix}",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=124.45,
        entry_time=IST.localize(datetime(2026, 9, 2, 10, 5)),
        trailing_stop=112.0,
        current_stop=112.0,
        lots=1,
        lot_size=65,
        grade="B+",
        confidence=0.61,
        inst_type="OPT",
        trading_symbol="NIFTY26SEP24500CE",
        symbol_token="60620",
        lifecycle_id=f"LC-QUALITY-{suffix}",
        status="OPEN",
    )


def _delete_trade(trade_id: str) -> None:
    with db.write_lock:
        db.conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        db.conn.commit()


@pytest.mark.parametrize("writer", ["single", "batch"])
def test_quality_verdict_is_json_encoded_and_survives_restart(writer):
    trade = _trade(writer.upper())
    observed_at = IST.localize(datetime(2026, 9, 2, 10, 4, 59))
    trade.quality_verdict = {
        "allowed": True,
        "route": "bplus_risky_confirmed",
        "reason_code": "QUALITY_OK",
        "checks": {"adx": 27.5, "trend_15m": True, "sources": ["utbot", "vwap"]},
        # The field contract is JSON-compatible, but persistence remains best-effort
        # when an instrumentation caller supplies a datetime diagnostic.
        "observed_at": observed_at,
    }
    expected = {
        **trade.quality_verdict,
        "observed_at": str(observed_at),
    }

    assert "quality_verdict" in TRADE_PERSISTED_COLUMNS
    _delete_trade(trade.id)
    try:
        if writer == "single":
            db.save_trade(trade.to_dict())
        else:
            db.save_trades([trade.to_dict()])

        with db.write_lock:
            raw = db.conn.execute(
                "SELECT quality_verdict FROM trades WHERE id = ?", (trade.id,)
            ).fetchone()[0]
        assert isinstance(raw, str)
        assert json.loads(raw) == expected

        row = next(item for item in db.load_open_trades() if item["id"] == trade.id)
        assert row["quality_verdict"] == expected
        assert Trade.from_dict(row).quality_verdict == expected
    finally:
        _delete_trade(trade.id)


def test_malformed_legacy_quality_json_fails_closed_to_empty_mapping():
    trade = _trade("MALFORMED")
    _delete_trade(trade.id)
    try:
        db.save_trades([trade.to_dict()])
        with db.write_lock:
            db.conn.execute(
                "UPDATE trades SET quality_verdict = ? WHERE id = ?",
                ("not-json", trade.id),
            )
            db.conn.commit()

        row = next(item for item in db.load_open_trades() if item["id"] == trade.id)
        assert row["quality_verdict"] == {}
        assert Trade.from_dict(row).quality_verdict == {}
    finally:
        _delete_trade(trade.id)


def _capture_trade_event(monkeypatch, trade: Trade, *, manager_auto_mode: bool) -> dict:
    import data.sqlite_db as sqlite_db

    seen = []
    monkeypatch.setattr(
        sqlite_db.db,
        "append_signal_lifecycle_events",
        lambda events: seen.extend(events) or len(events),
    )
    manager = TradeManager.__new__(TradeManager)
    manager.mode = "REAL"
    manager.auto_mode = manager_auto_mode
    manager._record_trade_forensics((trade,))

    deadline = time.time() + 5.0
    while not seen and time.time() < deadline:
        time.sleep(0.02)
    assert seen, "trade lifecycle event was not dispatched"
    return seen[0]


@pytest.mark.parametrize(
    "exec_type,manager_auto_mode,expected_mode",
    [("M", True, "MANUAL"), ("A", False, "AUTO")],
)
def test_trade_forensics_keeps_entry_mode_and_quality_payload(
    monkeypatch, exec_type, manager_auto_mode, expected_mode
):
    trade = _trade(expected_mode)
    trade.exec_type = exec_type
    trade.quality_verdict = {
        "policy_version": "quality_verdict.v3-evidence-routes",
        "route": "bplus_risky_confirmed",
        "reason_code": "QUALITY_OK",
        "checks": {"adx": 26.0, "volume_confirmed": True},
    }

    event = _capture_trade_event(
        monkeypatch, trade, manager_auto_mode=manager_auto_mode
    )

    # The manager state is deliberately the opposite of the trade's frozen origin.
    assert event["execution_mode"] == expected_mode
    assert event["payload"]["quality_verdict"] == trade.quality_verdict
