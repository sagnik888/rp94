from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import threading

from config.settings import get_settings
import scanner as scanner_module
from scanner import Scanner
from trading.broker import SmartApiBroker
from trading.trade_manager import IST, Trade, TradeManager


class BrokerApi:
    def __init__(self):
        self.placed = []
        self.modified = []
        self.cancelled = []
        self.orders = []

    def placeOrderFullResponse(self, params):
        self.placed.append(dict(params))
        return {"status": True, "data": {"orderid": f"O{len(self.placed)}"}}

    def placeOrder(self, params):
        return self.placeOrderFullResponse(params)

    def modifyOrder(self, params):
        self.modified.append(dict(params))
        return {"status": True}

    def cancelOrder(self, order_id, variety):
        self.cancelled.append((order_id, variety))
        return {"status": True}

    def orderBook(self):
        return {"status": True, "data": list(self.orders)}

    def position(self):
        return {"status": True, "data": []}


def test_broker_places_and_tightens_slm_and_rate_limits_entries():
    settings = get_settings()
    old_limit = settings.max_entry_orders_per_minute
    old_latch = settings.entry_order_kill_latch_seconds
    try:
        settings.max_entry_orders_per_minute = 2
        settings.entry_order_kill_latch_seconds = 10
        api = BrokerApi()
        broker = SmartApiBroker(api)
        broker._api_limiter.min_interval_seconds = 0.001

        stop_id = broker.place_protective_stop(
            "NIFTYTESTFUT", "1", 10, "SELL", 95.03
        )
        assert stop_id == "O1"
        assert api.placed[-1]["variety"] == "STOPLOSS"
        assert api.placed[-1]["ordertype"] == "STOPLOSS_MARKET"
        assert float(api.placed[-1]["triggerprice"]) == 95.05
        assert broker.modify_protective_stop(
            stop_id, "NIFTYTESTFUT", "1", 10, "SELL", 96.0
        )
        assert float(api.modified[-1]["triggerprice"]) == 96.0

        assert broker.place_order("NIFTYTESTFUT", "1", 10, "BUY")
        assert broker.place_order("BANKNIFTYTESTFUT", "2", 10, "BUY")
        assert broker.place_order("SENSEXTESTFUT", "3", 10, "BUY") is None
        # Risk-reducing exits are intentionally never blocked by the entry latch.
        assert broker.close_order("NIFTYTESTFUT", "1", 10, "SELL")
    finally:
        settings.max_entry_orders_per_minute = old_limit
        settings.entry_order_kill_latch_seconds = old_latch


class LifecycleBroker:
    def __init__(self, protection_ok=True):
        self.protection_ok = protection_ok
        self.protective = []
        self.modified = []
        self.cancelled = []
        self.exits = []
        self.entries = []
        self.order_status = None

    def get_positions(self):
        return {"data": []}

    def get_order_status(self, order_id):
        return self.order_status

    def cancel_order(self, order_id, variety="NORMAL"):
        self.cancelled.append((order_id, variety))
        return True

    def place_order(self, symbol, token, qty, side, product_type, price=0.0):
        self.entries.append((symbol, qty, side, price))
        return "REPLACEMENT"

    def close_order(self, symbol, token, qty, side, product_type, price=0.0):
        self.exits.append((symbol, qty, side))
        return "EXIT1"

    def place_protective_stop(self, symbol, token, qty, side, trigger_price, product_type):
        self.protective.append((symbol, qty, side, trigger_price))
        return "SL1" if self.protection_ok else None

    def modify_protective_stop(
        self, order_id, symbol, token, qty, side, trigger_price, product_type
    ):
        self.modified.append((order_id, trigger_price))
        return True


def make_manager(broker):
    manager = TradeManager.__new__(TradeManager)
    manager.broker = broker
    manager.ui_logger = None
    manager.max_positions = 4
    manager.product_type = "CARRYFORWARD"
    manager.open_trades = {}
    manager.closed_trades = []
    manager.orphan_positions = {}
    manager._lock = threading.RLock()
    manager._last_mark_to_market_save = 0.0
    manager._last_protective_sync = {}
    manager._last_protective_status_poll = {}
    manager._last_recovery_alert = {}
    manager.futures_sl_pct = 0.3
    manager.options_sl_pct = 15.0
    manager.mode = "REAL"
    manager.save_state = lambda: None
    return manager


def make_trade():
    return Trade(
        id="T1",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime.now(IST),
        trailing_stop=95.0,
        current_stop=95.0,
        lots=1,
        lot_size=10,
        grade="A",
        trading_symbol="NIFTYTESTFUT",
        symbol_token="1",
        inst_type="FUT",
        current_price=100.0,
        broker_order_id="ENTRY1",
        entry_submitted_at=datetime.now(IST),
        pending_execution=True,
        execution_status="ENTRY_SUBMITTED",
    )


def position_row(qty=10):
    return {
        "tradingsymbol": "NIFTYTESTFUT",
        "netqty": str(qty),
        "buyavgprice": "100",
        "sellavgprice": "0",
    }


