"""A lost acknowledgement is answerable, not unknowable.

Kite echoes the order tag on the order book, on trades and on every postback. The live
system already WROTE a tag on entry orders and then read it back with nothing -- so an
unacknowledged submission still had to be treated as unknowable, and every caller latched
and escalated to a human. That is the seventh primitive in this codebase written correctly
and never wired to a consumer.

With the lookup in place the question has a real answer:
  FOUND     the order reached the exchange -> adopt its id, no latch, no phone call
  ABSENT    a SUCCESSFUL read that found no such tag -> nothing arrived, retry is safe
  UNKNOWN   the order book could not be read -> still unknowable, latch (unchanged)
  AMBIGUOUS two orders share the tag -> escalate, never guess

ABSENT is only ever concluded from a read that succeeded. Inferring "never placed" from a
scan that errored is exactly how a live position gets duplicated.
"""
import threading
from datetime import datetime

import pytest

from trading.kite_broker import KiteBroker
from trading.order_retry import BrokerOrderAmbiguous
from trading.trade_manager import IST, Trade, TradeManager

SYMBOL = "NIFTY2680424500CE"
TRADE_ID = "T0005_NIFTY_07311451"


# ── the tag itself ───────────────────────────────────────────────────────────

def test_the_entry_tag_is_unchanged_by_the_leg_aware_form():
    """An order already resting at the exchange must stay findable across this change."""
    assert KiteBroker.order_tag(TRADE_ID) == "T0005_07311451"
    assert KiteBroker.order_tag(TRADE_ID, "ENTRY", 0) == "T0005_07311451"


def test_each_leg_gets_a_distinct_tag():
    """An exit sharing its entry's tag would return AMBIGUOUS exactly when it matters."""
    tags = {
        KiteBroker.order_tag(TRADE_ID, leg)
        for leg in ("ENTRY", "EXIT", "PROTECTIVE")
    }
    assert len(tags) == 3


def test_a_replaced_entry_does_not_collide_with_the_one_it_replaced():
    assert KiteBroker.order_tag(TRADE_ID, "ENTRY", 0) != KiteBroker.order_tag(TRADE_ID, "ENTRY", 1)


def test_every_tag_fits_kites_limit():
    for leg in ("ENTRY", "EXIT", "PROTECTIVE"):
        for attempt in range(4):
            assert len(KiteBroker.order_tag(TRADE_ID, leg, attempt)) <= 20
    long_id = "T0005_" + "X" * 80
    assert len(KiteBroker.order_tag(long_id, "EXIT")) <= 20


def test_an_unsupported_leg_is_refused_rather_than_silently_mistagged():
    with pytest.raises(ValueError):
        KiteBroker.order_tag(TRADE_ID, "WHATEVER")


# ── the lookup ───────────────────────────────────────────────────────────────

def _broker_with_book(rows):
    b = KiteBroker.__new__(KiteBroker)
    b._order_book = lambda: rows
    return b


def test_an_unreadable_order_book_is_unknown_not_absent():
    """The invariant that keeps a live position from being duplicated."""
    result = _broker_with_book(None).inspect_order_by_tag("U1X0ABC")
    assert result["state"] == "UNKNOWN"


def test_an_empty_book_that_read_successfully_is_absent():
    assert _broker_with_book([])._order_book() == []
    assert _broker_with_book([]).inspect_order_by_tag("U1X0ABC")["state"] == "ABSENT"


def test_one_exact_match_is_found():
    rows = [{"tag": "U1X0ABC", "orderid": "251003111", "orderstatus": "open"}]
    result = _broker_with_book(rows).inspect_order_by_tag("U1X0ABC")
    assert result["state"] == "FOUND" and result["order_id"] == "251003111"


def test_a_near_miss_tag_does_not_match():
    rows = [{"tag": "U1X0ABCD", "orderid": "251003111"}]
    assert _broker_with_book(rows).inspect_order_by_tag("U1X0ABC")["state"] == "ABSENT"


def test_two_orders_sharing_a_tag_are_ambiguous_not_found():
    rows = [{"tag": "U1X0ABC", "orderid": "1"}, {"tag": "U1X0ABC", "orderid": "2"}]
    assert _broker_with_book(rows).inspect_order_by_tag("U1X0ABC")["state"] == "AMBIGUOUS"


# ── the payoff: a lost flatten acknowledgement ───────────────────────────────

