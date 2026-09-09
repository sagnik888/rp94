"""Trade rows must survive the save -> load round-trip with their broker identity.

2026-07-31: every live trade persisted with trading_symbol=NULL and symbol_token=NULL.
Trade.to_dict() — the only serializer save_state() feeds to db.save_trades() — simply
never emitted those two keys, and because the writer uses INSERT OR REPLACE, an omitted
column is NULLed on the stored row rather than left untouched. Those are precisely the
fields emergency_flatten_trade() and _ensure_protective_stop() guard on, so a reloaded
position could neither be flattened nor given an exchange-side stop, and FORCE_EOD_KILL
logged "emergency flatten unavailable" on a loop until the session ended.
"""

from datetime import datetime

import pytest

from data.sqlite_db import TRADE_PERSISTED_COLUMNS, db
from trading.trade_manager import IST, Trade


def _trade() -> Trade:
    return Trade(
        id="T0003_NIFTY_07311356",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=124.45,
        entry_time=IST.localize(datetime(2026, 7, 31, 13, 56, 19)),
        trailing_stop=105.78,
        current_stop=105.78,
        lots=1,
        lot_size=65,
        grade="A+",
        inst_type="OPT",
        atm_strike=24350.0,
        option_type="CE",
        status="OPEN",
        trading_symbol="NIFTY04AUG2624350CE",
        symbol_token="60620",
    )


def test_to_dict_emits_every_persisted_column():
    """to_dict() is the only writer; a column it omits is NULLed, not preserved."""
    emitted = set(_trade().to_dict())
    missing = TRADE_PERSISTED_COLUMNS - emitted - {"id"}
    assert not missing, f"to_dict() drops persisted columns, they will be NULLed: {sorted(missing)}"


# Fields from_dict() reads that are deliberately NOT stored: post-trade attribution and
# execution telemetry. Losing them on restart degrades reporting, not position management.
# Anything that decides how a LIVE position is managed does not belong here.
UNPERSISTED_BY_DESIGN = {
    "entered_intracandle", "entry_slippage", "exit_slippage", "execution_ask_price",
    "execution_bid_price", "execution_touch_age_seconds", "execution_touch_reason",
    "execution_touch_source", "execution_touch_usable", "quad_union_entry",
    "raw_union_entry", "signal_entry_price", "signal_fire_mode", "source_engine",
    "source_engines",
    "signal_timestamp",   # alias; the value persists under signal_time
    # Diagnostics only — nothing reads the path to manage a position; it exists so the
    # "would a tighter trail have kept more?" question is answerable offline. The copy
    # the analysis actually reads lives on the TradeCandidate, which persists whole via
    # json.dumps into session_signals.payload, so the offline record is not lost here.
    "pnl_path",
    # Historical-only replay evidence is emitted directly into the backtest report.
    # Persisting hundreds of minute bars per simulated trade in the live SQLite
    # position table would add RAM/DB pressure and cannot affect position recovery.
    "replay_price_path", "replay_exit_trace",
}


def test_every_field_from_dict_reads_is_actually_stored():
    """The reverse of the check above, which was the direction that stayed open.

    Asserting only TRADE_PERSISTED_COLUMNS <= to_dict() catches a stored column nobody
    emits. It does NOT catch the opposite: a field added to to_dict()/from_dict() but
    never added to TRADE_PERSISTED_COLUMNS. save_trades filters every row through that
    set, so such a field is dropped on write and from_dict quietly falls back to its
    default on the next restart — no error, no NULL, just the default.

    That is how partial_exit_flatten_count (which BOUNDS re-submission of a partially
    filled exit) and reversal_watch_required both reverted to zero/False on restart while
    the module comment claimed this file guarded the three-way sync.
    """
    import inspect
    import re

    src = inspect.getsource(Trade.from_dict)
    read = set(re.findall(r'data\.get\(\s*["\']([A-Za-z0-9_]+)["\']', src))
    unstored = sorted(read - set(TRADE_PERSISTED_COLUMNS) - UNPERSISTED_BY_DESIGN)
    assert not unstored, (
        "from_dict() reads these but save_trades never stores them, so they silently "
        f"revert to their defaults on restart: {unstored}. Add them to "
        "TRADE_PERSISTED_COLUMNS (and the _ensure_columns type map), or to "
        "UNPERSISTED_BY_DESIGN if losing them genuinely cannot affect a live position."
    )


def test_broker_contract_identity_survives_save_and_load():
    trade = _trade()
    with db.write_lock:
        db.conn.execute("DELETE FROM trades WHERE id = ?", (trade.id,))
        db.conn.commit()

    db.save_trades([trade.to_dict()])
    # Save twice: the first write could insert, only a re-save proves REPLACE keeps them.
    db.save_trades([trade.to_dict()])

    row = next(r for r in db.load_open_trades() if r["id"] == trade.id)
    assert row["trading_symbol"] == "NIFTY04AUG2624350CE"
    assert row["symbol_token"] == "60620"

    restored = Trade.from_dict(row)
    assert restored.trading_symbol == trade.trading_symbol
    assert restored.symbol_token == trade.symbol_token


