"""A resting Kite entry must remain owned across the entry-tag format upgrade."""

import threading
from datetime import datetime

from trading.kite_broker import KiteBroker
from trading.trade_manager import IST, Trade, TradeManager


TRADE_ID = "T0005_NIFTY_07311451"
SYMBOL = "NIFTY26AUG24000CE"


class _EmptyAckApi:
    def __init__(self, book):
        self.book = list(book)
        self.sent = None

    def place_order(self, **params):
        self.sent = params
        return ""

    def orders(self):
        return list(self.book)


class _NoPlacementApi(_EmptyAckApi):
    """Recovery must never submit a second order just to discover the first one."""

    def place_order(self, **params):
        raise AssertionError("restart recovery attempted a new entry placement")


def test_legacy_attempt_zero_tag_is_an_owned_reconciliation_alias_only():
    current = KiteBroker.order_tag(TRADE_ID, "ENTRY", 0)
    legacy = "T0005_07311451"
    assert current == "T000507311451"
    assert current.isalnum()
    assert legacy in KiteBroker.__new__(KiteBroker)._own_entry_tags(TRADE_ID)

    api = _EmptyAckApi([
        {
            "order_id": "LEGACY-ENTRY",
            "tradingsymbol": SYMBOL,
            "transaction_type": "BUY",
            "quantity": 75,
            "filled_quantity": 75,
            "status": "COMPLETE",
            "order_type": "MARKET",
            "product": "NRML",
            "tag": legacy,
        }
    ])
    broker = KiteBroker(kite=api, account_id="", instance_id="legacy-tag-upgrade-test")
    broker.set_execution_authority(lambda: True)

    recovered = broker.place_order(
        SYMBOL,
        "12345",
        75,
        "BUY",
        "CARRYFORWARD",
        price=0.0,
        trade_id=TRADE_ID,
        confidence=0.9,
    )

    assert api.sent["tag"] == current, "new placements must keep the alphanumeric tag"
    assert recovered == "LEGACY-ENTRY", "the legacy broker row must remain owned"


def _recovery_manager(broker):
    manager = TradeManager.__new__(TradeManager)
    manager.broker = broker
    manager.ui_logger = None
    manager.mode = "REAL"
    manager.product_type = "CARRYFORWARD"
    manager.open_trades = {}
    manager.closed_trades = []
    manager.orphan_positions = {}
    manager._lock = threading.RLock()
    manager._protection_lock = threading.RLock()
    manager.save_state = lambda: None
    manager._last_protective_sync = {}
    manager._last_protective_status_poll = {}
    manager._last_recovery_alert = {}
    manager._last_mark_to_market_save = 0.0
    manager.max_positions = 4
    manager.futures_sl_pct = 0.3
    manager.options_sl_pct = 15.0
    manager._log_ui = lambda *args, **kwargs: None
    manager.on_notification = None
    # This test isolates order-id recovery; expiry settlement is outside its scope.
    manager._settle_expired_contract = lambda *args, **kwargs: False
    return manager


def test_restart_recovers_legacy_pending_entry_before_any_new_placement():
    legacy = "T0005_07311451"
    api = _NoPlacementApi([
        {
            "order_id": "LEGACY-ENTRY",
            "tradingsymbol": SYMBOL,
            "transaction_type": "BUY",
            "quantity": 75,
            "filled_quantity": 0,
            "status": "OPEN",
            "order_type": "LIMIT",
            "product": "NRML",
            "tag": legacy,
        }
    ])
    broker = KiteBroker(kite=api, account_id="", instance_id="legacy-tag-restart-test")
    manager = _recovery_manager(broker)
    trade = Trade(
        id=TRADE_ID,
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=200.0,
        entry_time=datetime.now(IST),
        trailing_stop=180.0,
        current_stop=180.0,
        lots=1,
        lot_size=75,
        grade="A",
        inst_type="OPT",
        trading_symbol=SYMBOL,
        symbol_token="12345",
    )
    trade.pending_execution = True
    trade.execution_status = "RECOVERY_REQUIRED"
    manager.open_trades[trade.id] = trade

    manager.reconcile_broker_positions({"data": []})

    assert trade.broker_order_id == "LEGACY-ENTRY"
    assert trade.execution_status == "ENTRY_SUBMITTED"
    assert api.sent is None, "recovery must not place a replacement entry"


def test_current_and_legacy_entry_aliases_are_ambiguous_not_adopted():
    broker = KiteBroker.__new__(KiteBroker)
    broker._order_book = lambda: [
        {"tag": KiteBroker.order_tag(TRADE_ID), "orderid": "CURRENT"},
        {"tag": "T0005_07311451", "orderid": "LEGACY"},
    ]

    result = broker.inspect_entry_order_for_trade(TRADE_ID)

    assert result["state"] == "AMBIGUOUS"