class _Broker:
    def __init__(self, book):
        self._book = book
        self.exits = []

    def order_tag(self, trade_id, leg="ENTRY", attempt=0):
        return KiteBroker.order_tag(trade_id, leg, attempt)

    def inspect_order_by_tag(self, tag):
        return KiteBroker.inspect_order_by_tag(self, tag)

    def _order_book(self):
        return self._book

    def close_order(self, *a, **k):
        self.exits.append(a)
        raise BrokerOrderAmbiguous("read timed out after submit")

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


def _trade():
    t = Trade(
        id=TRADE_ID, instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=200.0, entry_time=datetime.now(IST), trailing_stop=180.0,
        current_stop=180.0, lots=1, lot_size=65, grade="A", inst_type="OPT",
        trading_symbol=SYMBOL, symbol_token="1", broker_order_id="251003000111111",
    )
    t.execution_status = "RECOVERY_REQUIRED"
    t.broker_quantity = 65
    return t


def _flatten(book):
    broker = _Broker(book)
    manager = _manager(broker)
    trade = _trade()
    manager.open_trades[trade.id] = trade
    ok = manager.emergency_flatten_trade(trade, "FORCE_EOD_KILL")
    return manager, trade, ok


def test_a_lost_ack_whose_tag_is_found_adopts_the_order_instead_of_latching():
    exit_tag = KiteBroker.order_tag(TRADE_ID, "EXIT")
    manager, trade, ok = _flatten([{"tag": exit_tag, "orderid": "EXIT-REAL", "orderstatus": "open"}])
    assert ok is True
    assert trade.exit_order_id == "EXIT-REAL"
    assert trade.execution_status == "EXIT_PENDING"
    assert trade.id not in getattr(manager, "_flatten_ambiguous_latched", set()), (
        "froze a trade whose exit the order book could positively identify"
    )


def test_a_lost_ack_whose_tag_is_absent_stays_retryable():
    """A successful read finding nothing proves the exit never arrived."""
    manager, trade, ok = _flatten([])
    assert ok is False
    assert trade.id not in getattr(manager, "_flatten_ambiguous_latched", set()), (
        "latched a trade that provably has no exit at the exchange, stranding it"
    )


def test_an_unreadable_book_still_latches():
    """When the question cannot be answered, freezing remains the right answer."""
    manager, trade, ok = _flatten(None)
    assert ok is False
    assert trade.id in manager._flatten_ambiguous_latched


# ── tagging must never take an order down with it ────────────────────────────

class _UntaggedBroker(_Broker):
    """A broker predating the tag argument -- SignalSimBroker and every test double."""

    def close_order(self, symbol, token, qty, side, product_type="CARRYFORWARD", price=0.0):
        self.exits.append((symbol, qty, side))
        return "EXIT-OK"

    def place_protective_stop(self, symbol, token, qty, side, trigger, product_type="CARRYFORWARD"):
        return "SLM-OK"


def test_a_broker_without_tag_support_still_gets_its_order_placed():
    """An unexpected keyword is a TypeError that aborts the whole order path.

    Tagging only improves RECOVERY, so it has to degrade to nothing rather than take the
    exit down with it -- passing it unconditionally broke 28 existing tests and would
    have broken simulation mode the same way.
    """
    broker = _UntaggedBroker([])
    manager = _manager(broker)
    trade = _trade()
    manager.open_trades[trade.id] = trade

    assert manager.emergency_flatten_trade(trade, "FORCE_EOD_KILL") is True
    assert broker.exits, "the exit never reached a broker that does not accept tags"
    assert trade.exit_order_id == "EXIT-OK"


def test_the_tag_is_omitted_rather_than_passed_as_empty():
    manager = _manager(_UntaggedBroker([]))
    trade = _trade()
    assert manager._tag_kwargs(manager.broker.close_order, trade, "EXIT") == {}


def test_a_tag_aware_broker_does_receive_it():
    manager = _manager(_Broker([]))

    def tagged(symbol, token, qty, side, product_type="CARRYFORWARD", price=0.0, tag=""):
        return "X"

    kwargs = manager._tag_kwargs(tagged, _trade(), "EXIT")
    assert kwargs == {"tag": KiteBroker.order_tag(TRADE_ID, "EXIT")}


def test_a_broker_taking_kwargs_is_treated_as_supporting_the_tag():
    manager = _manager(_Broker([]))
    assert manager._tag_kwargs(lambda *a, **k: None, _trade(), "EXIT")["tag"]
