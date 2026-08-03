"""A squared-off position row must not close a trade whose entry is still working.

Kite keeps an intraday position that has been squared off in `net` with quantity 0 for
the rest of the session, carrying the earlier trade's sell average and realised P&L. That
zero row is also this system's ONLY exit-fill confirmation signal, so the two meanings
collide the moment the system re-enters a strike it already traded today — routine, since
the ATM strike does not move in the chop/range pockets the re-entry cooldown targets.

Before the guard, reconcile read the residue as "this trade went flat" and closed a trade
whose entry order was still live at the exchange. Three consequences, all real money:

  * the earlier trade's realised P&L was booked onto the new trade, feeding the daily
    loss circuit breaker with a double-counted figure
  * the live order id was discarded (open_trade only records it if the trade is still in
    open_trades), so nothing local knew the order existed
  * the resulting position was never managed — no protective stop, and check_session_end
    routes through close_trade, which cannot see a trade that has left open_trades, so a
    naked long option rides past the EOD force-exit

The order book was already being consulted and answering "open"; its answer was simply
discarded, because it was only read inside the `position is None` branch.
"""
import threading
from datetime import datetime, timedelta

import pytest

from trading.trade_manager import IST, Trade, TradeManager

SYMBOL = "NIFTY26AUG24000CE"

# The residue an EARLIER trade on this strike leaves behind for the rest of the session.
SQUARED_OFF_ROW = {
    "tradingsymbol": SYMBOL, "netqty": "0",
    "buyavgprice": "114.0", "sellavgprice": "61.0", "realised": "-3900.0",
}
LIVE_ROW = {
    "tradingsymbol": SYMBOL, "netqty": "75",
    "buyavgprice": "62.0", "sellavgprice": "0", "realised": "0",
}


class _Broker:
    def __init__(self, order_state=None):
        self.order_state = order_state
        self.cancelled = []
        self.protective = []

    def get_order_status(self, oid):
        return self.order_state

    def cancel_order(self, oid, variety="NORMAL"):
        self.cancelled.append((oid, variety))
        return True

    def get_positions(self):
        return {"data": []}

    def find_working_protective_stop(self, symbol, token, side):
        return ""

    def place_protective_stop(self, *a, **k):
        self.protective.append(a)
        return "SLM-1"

    def get_fill(self, order_id):
        return {"state": "UNKNOWN", "reason": "probe"}


def _manager(broker):
    m = TradeManager.__new__(TradeManager)
    m.broker = broker
    m.ui_logger = None
    m.mode = "REAL"
    m.product_type = "CARRYFORWARD"
    m.open_trades = {}
    m.closed_trades = []
    m.orphan_positions = {}
    m._lock = threading.RLock()
    m._protection_lock = threading.RLock()
    m.save_state = lambda: None
    m._last_protective_sync = {}
    m._last_protective_status_poll = {}
    m._last_recovery_alert = {}
    m._last_mark_to_market_save = 0.0
    m.max_positions = 4
    m.futures_sl_pct = 0.3
    m.options_sl_pct = 15.0
    m._log_ui = lambda *a, **k: None
    m.on_notification = None
    return m


def _trade(status, broker_quantity, pending, submitted_secs_ago=1):
    t = Trade(
        id="T0002_NIFTY_08031130", instrument="NIFTY", timeframe="5min",
        direction="LONG", entry_price=62.0, entry_time=datetime.now(IST),
        trailing_stop=55.0, current_stop=55.0, lots=1, lot_size=75, grade="A",
        inst_type="OPT", trading_symbol=SYMBOL, symbol_token="1",
        broker_order_id="251003000111111",
    )
    t.execution_status = status
    t.pending_execution = pending
    t.broker_quantity = broker_quantity
    t.entry_submitted_at = datetime.now(IST) - timedelta(seconds=submitted_secs_ago)
    return t


@pytest.mark.parametrize("status", ["ENTRY_SUBMITTED", "ENTRY_PENDING", "PARTIAL"])
def test_an_unconfirmed_entry_survives_an_earlier_trades_squared_off_row(status):
    broker = _Broker({"orderid": "251003000111111", "orderstatus": "open"})
    manager = _manager(broker)
    trade = _trade(status, broker_quantity=0, pending=True)
    manager.open_trades[trade.id] = trade

    result = manager.reconcile_broker_positions({"data": [SQUARED_OFF_ROW]})

    assert result["closed"] == 0, "closed a trade whose entry order is still working"
    assert trade.id in manager.open_trades
    assert manager.closed_trades == []


def test_the_earlier_trades_realised_pnl_is_not_booked_onto_the_new_trade():
    """-3900 belongs to a trade that already closed; double-counting it skews the
    daily loss breaker and could also let trading continue past the real cap."""
    broker = _Broker({"orderid": "251003000111111", "orderstatus": "open"})
    manager = _manager(broker)
    trade = _trade("ENTRY_SUBMITTED", broker_quantity=0, pending=True)
    manager.open_trades[trade.id] = trade

    manager.reconcile_broker_positions({"data": [SQUARED_OFF_ROW]})

    assert float(getattr(trade, "pnl", 0.0) or 0.0) != -3900.0
    assert float(getattr(trade, "exit_price", 0.0) or 0.0) != 61.0


def test_a_confirmed_open_position_going_flat_still_closes_normally():
    """The guard must not break the ONLY exit-fill confirmation path."""
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade("EXIT_PENDING", broker_quantity=75, pending=False)
    trade.exit_reason = "TRAILING_STOP"
    manager.open_trades[trade.id] = trade

    result = manager.reconcile_broker_positions({"data": [SQUARED_OFF_ROW]})

    assert result["closed"] == 1, "a genuine exit fill was no longer recognised"
    assert manager.closed_trades and manager.closed_trades[0].exit_reason == "TRAILING_STOP"


def test_an_open_trade_that_went_flat_outside_the_manager_still_closes():
    """SL-M fired at the exchange: broker_quantity was confirmed, so this is a real exit."""
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade("OPEN", broker_quantity=75, pending=False)
    manager.open_trades[trade.id] = trade

    assert manager.reconcile_broker_positions({"data": [SQUARED_OFF_ROW]})["closed"] == 1


def test_a_real_fill_on_the_same_contract_is_still_confirmed():
    """A genuine non-zero row must confirm the entry, not be diverted by the guard."""
    broker = _Broker({"orderid": "251003000111111", "orderstatus": "COMPLETE"})
    manager = _manager(broker)
    trade = _trade("ENTRY_SUBMITTED", broker_quantity=0, pending=True)
    manager.open_trades[trade.id] = trade

    result = manager.reconcile_broker_positions({"data": [LIVE_ROW]})

    assert result["confirmed"] == 1
    assert trade.execution_status == "OPEN"
    assert trade.broker_quantity == 75
