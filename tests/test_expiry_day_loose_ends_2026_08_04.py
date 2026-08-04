"""Two ways an expiry-day position outlived every mechanism meant to close it.

1. LATE-WINDOW COHORT. expiry_day_late_window (14:00-15:15) re-engages the CURRENT
   expiring contract, so a position can open at 15:10 on a contract that expires at 15:30.
   check_expiry_gamma_time_exit could never reach it: its time exit is 13:30, already past
   when the window opens, and its entry-time filter dropped every trade entered at/after
   the 13:00 cutoff -- structurally every trade the late window can create. Only the 15:25
   force exit stood between it and settlement, in the least liquid minutes of the day.

2. EXPIRED CONTRACT. If that force exit is refused, the contract expires and the broker
   drops it. reconcile's `position is None` branch only ever acted on ENTRY_SUBMITTED, so
   the row fell through to a bare `continue` and stayed OPEN forever at a stale premium --
   counted against exposure, and re-flattened against a contract that no longer trades.
"""
import threading
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from trading.trade_manager import IST, Trade, TradeManager

SYMBOL = "NIFTY2680424500CE"
TOKEN = "16858626"


class _Broker:
    def __init__(self):
        self.exits = []

    def close_order(self, symbol, token, qty, side, product_type, price=0.0):
        self.exits.append((symbol, qty, side))
        return "EXIT-1"

    def cancel_order(self, oid, variety="NORMAL"):
        return True

    def get_positions(self):
        return {"data": []}

    def get_order_status(self, oid):
        return None

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


def _trade(entry_at, tid="T1"):
    t = Trade(
        id=tid, instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=200.0, entry_time=entry_at, trailing_stop=180.0,
        current_stop=180.0, lots=1, lot_size=65, grade="A", inst_type="OPT",
        option_type="CE", atm_strike=24500.0,
        trading_symbol=SYMBOL, symbol_token=TOKEN,
        broker_order_id="251003000111111",
    )
    t.execution_status = "OPEN"
    t.broker_quantity = 65
    t.current_price = 205.0
    return t


# ── 1. the late-window cohort ────────────────────────────────────────────────

class _Expiry:
    """Expiry day, and the late window is what put this trade on the contract."""

    def __init__(self, gamma_window=True):
        self._gamma_window = gamma_window

    def is_expiry_day(self, name, target_date=None):
        return True

    def in_gamma_window(self, name, now_dt=None):
        return self._gamma_window


class _Settings:
    """Pinned here on purpose: the live .env value for this toggle is operator-controlled
    and was observed flipping mid-session, so reading it would make these tests depend on
    whatever the dashboard last wrote."""

    expiry_day_trade_current_week = True
    expiry_day_gamma_cutoff = "13:00"
    expiry_day_gamma_time_exit = "13:30"
    expiry_day_late_window = True
    expiry_day_late_window_start = "14:00"
    expiry_day_late_window_end = "15:15"
    expiry_day_late_window_time_exit = "15:20"


def _run_gamma_exit(manager, at, expiry_stub, settings=None):
    import config.settings as cs
    import engine.expiry_manager as em

    original_em = em.expiry_manager
    original_get = cs.get_settings
    em.expiry_manager = expiry_stub
    cs.get_settings = lambda: (settings or _Settings())
    try:
        manager.check_expiry_gamma_time_exit(current_time=at)
    finally:
        em.expiry_manager = original_em
        cs.get_settings = original_get


def test_the_whole_feature_stays_gated_off_when_disabled():
    """With the toggle off nothing here may fire, whichever cohort it is."""

    class _Off(_Settings):
        expiry_day_trade_current_week = False

    broker = _Broker()
    manager = _manager(broker)
    trade = _trade(_at(15, 10))
    manager.open_trades[trade.id] = trade
    manager.close_trade = lambda *a, **k: broker.exits.append(("x", 0, "x"))

    _run_gamma_exit(manager, _at(15, 20), _Expiry(), settings=_Off())
    assert broker.exits == []


def _at(h, m):
    return IST.localize(datetime(2026, 8, 4, h, m))


def test_a_late_window_trade_is_closed_before_the_force_exit():
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade(_at(15, 10))
    manager.open_trades[trade.id] = trade
    manager.close_trade = lambda tid, px, reason, exit_time=None: broker.exits.append(
        (SYMBOL, 65, reason)
    )

    _run_gamma_exit(manager, _at(15, 20), _Expiry())

    assert broker.exits, (
        "a position opened at 15:10 on a contract expiring at 15:30 had no time exit "
        "before the 15:25 force kill"
    )
    assert broker.exits[0][2] == "EXPIRY_LATE_WINDOW_TIME_EXIT"


def test_the_late_window_trade_is_left_alone_before_its_exit_time():
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade(_at(15, 10))
    manager.open_trades[trade.id] = trade
    manager.close_trade = lambda *a, **k: broker.exits.append(("x", 0, "x"))

    _run_gamma_exit(manager, _at(15, 12), _Expiry())
    assert broker.exits == []


def test_a_rolled_next_expiry_trade_is_not_touched():
    """After the cutoff the default mode rolls to next expiry; those must not be caught."""
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade(_at(14, 30))
    manager.open_trades[trade.id] = trade
    manager.close_trade = lambda *a, **k: broker.exits.append(("x", 0, "x"))

    _run_gamma_exit(manager, _at(15, 20), _Expiry(gamma_window=False))
    assert broker.exits == [], "closed a trade sitting on the NEXT expiry"


