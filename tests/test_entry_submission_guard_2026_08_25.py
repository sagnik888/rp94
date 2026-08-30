"""Last-moment AUTO-entry authority at every broker exposure mutation."""

from datetime import datetime, timedelta
from types import SimpleNamespace
import asyncio
import inspect
import threading

import pytest

from trading.order_retry import BrokerOrderRejected
from trading.trade_manager import IST, Trade, TradeManager


class _Broker:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.place_calls = []
        self.place_payloads = []
        self.close_calls = []
        self.reservations = []
        self.released = []
        self.cancel_calls = []

    def preflight_entry_margin(self, **_kwargs):
        reservation_id = f"R{len(self.reservations) + 1}"
        self.reservations.append(reservation_id)
        return {"ok": True, "reservation_id": reservation_id}

    def release_entry_margin_reservation(self, reservation_id):
        self.released.append(str(reservation_id))

    def place_order(self, *args, **kwargs):
        symbol = kwargs.get("symbol") or (args[0] if args else "")
        self.place_calls.append(str(symbol))
        self.place_payloads.append({"args": args, "kwargs": dict(kwargs)})
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return f"ENTRY-{symbol}"

    def close_order(self, *args, **kwargs):
        symbol = kwargs.get("symbol") or (args[0] if args else "")
        self.close_calls.append(str(symbol))
        return f"EXIT-{symbol}"

    def get_positions(self):
        return {"data": []}

    def get_order_status(self, _order_id):
        return {"status": "OPEN"}

    def cancel_order_confirmed(self, order_id, variety="NORMAL"):
        self.cancel_calls.append((str(order_id), str(variety)))
        return True


def _manager(broker, guard=None):
    manager = TradeManager.__new__(TradeManager)
    manager.broker = broker
    manager.ui_logger = None
    manager.on_notification = None
    manager.entry_submission_guard = guard
    manager.max_positions = 4
    manager.session_end = manager._parse_time("15:25")
    manager.product_type = "CARRYFORWARD"
    manager.open_trades = {}
    manager.closed_trades = []
    manager._trade_counter = 0
    manager._lock = threading.RLock()
    manager._protection_lock = threading.RLock()
    manager._state_file = ""
    manager._last_mark_to_market_save = 0.0
    manager._last_protective_sync = {}
    manager._last_protective_status_poll = {}
    manager._last_recovery_alert = {}
    manager.orphan_positions = {}
    manager.futures_sl_pct = 0.3
    manager.options_sl_pct = 15.0
    manager.mode = "REAL"
    manager.is_warmup = False
    manager.save_state = lambda: None
    return manager


def _open(manager, *, exec_type="A", lifecycle_id="", callback=None, guard=None):
    return manager.open_trade(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        price=100.0,
        trailing_stop=95.0,
        lots=1,
        lot_size=10,
        grade="A",
        target=110.0,
        trading_symbol="NIFTYTESTFUT",
        symbol_token="TOKEN-NIFTY",
        inst_type="FUT",
        exec_type=exec_type,
        entry_time=IST.localize(datetime(2026, 8, 25, 10, 0)),
        lifecycle_id=lifecycle_id,
        on_execution_result=callback,
        entry_submission_guard=guard,
    )


@pytest.fixture(autouse=True)
def _synchronous_queue_and_no_retry_sleep(monkeypatch):
    monkeypatch.setattr("trading.order_retry.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda _name, func, *args, **kwargs: func(*args, **kwargs),
    )


def test_guard_blocks_before_first_broker_call_releases_margin_and_keeps_lifecycle():
    broker = _Broker()
    seen = []
    callbacks = []

    def guard(trade):
        seen.append(trade.lifecycle_id)
        return False, "critical host pressure"

    manager = _manager(broker, guard=guard)
    trade = _open(
        manager,
        lifecycle_id="NIFTY|5min|LONG|source-candle|lane-a|contract-a",
        callback=lambda _trade, accepted: callbacks.append(accepted),
    )

    assert seen == ["NIFTY|5min|LONG|source-candle|lane-a|contract-a"]
    assert broker.place_calls == []
    assert broker.reservations == broker.released == ["R1"]
    assert trade not in manager.open_trades.values()
    assert trade in manager.closed_trades
    assert trade.execution_status == "REJECTED"
    assert trade.exit_reason == "ENTRY_SUBMISSION_GUARD"
    assert "critical host pressure" in trade.execution_error
    assert callbacks == [False]


