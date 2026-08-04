"""A partially filled exit must not leave the remainder naked for the whole session.

Sending an exit is preceded by CANCELLING the protective stop, so between the partial
fill and the 15:25 force-exit the residual has no stop, no working exit, and nothing
scheduled to touch it. The EOD flatten was added yesterday; this is the intraday half.

Waiting is the correct posture only while the exit order can still fill the rest. That is
not a judgement call — the broker states it. So the rule is:

  CANCELLED / REJECTED -> the order is finished and did not fill the rest; square off now
  still working        -> leave it alone, the remainder is coming
  COMPLETE             -> the whole quantity filled; a residual beside it is stale
                          position data, and exiting on it would open a naked short
  UNKNOWN              -> unprovable; withhold, because a second exit on top of a resting
                          one is how an exit becomes a reversed position
"""
import threading
from datetime import datetime

from trading.trade_manager import IST, Trade, TradeManager

SYMBOL = "NIFTY26AUG24000CE"


class _Broker:
    def __init__(self, lifecycle_state="CANCELLED", close_fails=False):
        self.lifecycle_state = lifecycle_state
        self.close_fails = close_fails
        self.exits = []

    def get_order_lifecycle(self, order_id):
        if self.lifecycle_state is None:
            raise RuntimeError("history lookup failed")
        return {"state": self.lifecycle_state, "reason": "", "transitions": []}

    def close_order(self, symbol, token, qty, side, product_type, price=0.0):
        self.exits.append((symbol, qty, side))
        return "" if self.close_fails else "EXIT-2"

    def cancel_order(self, oid, variety="NORMAL"):
        return True

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


def _trade(status="PARTIAL_EXIT"):
    t = Trade(
        id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=62.0, entry_time=datetime.now(IST), trailing_stop=55.0,
        current_stop=55.0, lots=1, lot_size=75, grade="A", inst_type="OPT",
        trading_symbol=SYMBOL, symbol_token="1", broker_order_id="251003000111111",
    )
    t.execution_status = status
    t.pending_exit = True
    t.exit_order_id = "EXIT-1"
    t.broker_quantity = 75
    return t


def _residual_row(qty=25):
    return {"data": [{
        "tradingsymbol": SYMBOL, "netqty": str(qty),
        "buyavgprice": "62.0", "sellavgprice": "0", "realised": "0",
    }]}


def _run(lifecycle_state, status="PARTIAL_EXIT", attempts=0):
    broker = _Broker(lifecycle_state=lifecycle_state)
    manager = _manager(broker)
    trade = _trade(status=status)
    trade.partial_exit_flatten_count = attempts
    manager.open_trades[trade.id] = trade
    manager.reconcile_broker_positions(_residual_row())
    return broker, trade


def test_a_dead_exit_order_releases_the_remainder_intraday():
    """CANCELLED means nothing more is coming — the residual is naked right now."""
    broker, trade = _run("CANCELLED")
    assert broker.exits, (
        "the exit order was cancelled with quantity still open and the remainder was "
        "left unprotected until the close"
    )
    assert broker.exits[0][1] == 25, "squared off the wrong quantity"
    assert trade.exit_order_id == "EXIT-2"


def test_a_rejected_exit_order_also_releases_the_remainder():
    broker, _ = _run("REJECTED")
    assert broker.exits and broker.exits[0][1] == 25


def test_a_working_exit_order_is_left_alone():
    """The remainder is still coming; a second exit here doubles the sell."""
    broker, trade = _run("OPEN")
    assert broker.exits == [], "sent a second exit while the first was still working"
    assert trade.execution_status == "PARTIAL_EXIT"


def test_a_complete_exit_order_is_not_acted_on():
    """COMPLETE means the whole quantity filled; the residual row is stale."""
    broker, _ = _run("COMPLETE")
    assert broker.exits == [], "exited against stale position data — that is a naked short"


def test_an_unknown_lifecycle_withholds_rather_than_guessing():
    """A failed lookup is not proof the order is dead."""
    for state in (None, "UNKNOWN", ""):
        broker, _ = _run(state)
        assert broker.exits == [], f"treated lifecycle {state!r} as a proven dead order"


def test_no_exit_order_id_is_unknown_not_dead():
    broker = _Broker(lifecycle_state="CANCELLED")
    manager = _manager(broker)
    trade = _trade()
    trade.exit_order_id = ""
    manager.open_trades[trade.id] = trade
    manager.reconcile_broker_positions(_residual_row())
    assert broker.exits == []


def test_re_exit_attempts_are_bounded():
    """A contract the broker keeps refusing must not spin an order every pass."""
    broker, trade = _run("CANCELLED", attempts=3)
    assert broker.exits == [], "kept re-submitting after three refused attempts"
    assert trade.execution_status == "RECOVERY_REQUIRED"


def test_the_attempt_counter_survives_a_restart():
    """Otherwise a crash resets the bound and the loop starts over."""
    trade = _trade()
    trade.partial_exit_flatten_count = 2
    assert Trade.from_dict(trade.to_dict()).partial_exit_flatten_count == 2
