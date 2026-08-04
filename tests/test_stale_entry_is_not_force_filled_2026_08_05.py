"""A stale entry is replaced with a MARKET order, which guarantees a fill.

Live places confidence-gated LIMIT entries and sets no Kite TTL
(kite_entry_limit_ttl_minutes defaults to 0), so an unfilled limit rests until reconcile
notices it. After broker_fill_confirmation_timeout_seconds (12s) the order is cancelled
and re-sent with price=0.0 -- a MARKET order.

That is right when the setup still stands and the limit merely missed by a tick. It is
wrong when price has already run past the stop: the fill then opens a position that is a
loser the instant it exists. Twelve seconds is ample for that on a 1min option.

Fails OPEN throughout -- no mark or no stop means the existing replace behaviour is
unchanged, because a missing price must never silently stop entries.
"""
import threading
from datetime import datetime, timedelta

import pytest

from trading.trade_manager import IST, Trade, TradeManager

SYMBOL = "NIFTY2680524500CE"


class _Broker:
    def __init__(self, order_state="open"):
        self.order_state = order_state
        self.placed = []

    def cancel_order_confirmed(self, oid, variety="NORMAL", **kw):
        return True

    def cancel_order(self, oid, variety="NORMAL"):
        return True

    def place_order(self, *a, **k):
        self.placed.append(a)
        return "REPLACEMENT-1"

    def get_order_status(self, oid):
        return {"orderid": oid, "orderstatus": self.order_state}

    def get_positions(self):
        return {"data": []}

    def inspect_working_protective_stop(self, *a, **k):
        return {"state": "NONE", "order_id": "", "reason": "none", "matches": []}

    def find_working_protective_stop(self, *a, **k):
        return ""

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


def _stale_trade(mark, stop=180.0):
    t = Trade(
        id="TS1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=200.0, entry_time=datetime.now(IST), trailing_stop=stop,
        current_stop=stop, lots=1, lot_size=65, grade="A", inst_type="OPT",
        trading_symbol=SYMBOL, symbol_token="1", broker_order_id="251003000111111",
    )
    t.execution_status = "ENTRY_SUBMITTED"
    t.pending_execution = True
    t.broker_quantity = 0
    t.current_price = mark
    t.entry_submitted_at = datetime.now(IST) - timedelta(seconds=60)
    return t


def _reconcile(mark, stop=180.0, revalidator=None):
    broker = _Broker()
    manager = _manager(broker)
    trade = _stale_trade(mark, stop)
    if revalidator is not None:
        manager.entry_revalidator = revalidator
    manager.open_trades[trade.id] = trade
    manager.reconcile_broker_positions({"data": []})
    return manager, trade, broker


def test_a_setup_that_still_stands_is_replaced_at_market_as_before():
    """The feature must not stop ordinary missed-by-a-tick fills."""
    _m, trade, broker = _reconcile(mark=205.0)
    assert broker.placed, "a valid setup was abandoned instead of being filled"
    assert trade.entry_replace_count == 1


def test_an_entry_whose_stop_has_already_been_passed_is_not_force_filled():
    _m, trade, broker = _reconcile(mark=175.0)
    assert broker.placed == [], (
        "force-filled at market into a setup whose stop had already been breached — "
        "the position is a loser the instant it exists"
    )
    assert trade.exit_reason == "SIGNAL_INVALIDATED"
    assert trade.execution_status == "REJECTED"


def test_the_abandoned_row_is_closed_not_left_pending():
    manager, trade, _broker = _reconcile(mark=175.0)
    assert trade.id not in manager.open_trades
    assert trade.pending_execution is False
    assert trade.status == "CLOSED"


def test_price_exactly_at_the_stop_counts_as_breached():
    _m, _trade, broker = _reconcile(mark=180.0, stop=180.0)
    assert broker.placed == []


def test_a_missing_mark_fails_open():
    """No price is not evidence the setup failed."""
    _m, _trade, broker = _reconcile(mark=0.0)
    assert broker.placed, "a missing mark silently stopped an entry"


def test_a_missing_stop_fails_open():
    _m, _trade, broker = _reconcile(mark=205.0, stop=0.0)
    assert broker.placed


def test_an_external_revalidator_can_veto():
    """Optional hook for a scanner-side check; absent by default."""
    _m, trade, broker = _reconcile(mark=205.0, revalidator=lambda t: False)
    assert broker.placed == []
    assert trade.exit_reason == "SIGNAL_INVALIDATED"


def test_a_revalidator_that_approves_does_not_block():
    _m, _trade, broker = _reconcile(mark=205.0, revalidator=lambda t: True)
    assert broker.placed


def test_a_broken_revalidator_does_not_stop_trading():
    def _boom(trade):
        raise RuntimeError("scanner unavailable")

    _m, _trade, broker = _reconcile(mark=205.0, revalidator=_boom)
    assert broker.placed, "an erroring revalidator blocked a valid entry"


def test_a_short_future_breaches_upward():
    manager = _manager(_Broker())
    trade = _stale_trade(mark=205.0, stop=200.0)
    trade.inst_type = "FUT"
    trade.direction = "SHORT"
    assert manager._stale_entry_still_worth_filling(trade) is False
    trade.current_price = 195.0
    assert manager._stale_entry_still_worth_filling(trade) is True
