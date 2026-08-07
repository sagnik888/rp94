"""Three ways an order could strand real money at the exchange overnight.

1. AMBIGUOUS FLATTEN. emergency_flatten_trade marks RECOVERY_REQUIRED when close_order
   cannot be acknowledged, and check_session_end re-dispatches every RECOVERY_REQUIRED
   row to it on each pass (~1 Hz). Without a latch, a fresh MARKET exit went out every
   second until the close; if the first had in fact filled, the account ends up SHORT and
   naked in an NRML product the broker will not square off.

2. PARTIAL_EXIT. A partially filled exit sets PARTIAL_EXIT + pending_exit, and close_trade
   refuses both. The protective stop was already cancelled before the exit was sent, so
   the unfilled remainder had no stop, no retry, and no EOD flatten.

3. STALE-ENTRY CANCEL/REPLACE. cancel_order returns True on the API acknowledgement
   alone. The order is being replaced precisely BECAUSE price is moving toward its limit,
   so the fill routinely wins the race — leaving the original filled AND a MARKET
   replacement live, i.e. twice the intended size.
"""
import threading
from datetime import datetime, timedelta

import pytest

from trading.order_retry import BrokerOrderAmbiguous
from trading.trade_manager import IST, Trade, TradeManager

SYMBOL = "NIFTY26AUG24000CE"


class _Broker:
    def __init__(self, exit_raises=None, cancel_confirmed=True, order_state=None):
        self.exit_raises = exit_raises
        self.cancel_confirmed = cancel_confirmed
        self.order_state = order_state
        self.exits = []
        self.placed = []

    def close_order(self, symbol, token, qty, side, product_type, price=0.0):
        self.exits.append((symbol, qty, side))
        if self.exit_raises is not None:
            raise self.exit_raises
        return "EXIT-1"

    def cancel_order(self, oid, variety="NORMAL"):
        return True

    def cancel_order_confirmed(self, oid, variety="NORMAL", **kw):
        return self.cancel_confirmed

    def place_order(self, *a, **k):
        self.placed.append(a)
        return "NEW-ORDER-1"

    def get_order_status(self, oid):
        return self.order_state

    def get_positions(self):
        return {"data": []}

    def inspect_working_protective_stop(self, *a, **k):
        return {"state": "NONE", "order_id": "", "reason": "none", "matches": []}

    def find_working_protective_stop(self, *a, **k):
        return ""

    def place_protective_stop(self, *a, **k):
        return "SLM-1"

    def get_fill(self, order_id):
        return {"state": "UNKNOWN", "reason": "stub"}


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


def _trade(status="RECOVERY_REQUIRED", qty=75):
    t = Trade(
        id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=62.0, entry_time=datetime.now(IST), trailing_stop=55.0,
        current_stop=55.0, lots=1, lot_size=qty, grade="A", inst_type="OPT",
        trading_symbol=SYMBOL, symbol_token="1", broker_order_id="251003000111111",
    )
    t.execution_status = status
    t.broker_quantity = qty
    return t


def test_an_ambiguous_flatten_is_not_re_sent_every_pass():
    """One unacknowledged exit must not become ten live market orders."""
    broker = _Broker(exit_raises=BrokerOrderAmbiguous("read timed out after submit"))
    manager = _manager(broker)
    trade = _trade()
    manager.open_trades[trade.id] = trade

    for _ in range(10):
        manager.emergency_flatten_trade(trade, "FORCE_EOD_KILL")

    assert len(broker.exits) == 1, (
        f"submitted {len(broker.exits)} exits for one position after an ambiguous "
        "acknowledgement; the first may already be live"
    )


def test_reconciliation_releases_the_ambiguous_latch():
    """Once the broker states the real quantity, automatic exits may resume."""
    broker = _Broker(exit_raises=BrokerOrderAmbiguous("timeout"))
    manager = _manager(broker)
    trade = _trade()
    manager.open_trades[trade.id] = trade
    manager.emergency_flatten_trade(trade, "FORCE_EOD_KILL")
    assert trade.id in manager._flatten_ambiguous_latched

    manager.reconcile_broker_positions({"data": [{
        "tradingsymbol": SYMBOL, "netqty": "75",
        "buyavgprice": "62.0", "sellavgprice": "0", "realised": "0",
    }]})
    assert trade.id not in manager._flatten_ambiguous_latched


def test_a_partial_exit_is_squared_off_at_the_close():
    """close_trade refuses PARTIAL_EXIT, so the EOD path must not route it there."""
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade(status="PARTIAL_EXIT")
    trade.pending_exit = True
    trade.broker_quantity = 25          # residual after a partial fill
    manager.open_trades[trade.id] = trade

    # A fixed WEDNESDAY. datetime.now() made this pass Mon-Fri and fail every SATURDAY,
    # when _session_is_a_trading_day correctly reports a non-trading day and
    # check_session_end takes the "cannot square off" branch instead.
    manager.check_session_end(
        datetime.now(IST).replace(
            year=2026, month=8, day=5, hour=15, minute=29, second=0, microsecond=0
        )
    )

    assert broker.exits, "the residual of a partially filled exit was never squared off"
    assert broker.exits[0][1] == 25


def test_a_stale_entry_is_not_replaced_on_an_unproven_cancel():
    """cancel_order_confirmed returns False when the order actually filled."""
    broker = _Broker(cancel_confirmed=False,
                     order_state={"orderid": "251003000111111", "orderstatus": "open"})
    manager = _manager(broker)
    trade = _trade(status="ENTRY_SUBMITTED")
    trade.pending_execution = True
    trade.broker_quantity = 0
    trade.entry_submitted_at = datetime.now(IST) - timedelta(seconds=60)
    manager.open_trades[trade.id] = trade

    manager.reconcile_broker_positions({"data": []})

    assert broker.placed == [], (
        "sent a MARKET replacement while the original order had filled — double size"
    )


def test_a_proven_cancel_still_replaces_the_stale_entry():
    """The feature must keep working when the cancel is genuinely confirmed."""
    broker = _Broker(cancel_confirmed=True,
                     order_state={"orderid": "251003000111111", "orderstatus": "open"})
    manager = _manager(broker)
    trade = _trade(status="ENTRY_SUBMITTED")
    trade.pending_execution = True
    trade.broker_quantity = 0
    trade.entry_submitted_at = datetime.now(IST) - timedelta(seconds=60)
    manager.open_trades[trade.id] = trade

    manager.reconcile_broker_positions({"data": []})

    assert len(broker.placed) == 1
    assert trade.entry_replace_count == 1
