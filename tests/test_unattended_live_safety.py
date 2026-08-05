from collections import deque
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import threading
import time

import pytest

from config.settings import get_settings
import scanner as scanner_module
from scanner import Scanner
from trading.kite_broker import KiteBroker, OrderException
from trading.order_retry import BrokerOrderRejected
from trading.trade_manager import IST, Trade, TradeManager


class BrokerApi:
    def __init__(self):
        self.placed = []
        self.modified = []
        self.cancelled = []
        self.order_book = []

    def place_order(self, **params):
        self.placed.append(dict(params))
        return f"O{len(self.placed)}"

    def modify_order(self, **params):
        self.modified.append(dict(params))
        return params.get("order_id")

    def cancel_order(self, **params):
        self.cancelled.append(params)
        return params.get("order_id")

    def orders(self):
        return list(self.order_book)

    def positions(self):
        return {"net": []}


def test_broker_places_and_tightens_slm_and_rate_limits_entries():
    settings = get_settings()
    old_limit = settings.max_entry_orders_per_minute
    old_latch = settings.entry_order_kill_latch_seconds
    try:
        settings.max_entry_orders_per_minute = 2
        settings.entry_order_kill_latch_seconds = 10
        api = BrokerApi()
        broker = KiteBroker(kite=api)
        broker._min_call_interval = 0.001

        stop_id = broker.place_protective_stop(
            "NIFTYTESTFUT", "1", 10, "SELL", 95.03
        )
        assert stop_id == "O1"
        assert api.placed[-1]["order_type"] == "SL-M"
        # The exchange rejects an off-tick trigger outright, so 95.03 must round.
        assert float(api.placed[-1]["trigger_price"]) == 95.05
        assert broker.modify_protective_stop(
            stop_id, "NIFTYTESTFUT", "1", 10, "SELL", 96.0
        )
        assert float(api.modified[-1]["trigger_price"]) == 96.0

        assert broker.place_order("NIFTYTESTFUT", "1", 10, "BUY")
        assert broker.place_order("BANKNIFTYTESTFUT", "2", 10, "BUY")
        # Third entry inside the window latches the kill-switch. Without this the
        # only bound on live entries is however fast the signal loop can call.
        assert broker.place_order("SENSEXTESTFUT", "3", 10, "BUY") is None
        # Risk-reducing exits are intentionally never blocked by the entry latch.
        assert broker.close_order("NIFTYTESTFUT", "1", 10, "SELL")
        # Nor are protective stops: a latched runaway must not also go unprotected.
        assert broker.place_protective_stop("NIFTYTESTFUT", "1", 10, "SELL", 94.0)
    finally:
        settings.max_entry_orders_per_minute = old_limit
        settings.entry_order_kill_latch_seconds = old_latch


class _SlowDeque(deque):
    """Widens the gap between the size check and the record, so the race is forced.

    Threads racing on a real deque interleave only if the GIL happens to switch inside
    the ~5 bytecodes between `len(...)` and `append(...)`, which it effectively never
    does — a test built on hope passes with or without the lock and pins nothing.

    The delay has to sit at the START of append, not in __len__: sleeping before a read
    just returns a fresher value and every thread still sees the appends that landed
    while it waited. Holding threads inside append is what puts them all past the size
    check at once, which is exactly the window the lock closes.
    """

    def append(self, item):
        time.sleep(0.05)
        super().append(item)


def test_entry_kill_switch_holds_under_concurrent_entries():
    """Entries are placed from one background thread per trade.

    Unsynchronised, several threads each read "still under the cap", all pass, and the
    cap the kill-switch exists to enforce is overshot — live orders, not a counter.
    """
    settings = get_settings()
    old_limit = settings.max_entry_orders_per_minute
    old_latch = settings.entry_order_kill_latch_seconds
    try:
        settings.max_entry_orders_per_minute = 3
        settings.entry_order_kill_latch_seconds = 10
        api = BrokerApi()
        broker = KiteBroker(kite=api)
        broker._min_call_interval = 0.0
        broker._entry_order_times = _SlowDeque()

        start = threading.Barrier(12)
        results = []
        lock = threading.Lock()

        def fire(n):
            start.wait()
            got = broker.place_order(f"NIFTYTESTFUT{n}", str(n), 10, "BUY")
            with lock:
                results.append(got)

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        placed = [r for r in results if r]
        assert len(placed) == 3, f"kill-switch let {len(placed)} entries through a cap of 3"
        assert len(api.placed) == 3, "more orders reached the broker than the cap allows"
    finally:
        settings.max_entry_orders_per_minute = old_limit
        settings.entry_order_kill_latch_seconds = old_latch


def test_a_refused_entry_does_not_spend_the_runaway_budget():
    """Entries retry ~7x over 36s, all inside the 60s window.

    Counting attempts rather than placements means one RMS refusal on ONE trade eats
    the whole default budget of 6 and latches every entry off for 300s, without a
    single order having reached the exchange.
    """
    settings = get_settings()
    old_limit = settings.max_entry_orders_per_minute
    try:
        settings.max_entry_orders_per_minute = 3

        class _Refusing:
            def __init__(self):
                self.calls = 0

            def place_order(self, **_kw):
                self.calls += 1
                raise OrderException("RMS:Margin exceeds available")

            def orders(self):
                return []

        api = _Refusing()
        broker = KiteBroker(kite=api)
        broker._min_call_interval = 0.0

        for _ in range(6):
            with pytest.raises(BrokerOrderRejected):
                broker.place_order("NIFTYTESTFUT", "1", 10, "BUY")

        # None of those reached the exchange, so a good order must still go through.
        ok = KiteBroker(kite=BrokerApi())
        ok._min_call_interval = 0.0
        ok._entry_order_times = broker._entry_order_times
        ok._entry_rate_lock = broker._entry_rate_lock
        ok._entry_kill_latched_until = broker._entry_kill_latched_until
        assert ok.place_order("NIFTYTESTFUT", "1", 10, "BUY"), (
            "six refusals that never reached the exchange latched the kill-switch"
        )
    finally:
        settings.max_entry_orders_per_minute = old_limit


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
    # The retired broker's SDK was removed on 2026-08-05. It was pinned solely so a dead
    # import in data/market_data.py would resolve; that import constructed nothing and is
    # gone, so the pin would now install a package no code can reach.
    assert "smartapi-python" not in requirements
    assert "kiteconnect==5.2.1" in requirements
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


def test_concurrent_reservation_releases_do_not_over_release():
    """_release_entry_reservation is check-then-mutate on a shared deque.

    Unsynchronised, two threads releasing when one slot remains both pass the check and
    the second pop() raises IndexError — which would escape place_order's except-block in
    place of the classified BrokerOrderRejected/BrokerOrderAmbiguous the caller depends
    on. It must also never give back more slots than were taken: an over-release makes
    the kill-switch count fewer orders than are actually live.
    """
    broker = KiteBroker(kite=BrokerApi())
    broker._min_call_interval = 0.0
    broker._entry_order_times = _SlowDeque([1.0, 2.0])   # exactly two slots held

    errors = []
    start = threading.Barrier(8)

    def release():
        start.wait()
        try:
            broker._release_entry_reservation()
        except BaseException as exc:      # noqa: BLE001 - the point is to catch anything
            errors.append(exc)

    threads = [threading.Thread(target=release) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"release raised instead of degrading safely: {errors}"
    assert len(broker._entry_order_times) == 0
