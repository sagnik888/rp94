"""P&L must be measured on what the broker filled, not on what was intended.

Trade.quantity is the INTENDED size (lots * lot_size) and carries no clamp. On a partial
fill reconcile records the real figure in broker_quantity and flags the trade PARTIAL.
broker_quantity was already authoritative for ORDER SIZING — exit quantity, protective
stop quantity — but P&L alone kept using the intended size, overstating it by the fill
ratio (65 intended / 30 filled reported -860 against a true -440).

That number feeds the daily loss cap and the loss guard, so an overstatement halts
trading early on money that was never at risk; the same error with the sign reversed
overstates a win and lets trading continue past the real cap.
"""
import threading
from datetime import datetime

import pytest

from trading.trade_manager import IST, Trade


def _trade(lots=1, lot_size=65, filled=0, entry=100.0, mark=88.0, inst="OPT"):
    t = Trade(
        id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=entry, entry_time=datetime.now(IST),
        trailing_stop=80.0, current_stop=80.0, lots=lots, lot_size=lot_size,
        grade="A", inst_type=inst, trading_symbol="NIFTY26AUG24000CE",
        symbol_token="1", current_price=mark,
    )
    t.broker_quantity = filled
    return t


def test_pnl_quantity_prefers_the_confirmed_fill():
    assert _trade(filled=30).pnl_quantity == 30


def test_pnl_quantity_falls_back_to_intended_before_anything_is_confirmed():
    """Pre-fill this is an estimate, and the intended size is the only figure there is."""
    assert _trade(filled=0).pnl_quantity == 65


def test_a_partial_fill_does_not_overstate_the_loss():
    """65 intended, 30 filled, mark 12 below entry: the loss is on 30, not 65."""
    partial = _trade(filled=30)
    full = _trade(filled=65)
    assert partial.unrealized_pnl > full.unrealized_pnl, (
        "a half-filled position reported the same or worse loss than a full one"
    )
    # Gross scales with the filled size; charges differ slightly, so compare gross.
    assert (partial.unrealized_pnl + partial_charges(partial)) == pytest.approx(
        (88.0 - 100.0) * 30
    )


def partial_charges(trade):
    from config.settings import get_settings
    from engine.trade_accounting import estimate_trade_charges

    return estimate_trade_charges(
        trade.entry_price, trade.current_price, trade.pnl_quantity,
        trade.inst_type, get_settings(), trade.instrument_multiplier,
        instrument=trade.instrument,
    )


def test_a_fully_filled_trade_is_unchanged():
    """The common case must behave exactly as before."""
    full = _trade(filled=65)
    assert full.pnl_quantity == full.quantity
    assert (full.unrealized_pnl + partial_charges(full)) == pytest.approx((88.0 - 100.0) * 65)


def test_a_short_future_partial_fill_also_scales():
    short = _trade(filled=30, lot_size=65, entry=100.0, mark=110.0, inst="FUT")
    short.direction = "SHORT"
    gross = (100.0 - 110.0) * 30
    assert (short.unrealized_pnl + partial_charges(short)) == pytest.approx(gross)


def test_an_overfill_is_respected_rather_than_clamped():
    """If the broker reports MORE than intended, that is the real exposure."""
    assert _trade(lot_size=65, filled=130).pnl_quantity == 130
