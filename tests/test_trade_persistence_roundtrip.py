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
