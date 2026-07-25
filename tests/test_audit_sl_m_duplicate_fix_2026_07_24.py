"""
Audit fix verification (2026-07-24): exchange-side SL-M duplicate / naked-leg fixes.

Reference: reports/AUTO_MODE_READINESS_AUDIT_2026-07-24.md

- BLOCKER-1: SmartApiBroker.place_protective_stop now signals ambiguity (raises
  BrokerOrderAmbiguous) on a lost/empty acknowledgement instead of returning None.
  A None return was indistinguishable from a clean reject, so the caller re-placed
  and TWO SL-M orders rested at the exchange; when both triggered, the second opened
  a naked reversed leg.
- BLOCKER-2: TradeManager.ensure_broker_protection serialises the read-decide-place
  window (dedicated _protection_lock) and is idempotent (adopts an existing working
  stop via broker.find_working_protective_stop), so two concurrent callers, or a
  lost acknowledgement, can never create a duplicate SL-M for one fill.
- HIGH-2: enabling Auto Mode while already in REAL now re-runs
  validate_real_mode_readiness and blocks when the broker/feed is not ready.
"""
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from trading.broker import Broker, SmartApiBroker
from trading.order_retry import BrokerOrderAmbiguous
from trading.trade_manager import IST, Trade, TradeManager
from scanner import Scanner


# ───────────────────────── BLOCKER-1: broker ambiguity signalling ─────────────────────────

class FakeApi:
    def __init__(self, response=None, raises=None, order_book=None):
        self.response = response
        self.raises = raises
        self.order_book = order_book if order_book is not None else {"data": []}
        self.place_calls = 0

    def placeOrder(self, params):
        self.place_calls += 1
        if self.raises is not None:
            raise self.raises
        return self.response

    def orderBook(self):
        return self.order_book


def _smart_broker(**kw):
    return SmartApiBroker(FakeApi(**kw))


def test_place_protective_stop_raises_ambiguous_on_api_exception():
    # A STOPLOSS_MARKET can reach/accept at the exchange while the HTTP ack is lost.
    broker = _smart_broker(raises=TimeoutError("read timed out after submit"))
    with pytest.raises(BrokerOrderAmbiguous):
        broker.place_protective_stop("NIFTYTESTFUT", "123", 10, "SELL", 95.0, "CARRYFORWARD")


def test_place_protective_stop_raises_ambiguous_on_missing_order_id():
    broker = _smart_broker(response={"status": True, "data": {"message": "accepted"}})
    with pytest.raises(BrokerOrderAmbiguous):
        broker.place_protective_stop("NIFTYTESTFUT", "123", 10, "SELL", 95.0, "CARRYFORWARD")


def test_place_protective_stop_returns_none_on_clean_reject():
    broker = _smart_broker(response={"status": False, "errorcode": "AB1004", "message": "rejected"})
    assert broker.place_protective_stop("NIFTYTESTFUT", "123", 10, "SELL", 95.0, "CARRYFORWARD") is None


def test_place_protective_stop_returns_id_on_success():
    broker = _smart_broker(response={"status": True, "data": {"orderid": "SLM-1"}})
    assert broker.place_protective_stop("NIFTYTESTFUT", "123", 10, "SELL", 95.0, "CARRYFORWARD") == "SLM-1"


# ───────────────── find_working_protective_stop (idempotency order-book scan) ─────────────────

def test_find_working_protective_stop_matches_working_sl():
    rows = [{
        "tradingsymbol": "NIFTYTESTFUT", "symboltoken": "123",
        "variety": "STOPLOSS", "ordertype": "STOPLOSS_MARKET",
        "transactiontype": "SELL", "orderstatus": "trigger pending",
        "orderid": "SLM-99",
    }]
    broker = _smart_broker(order_book={"data": rows})
    assert broker.find_working_protective_stop("NIFTYTESTFUT", "123", "SELL") == "SLM-99"