def test_fill_confirmation_arms_protection_and_trailing_tightens_it():
    broker = LifecycleBroker()
    manager = make_manager(broker)
    trade = make_trade()
    manager.open_trades[trade.id] = trade

    result = manager.reconcile_broker_positions({"data": [position_row()]})
    assert result["confirmed"] == 1
    assert trade.execution_status == "OPEN"
    assert trade.protective_order_id == "SL1"
    assert broker.protective[-1][-1] == 95.0

    manager._last_protective_sync[trade.id] = 0.0
    manager.update_trade(
        trade.id,
        current_spot=102.0,
        new_trailing_stop=97.0,
        real_fut_price=102.0,
    )
    assert broker.modified[-1] == ("SL1", 97.0)


def test_unprotected_recovery_position_auto_flattens_after_grace():
    settings = get_settings()
    old_grace = settings.recovery_auto_flatten_seconds
    try:
        settings.recovery_auto_flatten_seconds = 5.0
        broker = LifecycleBroker(protection_ok=False)
        manager = make_manager(broker)
        trade = make_trade()
        trade.execution_status = "RECOVERY_REQUIRED"
        trade.recovery_required_since = datetime.now(IST) - timedelta(seconds=10)
        manager.open_trades[trade.id] = trade

        manager.reconcile_broker_positions({"data": [position_row()]})
        assert broker.exits == [("NIFTYTESTFUT", 10, "SELL")]
        assert trade.execution_status == "EXIT_PENDING"
        assert trade.exit_reason == "RECOVERY_AUTO_FLATTEN"
    finally:
        settings.recovery_auto_flatten_seconds = old_grace


def test_unprotected_recovery_position_auto_flattens_from_mark_to_market():
    settings = get_settings()
    old_grace = settings.recovery_auto_flatten_seconds
    try:
        settings.recovery_auto_flatten_seconds = 5.0
        broker = LifecycleBroker(protection_ok=False)
        manager = make_manager(broker)
        trade = make_trade()
        trade.execution_status = "RECOVERY_REQUIRED"
        trade.pending_execution = False
        trade.broker_quantity = trade.quantity
        trade.recovery_required_since = datetime.now(IST) - timedelta(seconds=10)
        manager.open_trades[trade.id] = trade

        manager.update_trade(
            trade.id,
            current_spot=102.0,
            real_fut_price=102.0,
        )

        assert broker.exits == [("NIFTYTESTFUT", 10, "SELL")]
        assert trade.execution_status == "EXIT_PENDING"
        assert trade.exit_reason == "RECOVERY_AUTO_FLATTEN"
    finally:
        settings.recovery_auto_flatten_seconds = old_grace


def test_stale_working_entry_is_cancelled_and_replaced_once():
    settings = get_settings()
    old_timeout = settings.broker_fill_confirmation_timeout_seconds
    try:
        settings.broker_fill_confirmation_timeout_seconds = 2.0
        broker = LifecycleBroker()
        broker.order_status = {"orderid": "ENTRY1", "orderstatus": "open"}
        manager = make_manager(broker)
        trade = make_trade()
        trade.entry_submitted_at = datetime.now(IST) - timedelta(seconds=5)
        manager.open_trades[trade.id] = trade

        manager.reconcile_broker_positions({"data": []})
        assert broker.cancelled == [("ENTRY1", "NORMAL")]
        assert broker.entries == [("NIFTYTESTFUT", 10, "BUY", 0.0)]
        assert trade.broker_order_id == "REPLACEMENT"
        assert trade.entry_replace_count == 1
    finally:
        settings.broker_fill_confirmation_timeout_seconds = old_timeout


def test_live_clock_skew_blocks_entries_and_requirements_are_reproducible(monkeypatch):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    now = IST.localize(datetime(2026, 6, 22, 10, 0))

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(scanner_module, "datetime", FixedDateTime)
    scanner.candles = type("Candles", (), {"get_max_timestamp": lambda self: now - timedelta(seconds=600)})()
    scanner.log_signal_decision_once = lambda *args, **kwargs: None
    assert scanner._live_clock_is_sane() is False
    scanner.candles = type("Candles", (), {"get_max_timestamp": lambda self: now - timedelta(seconds=30)})()
    assert scanner._live_clock_is_sane() is True

    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    assert "\x00" not in requirements
    assert ">=" not in requirements
    assert "smartapi-python==1.5.5" in requirements
    assert Path(".python-version").read_text(encoding="utf-8").strip() == "3.13.9"


def test_quarantined_tests_are_explicit_and_fixed_at_twelve():
    path = Path("tests/conftest.py")
    spec = importlib.util.spec_from_file_location("suite_conftest_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(module._QUARANTINED) == 12
    assert all("rolled-back feature" in reason or "superseded" in reason for reason in module._QUARANTINED.values())


def test_trade_loss_cap_is_clamped_to_segment_daily_budget():
    """Contract: with the rupee backstop DISABLED (0, the default) the cap is
    the segment's own daily budget — the user's visible settings govern.
    When a backstop is explicitly set (>0) it still clamps below the budget."""
    settings = get_settings()
    fut_budget = settings.capital_fut * settings.risk_fut_pct / 100.0
    opt_budget = settings.capital_opt * settings.risk_opt_pct / 100.0
    abs_cap = float(settings.max_trade_loss_abs or 0.0)
    expected_fut = fut_budget if abs_cap <= 0 else min(abs_cap, fut_budget)
    expected_opt = opt_budget if abs_cap <= 0 else min(abs_cap, opt_budget)
    assert settings.effective_trade_loss_cap("FUT") == expected_fut
    assert settings.effective_trade_loss_cap("OPT") == expected_opt
