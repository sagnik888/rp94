"""GTT fallback must protect an unprotected position without ever doubling the exit.

A GTT lives on Zerodha's servers and fires even when this process is down — which is
exactly the exposure that left positions naked on 2026-07-31 while FORCE_EOD_KILL
looped on "emergency flatten unavailable".

The invariant that makes it safe: a GTT and an exchange SL-M must NEVER both be live
for one position. If both fire, the position exits twice and the second fill opens a
naked reversed leg. So the GTT is armed only where the broker REFUSED the SL-M
outright (nothing resting to collide with), released the moment a real SL-M takes over,
and released before any flatten — and a refused release withholds the exit rather than
marketing out over an armed trigger.
"""
import threading
from datetime import datetime

import pytz

from trading.trade_manager import Trade, TradeManager

IST = pytz.timezone("Asia/Kolkata")


class _Broker:
    def __init__(self, stop_id=None, gtt_id="55501", cancel_gtt=True, cancel_order=True):
        self._stop_id = stop_id
        self._gtt_id = gtt_id
        self._cancel_gtt = cancel_gtt
        self._cancel_order = cancel_order
        self.gtt_placed = []
        self.gtt_cancelled = []
        self.close_calls = 0

    def find_working_protective_stop(self, *_a, **_kw):
        return ""

    def place_protective_stop(self, *_a, **_kw):
        return self._stop_id

    def place_gtt_stop(self, symbol, qty, side, trigger, last_price=0.0, product_type=""):
        self.gtt_placed.append((symbol, qty, side, trigger))
        return self._gtt_id

    def cancel_gtt_stop(self, trigger_id):
        self.gtt_cancelled.append(trigger_id)
        return self._cancel_gtt

    def cancel_order(self, *_a, **_kw):
        return self._cancel_order

    def close_order(self, *_a, **_kw):
        self.close_calls += 1
        return "EXIT-1"

    def get_order_status(self, *_a, **_kw):
        return None


def _manager(broker):
    m = TradeManager.__new__(TradeManager)
    m.broker = broker
    m.mode = "REAL"
    m.product_type = "NRML"
    m.open_trades = {}
    m.closed_trades = []
    m.ui_logger = None
    m.on_notification = lambda *_a, **_kw: None
    m._lock = threading.RLock()
    m._protection_lock = threading.RLock()
    m.save_state = lambda: None
    m.options_sl_pct = 15.0
    m.futures_sl_pct = 0.3
    m._alert_recovery = lambda _t: None
    return m


def _trade(**over):
    t = Trade(
        id="T0001_NIFTY_08010930", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=124.45, entry_time=datetime.now(IST), trailing_stop=105.78,
        current_stop=105.78, lots=1, lot_size=65, grade="A+", inst_type="OPT",
        status="OPEN", trading_symbol="NIFTY2680424350CE", symbol_token="16816386",
        broker_quantity=65, current_price=124.45,
    )
    for k, v in over.items():
        setattr(t, k, v)
    return t


def test_refused_slm_arms_a_server_side_gtt():
    broker = _Broker(stop_id=None)          # broker refuses the SL-M outright
    manager, trade = _manager(broker), _trade()

    assert manager.ensure_broker_protection(trade) is False   # still needs a human
    assert trade.gtt_trigger_id == "55501"
    assert broker.gtt_placed and broker.gtt_placed[0][2] == "SELL"
    assert "GTT fallback armed" in trade.execution_error


def test_working_slm_releases_the_gtt():
    """Both live means two exits and a naked reversed leg."""
    broker = _Broker(stop_id="SLM-1")
    manager, trade = _manager(broker), _trade(gtt_trigger_id="55501")

    assert manager.ensure_broker_protection(trade) is True
    assert trade.gtt_trigger_id == ""
    assert broker.gtt_cancelled == ["55501"]
    assert trade.protective_order_id == "SLM-1"


def test_a_second_gtt_is_never_stacked():
    broker = _Broker(stop_id=None)
    manager, trade = _manager(broker), _trade(gtt_trigger_id="EXISTING")
    manager.ensure_broker_protection(trade)
    assert broker.gtt_placed == []
    assert trade.gtt_trigger_id == "EXISTING"


def test_flatten_releases_the_gtt_first():
    broker = _Broker()
    manager, trade = _manager(broker), _trade(gtt_trigger_id="55501")

    assert manager.emergency_flatten_trade(trade, "FORCE_EOD_KILL") is True
    assert broker.gtt_cancelled == ["55501"]
    assert broker.close_calls == 1


def test_unconfirmed_gtt_cancellation_withholds_the_flatten():
    """Marketing out over an armed trigger is the double-exit this must prevent."""
    broker = _Broker(cancel_gtt=False)
    manager, trade = _manager(broker), _trade(gtt_trigger_id="55501")

    assert manager.emergency_flatten_trade(trade, "FORCE_EOD_KILL") is False
    assert broker.close_calls == 0
    assert trade.gtt_trigger_id == "55501"          # still armed, not forgotten
    assert "GTT cancellation unconfirmed" in trade.execution_error


def test_a_broker_without_gtt_support_still_works():
    """SignalSimBroker and any legacy broker have no GTT methods."""
    class _Plain(_Broker):
        place_gtt_stop = None
        cancel_gtt_stop = None

    broker = _Plain(stop_id=None)
    manager, trade = _manager(broker), _trade()
    assert manager.ensure_broker_protection(trade) is False
    assert trade.gtt_trigger_id == ""