def test_find_working_protective_stop_ignores_terminal_wrong_symbol_side_and_type():
    rows = [
        {"tradingsymbol": "NIFTYTESTFUT", "variety": "STOPLOSS", "transactiontype": "SELL",
         "orderstatus": "cancelled", "orderid": "DEAD"},
        {"tradingsymbol": "OTHERFUT", "variety": "STOPLOSS", "transactiontype": "SELL",
         "orderstatus": "open", "orderid": "WRONGSYM"},
        {"tradingsymbol": "NIFTYTESTFUT", "variety": "STOPLOSS", "transactiontype": "BUY",
         "orderstatus": "open", "orderid": "WRONGSIDE"},
        {"tradingsymbol": "NIFTYTESTFUT", "variety": "NORMAL", "ordertype": "MARKET",
         "transactiontype": "SELL", "orderstatus": "open", "orderid": "NOTSTOP"},
    ]
    broker = _smart_broker(order_book={"data": rows})
    assert broker.find_working_protective_stop("NIFTYTESTFUT", "", "SELL") == ""


def test_find_working_protective_stop_scan_failure_returns_empty_not_raise():
    broker = _smart_broker(order_book=None)
    broker.api.orderBook = lambda: (_ for _ in ()).throw(RuntimeError("orderBook down"))
    assert broker.find_working_protective_stop("NIFTYTESTFUT", "123", "SELL") == ""


def test_base_broker_find_working_protective_stop_defaults_empty():
    assert Broker().find_working_protective_stop("NIFTYTESTFUT", "123", "SELL") == ""


# ───────────────── BLOCKER-2: ensure_broker_protection is serialised + idempotent ─────────────────

class StopManagerBroker:
    """Scriptable protective-stop broker double."""

    def __init__(self, *, ambiguous=False, reaches_exchange=True, preexisting="", place_delay=0.0):
        self.ambiguous = ambiguous
        self.reaches_exchange = reaches_exchange
        self.preexisting = preexisting
        self.place_delay = place_delay
        self.place_calls = 0
        self.modify_calls = 0
        self._placed_id = ""
        self._lock = threading.Lock()

    def find_working_protective_stop(self, symbol, token, side):
        with self._lock:
            return self.preexisting or self._placed_id

    def place_protective_stop(self, symbol, token, qty, side, trigger_price, product_type):
        if self.place_delay:
            time.sleep(self.place_delay)
        with self._lock:
            self.place_calls += 1
            if self.reaches_exchange:
                self._placed_id = f"SLM-{symbol}"
            placed = self._placed_id
        if self.ambiguous:
            raise BrokerOrderAmbiguous("lost ack after protective stop submit")
        return placed

    def modify_protective_stop(self, *a, **k):
        with self._lock:
            self.modify_calls += 1
        return True

    def get_order_status(self, order_id):
        return None


def _protection_manager(broker):
    manager = TradeManager.__new__(TradeManager)
    manager.broker = broker
    manager.ui_logger = None
    manager.mode = "REAL"
    manager.product_type = "CARRYFORWARD"
    manager.futures_sl_pct = 0.3
    manager.options_sl_pct = 15.0
    manager._lock = threading.RLock()
    manager._protection_lock = threading.RLock()
    manager.save_state = lambda: None
    manager._last_protective_sync = {}
    manager._last_protective_status_poll = {}
    manager._last_recovery_alert = {}
    return manager


def _fut_trade(**over):
    fields = dict(
        id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=100.0, entry_time=IST.localize(datetime(2026, 6, 15, 10, 0)),
        trailing_stop=95.0, current_stop=95.0, lots=1, lot_size=10, grade="A",
        inst_type="FUT", trading_symbol="NIFTYTESTFUT", symbol_token="TOK",
        broker_quantity=10,
    )
    fields.update(over)
    return Trade(**fields)


def test_ensure_broker_protection_adopts_preexisting_stop_without_placing():
    broker = StopManagerBroker(preexisting="SLM-PRE")
    manager = _protection_manager(broker)
    trade = _fut_trade()

    assert manager.ensure_broker_protection(trade, 10) is True
    assert broker.place_calls == 0
    assert trade.protective_order_id == "SLM-PRE"


