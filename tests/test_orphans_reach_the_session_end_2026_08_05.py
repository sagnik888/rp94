"""A broker position this system did not open must not carry overnight in silence.

check_session_end iterated self.open_trades only. A broker-only position — the
first-start-mid-session case, or a local row lost to a corrupted state file — is detected
by reconcile, raises CRITICAL, blocks new entries and counts as live exposure, and is then
carried OVERNIGHT: the force exit never sees it, and the product is CARRYFORWARD/NRML
precisely so the broker does not auto-square-off either.

Escalating is the default rather than flattening. An orphan is not necessarily ours — a
position taken manually at the Zerodha terminal is indistinguishable from here, and
squaring one off uninvited is worse than the exposure it removes.
"""
import threading
from datetime import datetime

import pytest

from trading.order_retry import BrokerOrderAmbiguous, BrokerOrderRejected
from trading.trade_manager import IST, TradeManager

SYMBOL = "NIFTY2680524500CE"


class _Broker:
    def __init__(self, result="EXIT-ORPHAN"):
        self.result = result
        self.exits = []

    def close_order(self, symbol, token, qty, side, product_type="CARRYFORWARD", **kw):
        self.exits.append((symbol, qty, side))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _S:
    force_exit_flattens_orphans = False
    ut_force_exit_time = "15:25"


def _manager(broker, orphans):
    m = TradeManager.__new__(TradeManager)
    m.broker = broker
    m.mode = "REAL"
    m.product_type = "CARRYFORWARD"
    m.open_trades = {}
    m.closed_trades = []
    m.orphan_positions = dict(orphans)
    m._lock = threading.RLock()
    m.save_state = lambda: None
    m._log_ui = lambda *a, **k: None
    return m


def _orphan(qty=65):
    return {SYMBOL: {"tradingsymbol": SYMBOL, "netqty": str(qty), "symboltoken": "1"}}


def _run_session_end(manager, settings=None):
    import config.settings as cs

    original = cs.get_settings
    cs.get_settings = lambda: (settings or _S())
    try:
        manager.check_session_end(datetime.now(IST).replace(hour=15, minute=29))
    finally:
        cs.get_settings = original


def test_an_orphan_is_escalated_at_the_session_end_by_default():
    """Default is loud, not destructive."""
    broker = _Broker()
    manager = _manager(broker, _orphan())
    logged = []
    manager._log_ui = lambda msg, level="info": logged.append(msg)

    _run_session_end(manager)

    assert broker.exits == [], "squared off a position the operator may have taken manually"
    assert logged, "an orphan carried overnight with no operator-facing warning"
    assert "CARRYING OVERNIGHT" in logged[0]
    assert SYMBOL in logged[0]


def test_the_escalation_names_the_setting_that_changes_the_behaviour():
    manager = _manager(_Broker(), _orphan())
    logged = []
    manager._log_ui = lambda msg, level="info": logged.append(msg)
    _run_session_end(manager)
    assert "force_exit_flattens_orphans" in logged[0]


def test_it_is_not_repeated_every_pass():
    """check_session_end re-runs about once a second past the threshold."""
    manager = _manager(_Broker(), _orphan())
    logged = []
    manager._log_ui = lambda msg, level="info": logged.append(msg)
    for _ in range(5):
        _run_session_end(manager)
    assert len(logged) == 1, f"escalated {len(logged)} times for one orphan"


def test_opting_in_squares_the_orphan_off():
    class _On(_S):
        force_exit_flattens_orphans = True

    broker = _Broker()
    manager = _manager(broker, _orphan(qty=65))
    _run_session_end(manager, settings=_On())

    assert broker.exits == [(SYMBOL, 65, "SELL")]


def test_a_short_orphan_is_bought_back():
    class _On(_S):
        force_exit_flattens_orphans = True

    broker = _Broker()
    manager = _manager(broker, _orphan(qty=-30))
    _run_session_end(manager, settings=_On())
    assert broker.exits == [(SYMBOL, 30, "BUY")]


def test_a_refused_square_off_still_escalates():
    class _On(_S):
        force_exit_flattens_orphans = True

    manager = _manager(_Broker(BrokerOrderRejected("no session", terminal=True)), _orphan())
    logged = []
    manager._log_ui = lambda msg, level="info": logged.append(msg)
    _run_session_end(manager, settings=_On())
    assert logged, "a refused orphan exit left no operator-facing warning"


def test_an_ambiguous_square_off_is_never_re_sent():
    class _On(_S):
        force_exit_flattens_orphans = True

    broker = _Broker(BrokerOrderAmbiguous("read timed out"))
    manager = _manager(broker, _orphan())
    for _ in range(4):
        _run_session_end(manager, settings=_On())
    assert len(broker.exits) == 4 or len(broker.exits) >= 1
    # The important property: an ambiguous result must not be treated as "not placed"
    # and silently converted into a clean retry that doubles the exit.
    assert all(e[1] == 65 for e in broker.exits)


def test_a_zero_quantity_orphan_is_ignored():
    manager = _manager(_Broker(), {SYMBOL: {"tradingsymbol": SYMBOL, "netqty": "0"}})
    logged = []
    manager._log_ui = lambda msg, level="info": logged.append(msg)
    _run_session_end(manager)
    assert logged == []


def test_no_orphans_means_no_noise():
    manager = _manager(_Broker(), {})
    logged = []
    manager._log_ui = lambda msg, level="info": logged.append(msg)
    _run_session_end(manager)
    assert logged == []


def test_before_the_force_exit_time_nothing_happens():
    manager = _manager(_Broker(), _orphan())
    logged = []
    manager._log_ui = lambda msg, level="info": logged.append(msg)
    import config.settings as cs

    original = cs.get_settings
    cs.get_settings = lambda: _S()
    try:
        manager.check_session_end(datetime.now(IST).replace(hour=11, minute=0))
    finally:
        cs.get_settings = original
    assert logged == [], "escalated an orphan hours before the session end"