def test_recovery_reconstructs_existing_exposure_without_submitting_a_duplicate_order():
    broker = _Broker()
    manager = _manager(broker)

    trade = manager.open_trade(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        price=100.0,
        trailing_stop=95.0,
        lots=1,
        lot_size=10,
        grade="A",
        target=110.0,
        trading_symbol="NIFTYTESTFUT",
        symbol_token="TOKEN-NIFTY",
        inst_type="FUT",
        is_recovery=True,
        entry_time=IST.localize(datetime(2026, 8, 25, 10, 0)),
    )

    assert trade is not None
    assert trade.is_recovery is True
    assert trade.pending_execution is False
    assert trade.execution_status == "OPEN"
    assert broker.place_calls == []
    assert broker.reservations == []


def test_guard_is_rechecked_on_retry_and_all_attempt_reservations_are_released():
    broker = _Broker([BrokerOrderRejected("temporary refusal", terminal=False)])
    guard_calls = 0

    def guard(_trade):
        nonlocal guard_calls
        guard_calls += 1
        return (True, "") if guard_calls == 1 else (False, "Auto disabled while queued")

    manager = _manager(broker)
    trade = _open(manager, guard=guard)

    assert guard_calls == 2
    assert broker.place_calls == ["NIFTYTESTFUT"]
    assert broker.reservations == ["R1", "R2"]
    assert broker.released == ["R1", "R2"]
    assert trade.exit_reason == "ENTRY_SUBMISSION_GUARD"
    assert trade.execution_status == "REJECTED"


def test_each_retry_submits_the_same_fresh_levels_that_passed_final_policy():
    broker = _Broker([BrokerOrderRejected("temporary refusal", terminal=False), "ENTRY-2"])
    manager = _manager(broker)
    calls = 0

    def final_policy(trade):
        nonlocal calls
        calls += 1
        entry = 100.0 + calls
        trade._entry_policy_submission_levels = (entry, entry - 4.0, entry + 6.0)
        trade._entry_policy_submission_touch_quote = {
            "bid": entry - 0.5,
            "ask": entry,
            "age_seconds": 0.1,
            "usable": True,
            "source": "test_touch",
        }
        return True, ""

    manager.fresh_entry_policy_guard = final_policy
    trade = _open(manager)

    assert calls == 2
    assert [row["kwargs"]["price"] for row in broker.place_payloads] == [101.0, 102.0]
    assert [row["kwargs"]["touch_quote"]["ask"] for row in broker.place_payloads] == [
        101.0,
        102.0,
    ]
    assert trade.entry_price == pytest.approx(100.0)
    assert trade.execution_status == "ENTRY_SUBMITTED"


def test_manual_entry_bypasses_even_a_fail_closed_global_guard():
    broker = _Broker()
    guard_calls = 0

    def guard(_trade):
        nonlocal guard_calls
        guard_calls += 1
        return False, "must not veto operator"

    manager = _manager(broker, guard=guard)
    trade = _open(manager, exec_type="M")

    assert guard_calls == 0
    assert broker.place_calls == ["NIFTYTESTFUT"]
    assert trade in manager.open_trades.values()
    assert trade.execution_status == "ENTRY_SUBMITTED"


def test_manual_entry_cannot_bypass_the_immutable_fresh_entry_policy():
    broker = _Broker()
    policy_calls = 0
    global_guard_calls = 0

    def final_policy(_trade):
        nonlocal policy_calls
        policy_calls += 1
        return False, "actual execution RR is below 1.50R"

    def global_guard(_trade):
        nonlocal global_guard_calls
        global_guard_calls += 1
        return True, ""

    manager = _manager(broker, guard=global_guard)
    manager.fresh_entry_policy_guard = final_policy

    trade = _open(manager, exec_type="M")

    assert policy_calls == 1
    # Manual authority still bypasses only the operational AUTO/resource guard.
    assert global_guard_calls == 0
    assert broker.place_calls == []
    assert trade not in manager.open_trades.values()
    assert trade in manager.closed_trades
    assert trade.execution_status == "REJECTED"
    assert trade.exit_reason == "ENTRY_SUBMISSION_GUARD"
    assert "below 1.50R" in trade.execution_error


def test_exit_submission_never_reuses_the_entry_guard():
    broker = _Broker()
    guard_calls = 0

    def guard(_trade):
        nonlocal guard_calls
        guard_calls += 1
        return True, ""

    manager = _manager(broker, guard=guard)
    trade = _open(manager)
    assert guard_calls == 1

    trade.pending_execution = False
    trade.execution_status = "OPEN"
    trade.broker_quantity = trade.quantity
    assert manager.close_trade(trade.id, 101.0, "MANUAL") is True

    assert broker.close_calls == ["NIFTYTESTFUT"]
    assert guard_calls == 1