def test_the_morning_gamma_cohort_still_exits_at_1330():
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade(_at(10, 0))
    manager.open_trades[trade.id] = trade
    manager.close_trade = lambda tid, px, reason, exit_time=None: broker.exits.append(
        (SYMBOL, 65, reason)
    )

    _run_gamma_exit(manager, _at(13, 31), _Expiry())
    assert broker.exits and broker.exits[0][2] == "EXPIRY_GAMMA_TIME_EXIT"


# ── 2. the expired contract ──────────────────────────────────────────────────

def _master(with_contract):
    rows = [{"symbol": SYMBOL, "token": TOKEN}] if with_contract else [
        {"symbol": "NIFTY26AUG24500CE", "token": "999999"}
    ]
    return pd.DataFrame(rows)


def _run_reconcile(manager, listed):
    import engine.expiry_manager as em

    class _EM:
        df = _master(listed)

    original = em.expiry_manager
    em.expiry_manager = _EM()
    try:
        return manager.reconcile_broker_positions({"data": []})
    finally:
        em.expiry_manager = original


def _yesterday_trade():
    return _trade(datetime.now(IST) - timedelta(days=1), tid="EXPIRED")


def test_an_expired_contract_is_settled_out_of_open_trades():
    manager = _manager(_Broker())
    trade = _yesterday_trade()
    trade.execution_status = "RECOVERY_REQUIRED"
    manager.open_trades[trade.id] = trade

    _run_reconcile(manager, listed=False)

    assert trade.id not in manager.open_trades, (
        "the row survived its own contract and stays OPEN forever at a stale premium"
    )
    assert trade.exit_reason == "EXPIRY_SETTLED"
    assert trade.status == "CLOSED"
    assert "ESTIMATE" in trade.execution_error


def test_a_contract_still_listed_is_never_settled():
    """Absence from the positions book alone must not close a live position."""
    manager = _manager(_Broker())
    trade = _yesterday_trade()
    trade.execution_status = "RECOVERY_REQUIRED"
    manager.open_trades[trade.id] = trade

    _run_reconcile(manager, listed=True)

    assert trade.id in manager.open_trades
    assert trade.exit_reason != "EXPIRY_SETTLED"


def test_an_unreadable_master_is_unknown_not_expired():
    """None from the lookup must never read as 'the contract is gone'."""
    import engine.expiry_manager as em

    manager = _manager(_Broker())
    trade = _yesterday_trade()
    manager.open_trades[trade.id] = trade

    class _EM:
        df = None

    original = em.expiry_manager
    em.expiry_manager = _EM()
    try:
        manager.reconcile_broker_positions({"data": []})
    finally:
        em.expiry_manager = original

    assert trade.id in manager.open_trades, "settled a trade on a master we could not read"


def _settle_at(manager, trade, at, listed=False):
    """Drive the settlement decision at a FIXED clock.

    This assertion is about a session boundary, so it has to name the time rather than
    inherit whatever the wall clock happens to be: the first version of this test read
    datetime.now() through the implementation, passed all afternoon, and failed the
    moment the suite was run at 21:23.
    """
    import engine.expiry_manager as em

    class _EM:
        df = _master(listed)

    original = em.expiry_manager
    em.expiry_manager = _EM()
    try:
        return manager._settle_expired_contract(trade.id, trade, now=at)
    finally:
        em.expiry_manager = original


def test_a_same_session_trade_is_not_settled_intraday():
    """A contract missing from one positions read is usually just a slow book."""
    manager = _manager(_Broker())
    entry = datetime.now(IST).replace(hour=10, minute=0)
    trade = _trade(entry)
    manager.open_trades[trade.id] = trade

    settled = _settle_at(manager, trade, entry.replace(hour=11, minute=30))

    assert settled is False
    assert trade.id in manager.open_trades


def test_the_same_session_trade_is_settled_once_the_session_is_over():
    """After the close it is no longer a slow book — the contract really is gone."""
    manager = _manager(_Broker())
    entry = datetime.now(IST).replace(hour=10, minute=0)
    trade = _trade(entry)
    manager.open_trades[trade.id] = trade

    settled = _settle_at(manager, trade, entry.replace(hour=15, minute=40))

    assert settled is True
    assert trade.exit_reason == "EXPIRY_SETTLED"


def test_the_boundary_does_not_move_with_the_wall_clock():
    """Same inputs, two clocks, two answers — pinned so this cannot regress to now()."""
    entry = datetime.now(IST).replace(hour=10, minute=0)
    before, after = [], []
    for at, sink in ((entry.replace(hour=15, minute=34), before),
                     (entry.replace(hour=15, minute=36), after)):
        manager = _manager(_Broker())
        trade = _trade(entry)
        manager.open_trades[trade.id] = trade
        sink.append(_settle_at(manager, trade, at))
    assert before == [False] and after == [True]


def test_a_futures_row_is_never_settled_this_way():
    manager = _manager(_Broker())
    trade = _yesterday_trade()
    trade.inst_type = "FUT"
    manager.open_trades[trade.id] = trade

    _run_reconcile(manager, listed=False)

    assert trade.id in manager.open_trades
