"""FORCE_EOD_KILL must not retry a flatten that can never succeed (2026-07-31 EOD).

Two trades reached the end of session as RECOVERY_REQUIRED with no broker contract on
the row. check_session_end() calls emergency_flatten_trade() on every pass for as long
as a row stays RECOVERY_REQUIRED, and the guard at the top of that method re-marked
recovery and returned False each time, so the terminal logged

    CRITICAL | RECOVERY REQUIRED: NIFTY. FORCE_EOD_KILL; emergency flatten unavailable
    CRITICAL | RECOVERY REQUIRED: SENSEX. FORCE_EOD_KILL; emergency flatten unavailable

on a loop until the process was stopped. The row never reached a terminal state.

The safety rule that must never regress: a position the broker may still hold is NOT
auto-closed to silence the loop. We latch the retry and escalate to a human instead,
because inventing a terminal state for an unknown position is the dangerous failure.
"""
import threading
from datetime import datetime

import pytz

from trading.trade_manager import Trade, TradeManager

IST = pytz.timezone("Asia/Kolkata")


class _Broker:
    def __init__(self):
        self.close_calls = 0

    def close_order(self, *_a, **_kw):
        self.close_calls += 1
        return "EXIT-1"

    def cancel_order(self, *_a, **_kw):
        return True


def _manager(broker):
    m = TradeManager.__new__(TradeManager)
    m.broker = broker
    m.product_type = "CARRYFORWARD"
    m.open_trades = {}
    m.closed_trades = []
    m.ui_logger = None
    m.on_notification = lambda *_a, **_kw: None
    m._lock = threading.RLock()
    m.save_state = lambda: None
    return m


def _trade(**over):
    t = Trade(
        id="T0003_NIFTY_07311356",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=124.45,
        entry_time=IST.localize(datetime(2026, 7, 31, 13, 56, 19)),
        trailing_stop=105.78,
        current_stop=105.78,
        lots=1,
        lot_size=65,
        grade="A+",
        inst_type="OPT",
        status="OPEN",
        execution_status="RECOVERY_REQUIRED",
    )
    for key, value in over.items():
        setattr(t, key, value)
    return t


def test_unflattenable_recovery_alerts_once_then_stops_retrying():
    manager = _manager(_Broker())
    trade = _trade(trading_symbol="", symbol_token="")
    alerts = []
    manager._alert_recovery = lambda t: alerts.append(t.execution_error)

    for _ in range(10):  # ten EOD passes, as check_session_end would drive it
        assert manager.emergency_flatten_trade(trade, "FORCE_EOD_KILL") is False

    assert len(alerts) == 1, f"alerted {len(alerts)} times, expected a single escalation"
    assert "stop retrying" in alerts[0]
    # The unknown position is escalated, never silently closed.
    assert trade.status == "OPEN"
    assert trade.execution_status == "RECOVERY_REQUIRED"


def test_flatten_still_runs_when_the_contract_is_present():
    """The latch must not suppress a flatten that can actually be submitted."""
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade(trading_symbol="NIFTY04AUG2624350CE", symbol_token="60620", broker_quantity=65)

    assert manager.emergency_flatten_trade(trade, "FORCE_EOD_KILL") is True
    assert broker.close_calls == 1
    assert trade.execution_status == "EXIT_PENDING"
    assert trade.exit_order_id == "EXIT-1"