def test_scale_in_guard_blocks_before_place_order_and_releases_its_reservation():
    broker = _Broker()
    manager = _manager(broker, guard=lambda _trade: (False, "host pressure"))
    trade = Trade(
        id="T-SCALE",
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
        inst_type="FUT",
        trading_symbol="NIFTYTESTFUT",
        symbol_token="TOKEN-NIFTY",
        exec_type="A",
        broker_quantity=10,
        current_price=110.0,
        execution_status="OPEN",
    )

    manager._run_scale_in(trade, add_lots=1, quantity=10, price=110.0, reason="TEST")

    assert broker.place_calls == []
    assert broker.reservations == broker.released == ["R1"]
    assert trade.lots == 1
    assert trade.execution_status == "OPEN"


def test_futures_scale_in_policy_uses_a_fresh_marketable_touch_not_the_offer_price():
    broker = _Broker()
    manager = _manager(broker)
    seen = {}

    def final_policy(trade):
        seen["levels"] = tuple(trade._entry_policy_execution_levels)
        return False, "test stop after measuring"

    manager.fresh_entry_policy_guard = final_policy
    manager.scanner = SimpleNamespace(
        _candidate_execution_touch_quote=lambda trade: {
            "bid": 104.0,
            "ask": 105.0,
            "age_seconds": 0.1,
            "usable": True,
            "source": "kite_depth",
        }
    )
    trade = Trade(
        id="T-SCALE-FRESH-FUT",
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
        target=112.0,
        inst_type="FUT",
        trading_symbol="NIFTYTESTFUT",
        symbol_token="TOKEN-NIFTY",
        exec_type="A",
        broker_quantity=10,
        current_price=104.0,
        execution_status="OPEN",
    )

    manager._run_scale_in(trade, add_lots=1, quantity=10, price=110.0, reason="TEST")

    assert seen["levels"] == pytest.approx((102.5, 95.0, 112.0))
    assert broker.place_calls == []
    assert broker.reservations == broker.released == ["R1"]


def test_automated_scale_in_on_manual_origin_trade_does_not_bypass_guard():
    broker = _Broker()
    manager = _manager(broker, guard=lambda _trade: (False, "host pressure"))
    trade = Trade(
        id="T-MANUAL-SCALE",
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
        inst_type="FUT",
        trading_symbol="NIFTYTESTFUT",
        symbol_token="TOKEN-NIFTY",
        exec_type="M",
        broker_quantity=10,
        current_price=110.0,
        execution_status="OPEN",
    )

    manager._run_scale_in(trade, add_lots=1, quantity=10, price=110.0, reason="TEST")

    assert broker.place_calls == []
    assert broker.reservations == broker.released == ["R1"]
    assert trade.lots == 1


def test_automated_scale_in_on_recovered_trade_does_not_reuse_recovery_bypass():
    broker = _Broker()
    manager = _manager(broker, guard=lambda _trade: (False, "host pressure"))
    trade = Trade(
        id="T-RECOVERED-SCALE",
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
        inst_type="FUT",
        trading_symbol="NIFTYTESTFUT",
        symbol_token="TOKEN-NIFTY",
        exec_type="A",
        broker_quantity=10,
        current_price=110.0,
        execution_status="OPEN",
    )
    setattr(trade, "_entry_submission_guard_bypass", True)

    manager._run_scale_in(trade, add_lots=1, quantity=10, price=110.0, reason="TEST")

    assert broker.place_calls == []
    assert broker.reservations == broker.released == ["R1"]
    assert trade.lots == 1


def test_scanner_preserves_submission_guard_reason_in_rejected_signal_telemetry():
    from scanner import Scanner

    scanner = Scanner.__new__(Scanner)
    recorded = []
    logged = []
    scanner._rtracker_record_candidate = lambda candidate, **kwargs: recorded.append(kwargs)
    scanner._forget_signal_display_rows = lambda _candidate: None
    scanner.log_event = lambda message, category: logged.append((message, category))

    async def publish():
        return None

    scanner._publish_trade_payload_now = publish
    candidate = SimpleNamespace(
        instrument="NIFTY",
        direction="LONG",
        accepted_by_gate=False,
        status="ORDER PENDING",
    )
    trade = SimpleNamespace(
        exit_reason="ENTRY_SUBMISSION_GUARD",
        execution_error="critical host pressure",
    )

    asyncio.run(scanner._handle_broker_entry_result(candidate, trade, False))

    assert candidate.status == "REJECTED - AUTO SUBMISSION GUARD"
    assert candidate.exit_reason == "ENTRY_SUBMISSION_GUARD"
    assert recorded == [
        {
            "decision": "REJECTED",
            "reason": "critical host pressure",
            "source": "entry_submission_guard",
        }
    ]
    assert "critical host pressure" in logged[0][0]


