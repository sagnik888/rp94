"""Regression proof for the September live-resilience audit.

These checks pin the safety boundaries rather than trying to simulate a broker: a
failed request must not become fresh data, and an ambiguous exit must be reconciled by
its order tag before another MARKET exit is allowed.
"""
import inspect
import time
from datetime import datetime
from types import SimpleNamespace

from scanner import Scanner
from trading.kite_broker import KiteBroker
from trading.trade_manager import IST, Trade, TradeManager


def _future_trade():
    return SimpleNamespace(symbol_token="123", trading_symbol="NIFTY26SEPFUT")


def test_failed_futures_refresh_never_returns_an_expired_cached_mark():
    scanner = Scanner.__new__(Scanner)
    scanner._candidate_ltp_cache = {
        "FUT:123": {"price": 25000.0, "time": time.time() - 3600.0}
    }
    scanner.data = SimpleNamespace(
        get_ltp=lambda *args: (_ for _ in ()).throw(TimeoutError("broker stalled"))
    )

    assert scanner._get_live_fut_ltp_cached(_future_trade()) is None


def test_fast_trade_path_does_not_call_a_futures_quote_service():
    scanner = Scanner.__new__(Scanner)
    scanner._candidate_ltp_cache = {}
    calls = []
    scanner.data = SimpleNamespace(get_ltp=lambda *args: calls.append(args) or 25000.0)

    assert scanner._get_live_fut_ltp_cached(_future_trade(), allow_blocking=False) is None
    assert calls == []


def test_entry_gate_uses_only_a_nonblocking_cached_funds_refusal():
    source = inspect.getsource(TradeManager.open_trade)
    early = source[source.index("Non-blocking early funds refusal"):source.index("known_ids =")]
    assert "cached_entry_margin_refusal" in early
    assert "_entry_margin_verdict(" not in early


def test_scanner_heartbeat_defers_broker_protection_to_position_sync():
    source = inspect.getsource(TradeManager.update_trade)
    protection = source[source.index("if int(getattr(trade, \"broker_quantity\""):]
    assert "allow_broker_protection" in protection
    assert "self.nudge_position_sync()" in protection


def test_silent_feed_retry_waits_for_health_to_clear_not_one_successful_write():
    import main

    source = inspect.getsource(main.start_runtime_watchdog)
    assert "kite_stream_resubscribe_attempt_at" in source
    assert "now_monotonic - last_attempt >= retry_every" in source


def test_failed_intelligence_cannot_restamp_an_old_snapshot_as_fresh():
    source = inspect.getsource(Scanner._process_instrument)
    assert "intel_computed_this_pass = False" in source
    assert "and intel_computed_this_pass" in source
    assert "at=(cached_intel_time if intel_is_fresh else now)" not in source
    failure = source[source.index("except Exception as e:", source.index("intel_computed_this_pass")):]
    assert failure.index("intel_computed_this_pass = False") < failure.index("intel_result = cached_intel")


def test_held_own_timeframe_signal_is_registered_for_reconsideration():
    from engine.signal_processor import SignalProcessor

    source = inspect.getsource(SignalProcessor.process_best_signal)
    hold = source.index("own-timeframe intelligence; fail-closed grading is active.")
    retry = source.index("_live_signal_recheck_eligible", hold)
    remember = source.index("_remember_live_signal_identity", hold)
    assert retry < remember, "a held signal is remembered without a retry registration"
    assert "_live_signal_processing_deadline" in source[hold:remember]


class _ExitBroker:
    def __init__(self, book, lifecycle=None):
        self.book = book
        self.exits = []
        self.lifecycle = lifecycle

    def order_tag(self, trade_id, leg="ENTRY", attempt=0):
        return KiteBroker.order_tag(trade_id, leg, attempt)

    def inspect_order_by_tag(self, tag):
        matches = [row for row in self.book if row.get("tag") == tag]
        if not matches:
            return {"state": "ABSENT"}
        if len(matches) > 1:
            return {"state": "AMBIGUOUS"}
        return {"state": "FOUND", "order_id": matches[0]["orderid"]}

    def close_order(self, *args, **kwargs):
        self.exits.append((args, kwargs))
        return "RECOVERY-EXIT-1"

    def cancel_order(self, *args, **kwargs):
        return True

    def inspect_working_protective_stop(self, *args, **kwargs):
        return {"state": "NONE", "order_id": "", "reason": "none", "matches": []}

    def find_working_protective_stop(self, *args, **kwargs):
        return ""

    def get_fill(self, *args, **kwargs):
        return {"state": "UNKNOWN"}

    def get_order_lifecycle(self, *_args, **_kwargs):
        return {"state": self.lifecycle or "UNKNOWN"}


def _manager_and_trade(broker):
    manager = TradeManager.__new__(TradeManager)
    manager.broker = broker
    manager.mode = "REAL"
    manager.product_type = "CARRYFORWARD"
    manager.open_trades = {}
    manager.closed_trades = []
    manager.orphan_positions = {}
    import threading
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
    trade = Trade(
        id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=100.0, entry_time=datetime.now(IST), trailing_stop=90.0,
        current_stop=90.0, lots=1, lot_size=50, grade="A", inst_type="OPT",
        trading_symbol="NIFTY26SEP25000CE", symbol_token="123", broker_order_id="ENTRY-1",
    )
    trade.pending_exit = True
    trade.execution_status = "RECOVERY_REQUIRED"
    trade.broker_quantity = 50
    manager.open_trades[trade.id] = trade
    return manager, trade


def _live_position():
    return {"data": [{
        "tradingsymbol": "NIFTY26SEP25000CE", "netqty": "50",
        "buyavgprice": "100", "sellavgprice": "0", "realised": "0",
    }]}


def test_ambiguous_exit_found_by_tag_is_adopted_without_a_duplicate_exit():
    exit_tag = KiteBroker.order_tag("T1", "FULL_EXIT")
    manager, trade = _manager_and_trade(_ExitBroker([
        {"tag": exit_tag, "orderid": "EXIT-EXISTS"}
    ]))

    manager.reconcile_broker_positions(_live_position())

    assert trade.execution_status == "EXIT_PENDING"
    assert trade.exit_order_id == "EXIT-EXISTS"
    assert manager.broker.exits == []


def test_ambiguous_exit_is_retried_only_after_tag_is_authoritatively_absent():
    manager, trade = _manager_and_trade(_ExitBroker([]))

    manager.reconcile_broker_positions(_live_position())

    assert len(manager.broker.exits) == 1
    assert trade.execution_status == "EXIT_PENDING"
    assert trade.exit_order_id == "RECOVERY-EXIT-1"


def test_confirmed_dead_full_exit_is_retried_after_position_reconciliation():
    manager, trade = _manager_and_trade(_ExitBroker([], lifecycle="CANCELLED"))
    trade.execution_status = "EXIT_PENDING"
    trade.exit_order_id = "EXIT-CANCELLED"

    manager.reconcile_broker_positions(_live_position())

    assert len(manager.broker.exits) == 1
    assert trade.execution_status == "EXIT_PENDING"
    assert trade.exit_order_id == "RECOVERY-EXIT-1"