def test_protective_stop_state_survives_save_and_load():
    """Forgetting a live SL-M order lets a recovery flatten double-exit the position."""
    trade = _trade()
    trade.protective_order_id = "250731000123"
    trade.protective_trigger = 105.78
    trade.protective_quantity = 65
    with db.write_lock:
        db.conn.execute("DELETE FROM trades WHERE id = ?", (trade.id,))
        db.conn.commit()

    db.save_trades([trade.to_dict()])
    db.save_trades([trade.to_dict()])

    restored = Trade.from_dict(
        next(r for r in db.load_open_trades() if r["id"] == trade.id)
    )
    assert restored.protective_order_id == "250731000123"
    assert restored.protective_trigger == pytest.approx(105.78)
    assert restored.protective_quantity == 65


def test_entry_policy_and_dual_geometry_evidence_survive_save_and_load():
    """A restart must not erase the evidence that authorised fresh exposure."""
    trade = _trade()
    trade.underlying_entry = 24383.6
    trade.underlying_stop = 24340.0
    trade.underlying_target = 24449.0
    trade.premium_entry = 124.45
    trade.premium_stop = 111.20
    trade.premium_target = 144.33
    trade.geometry_observed_at = "2026-08-29T10:11:12+05:30"
    trade.geometry_source = "actual_fill"
    trade.geometry_verified = True
    trade.expected_move_pct = 0.31
    trade.expected_move_floor_pct = 0.15
    trade.expected_move_probability = 0.72
    trade.expected_move_sample_count = 240
    trade.expected_move_horizon_minutes = 15
    trade.expected_move_generated_at = "2026-08-29T10:11:10+05:30"
    trade.expected_move_data_as_of = "2026-08-29T10:10:00+05:30"
    trade.expected_move_model_version = "mfe_quantile.v1"
    trade.expected_move_source = "fyers_history"
    trade.entry_policy_allowed = True
    trade.entry_policy_phase = "actual_broker_fill"
    trade.entry_policy_checked_at = "2026-08-29T10:11:12+05:30"
    trade.entry_policy_reason = "allowed"
    trade.entry_policy_actual_rr = 1.5004
    trade.entry_policy_underlying_stop_pct = 0.1788
    trade.entry_policy_version = "final_entry_policy.v1"

    with db.write_lock:
        db.conn.execute("DELETE FROM trades WHERE id = ?", (trade.id,))
        db.conn.commit()
    db.save_trades([trade.to_dict()])

    restored = Trade.from_dict(
        next(r for r in db.load_open_trades() if r["id"] == trade.id)
    )
    assert restored.underlying_entry == pytest.approx(24383.6)
    assert restored.premium_stop == pytest.approx(111.20)
    assert restored.geometry_verified is True
    assert restored.expected_move_probability == pytest.approx(0.72)
    assert restored.expected_move_model_version == "mfe_quantile.v1"
    assert restored.entry_policy_allowed is True
    assert restored.entry_policy_actual_rr == pytest.approx(1.5004)
    assert restored.entry_policy_version == "final_entry_policy.v1"


# ── phantom P&L on trades that never reached the market ──────────────────────
def _rejected(**over):
    t = _trade()
    t.status = "CLOSED"
    t.execution_status = "REJECTED"
    t.exit_reason = "BROKER_ERROR"
    t.broker_order_id = ""
    t.broker_quantity = 0
    t.pnl = -80.0
    for key, value in over.items():
        setattr(t, key, value)
    return t


def test_a_trade_that_never_reached_the_market_has_no_pnl():
    """2026-07-31 rows showed -80 on orders the exchange never accepted.

    An UNREALISED mark was written into the persisted pnl column while the entry was
    still pending, and survived onto the closed row once the order turned out to have
    been rejected. There was no position; there can be no P&L.
    """
    assert _rejected().to_dict()["pnl"] == 0.0
    assert _rejected(exit_reason="CONTRACT_MISSING").to_dict()["pnl"] == 0.0


def test_an_ambiguous_acknowledgement_keeps_its_pnl():
    """The order may be live at the exchange — zeroing it would hide real money."""
    trade = _rejected(execution_status="RECOVERY_REQUIRED", exit_reason="")
    assert trade.to_dict()["pnl"] != 0.0


def test_a_rejection_that_still_carries_an_order_id_keeps_its_pnl():
    """An order id means it reached the exchange; the rejection may be partial."""
    assert _rejected(broker_order_id="250801000123").to_dict()["pnl"] != 0.0


def test_a_filled_trade_is_untouched():
    trade = _trade()
    trade.status = "CLOSED"
    trade.execution_status = "OPEN"
    trade.broker_order_id = "250801000123"
    trade.broker_quantity = 65
    trade.pnl = 1250.0
    assert trade.to_dict()["pnl"] == 1250.0