def test_cancelled_stale_entry_is_not_market_replaced_when_guard_turns_false():
    broker = _Broker()
    manager = _manager(broker, guard=lambda _trade: (False, "Auto disabled"))
    trade = Trade(
        id="T-REPLACE",
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
        inst_type="FUT",
        trading_symbol="NIFTYTESTFUT",
        symbol_token="TOKEN-NIFTY",
        exec_type="A",
        broker_order_id="ORIGINAL-LIMIT",
        pending_execution=True,
        execution_status="ENTRY_SUBMITTED",
        current_price=100.0,
    )
    trade.entry_submitted_at = datetime.now(IST) - timedelta(minutes=2)
    manager.open_trades[trade.id] = trade

    manager.reconcile_broker_positions({"data": []})

    assert broker.cancel_calls == [("ORIGINAL-LIMIT", "NORMAL")]
    assert broker.place_calls == []
    assert trade not in manager.open_trades.values()
    assert trade in manager.closed_trades
    assert trade.exit_reason == "ENTRY_SUBMISSION_GUARD"
    assert trade.execution_status == "REJECTED"


def test_stale_market_replacement_gets_its_own_fresh_policy_phase_and_touch():
    broker = _Broker()
    manager = _manager(broker)
    phases = []

    def final_policy(trade):
        phases.append(str(getattr(trade, "_entry_policy_phase", "") or "broker_queue"))
        trade._entry_policy_submission_levels = (106.0, 100.0, 115.0)
        trade._entry_policy_submission_touch_quote = {
            "bid": 105.5,
            "ask": 106.0,
            "age_seconds": 0.1,
            "usable": True,
            "source": "test_touch",
        }
        return True, ""

    manager.fresh_entry_policy_guard = final_policy
    trade = Trade(
        id="T-REPLACE-FRESH",
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
        target=110.0,
        inst_type="FUT",
        trading_symbol="NIFTYTESTFUT",
        symbol_token="TOKEN-NIFTY",
        exec_type="A",
        broker_order_id="ORIGINAL-LIMIT",
        pending_execution=True,
        execution_status="ENTRY_SUBMITTED",
        current_price=101.0,
    )
    trade.entry_submitted_at = datetime.now(IST) - timedelta(minutes=2)
    manager.open_trades[trade.id] = trade

    manager.reconcile_broker_positions({"data": []})

    assert phases == ["stale_entry_replacement_pre_submit"]
    assert broker.place_calls == ["NIFTYTESTFUT"]
    assert broker.place_payloads[0]["kwargs"]["touch_quote"]["ask"] == 106.0
    assert trade.entry_replace_count == 1


def test_acknowledged_stale_entry_emits_a_second_result_when_replacement_guard_blocks():
    broker = _Broker()
    guard_calls = 0
    results = []

    def guard(_trade):
        nonlocal guard_calls
        guard_calls += 1
        return (True, "") if guard_calls == 1 else (False, "host pressure after ACK")

    manager = _manager(broker, guard=guard)
    trade = _open(
        manager,
        callback=lambda executed_trade, accepted: results.append(
            (executed_trade.lifecycle_id, accepted, executed_trade.exit_reason)
        ),
        lifecycle_id="stale-entry-lifecycle",
    )
    assert [accepted for _lifecycle, accepted, _reason in results] == [True]

    trade.entry_submitted_at = datetime.now(IST) - timedelta(minutes=2)
    manager.reconcile_broker_positions({"data": []})

    assert broker.cancel_calls == [(trade.broker_order_id, "NORMAL")]
    assert broker.place_calls == ["NIFTYTESTFUT"]
    assert [accepted for _lifecycle, accepted, _reason in results] == [True, False]
    assert results[-1] == (
        "stale-entry-lifecycle",
        False,
        "ENTRY_SUBMISSION_GUARD",
    )


def test_scanner_wires_candidate_lifecycle_and_worker_guard_to_entry_calls():
    from scanner import Scanner

    source = inspect.getsource(Scanner)
    assert source.count("lifecycle_id=self._candidate_lifecycle_key(") == 3
    assert "entry_submission_guard=self._queued_auto_entry_submission_guard" in source