def test_ensure_broker_protection_places_single_slm_under_concurrency():
    # The core BLOCKER-2 case: PositionSync reconcile + scanner tick both drive
    # protection for the same fresh fill; without serialisation both would place.
    broker = StopManagerBroker(place_delay=0.05)
    manager = _protection_manager(broker)
    trade = _fut_trade()

    results = []
    barrier = threading.Barrier(2)

    def _arm():
        barrier.wait()
        results.append(manager.ensure_broker_protection(trade, 10))

    threads = [threading.Thread(target=_arm) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [True, True]
    assert broker.place_calls == 1  # never a duplicate exchange-side SL-M
    assert trade.protective_order_id == "SLM-NIFTYTESTFUT"


def test_ensure_broker_protection_recovers_ambiguous_via_rescan():
    # Ack lost but the SL-M reached the exchange → adopt it, do not re-place.
    broker = StopManagerBroker(ambiguous=True, reaches_exchange=True)
    manager = _protection_manager(broker)
    trade = _fut_trade()

    assert manager.ensure_broker_protection(trade, 10) is True
    assert broker.place_calls == 1
    assert trade.protective_order_id == "SLM-NIFTYTESTFUT"
    assert trade.execution_status != "RECOVERY_REQUIRED"


def test_ensure_broker_protection_marks_recovery_when_ambiguous_and_stop_absent():
    # Ack lost AND no stop at the exchange → fail closed to RECOVERY_REQUIRED,
    # never a blind re-place.
    broker = StopManagerBroker(ambiguous=True, reaches_exchange=False)
    manager = _protection_manager(broker)
    trade = _fut_trade()

    assert manager.ensure_broker_protection(trade, 10) is False
    assert broker.place_calls == 1
    assert trade.protective_order_id == ""
    assert trade.execution_status == "RECOVERY_REQUIRED"


def test_ensure_broker_protection_lazily_initialises_protection_lock():
    # Partially-constructed managers (tests / recovery paths) must not crash.
    broker = StopManagerBroker(preexisting="SLM-PRE")
    manager = _protection_manager(broker)
    del manager._protection_lock
    assert manager.ensure_broker_protection(_fut_trade(), 10) is True
    assert isinstance(manager._protection_lock, type(threading.RLock()))


# ───────────────── HIGH-2: enabling Auto Mode in REAL re-validates readiness ─────────────────

def _real_scanner():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = False
    scanner.on_notification = None
    scanner.capital_total = 500000.0
    scanner.data = SimpleNamespace()
    scanner.trades = SimpleNamespace(has_live_exposure=lambda: False)
    return scanner


def test_enable_auto_mode_while_already_real_is_blocked_when_not_ready(monkeypatch):
    scanner = _real_scanner()
    monkeypatch.setattr(
        "scanner.validate_real_mode_readiness",
        lambda settings, provider: {
            "ok": False,
            "blockers": ["all broker sources unavailable"],
            "warnings": [],
        },
    )

    configured = scanner.configure(
        auto_mode=True, confirm_real_mode=True, real_mode_verification="YES",
    )

    assert configured is False
    assert scanner.auto_mode is False
    assert "readiness" in scanner._last_configure_block_reason.lower()


def test_enable_auto_mode_in_real_succeeds_when_ready(monkeypatch):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = False
    scanner.on_notification = None
    scanner.data = SimpleNamespace()
    scanner.futures_sl_pct = 0.3
    scanner.options_sl_pct = 15.0
    scanner.max_daily_loss_pct = 3.0
    scanner.risk_fut_pct = 2.0
    scanner.risk_opt_pct = 8.0
    scanner.capital_fut = 250000.0
    scanner.capital_opt = 100000.0
    scanner.capital_total = 1100000.0
    scanner.trades = SimpleNamespace(
        update_risk_settings=lambda *a, **k: None,
        has_live_exposure=lambda: False,
    )
    scanner.risk_manager = SimpleNamespace(update_settings=lambda *a, **k: None)
    scanner._publish_config_snapshot = lambda *a, **k: None
    scanner.mtf = SimpleNamespace(apply_engine_params=lambda *a, **k: None)
    monkeypatch.setattr(
        "scanner.validate_real_mode_readiness",
        lambda settings, provider: {"ok": True, "blockers": [], "warnings": []},
    )

    configured = scanner.configure(
        auto_mode=True, confirm_real_mode=True, real_mode_verification="YES",
        persist_settings=False,
    )

    assert configured is True
    assert scanner.auto_mode is True
