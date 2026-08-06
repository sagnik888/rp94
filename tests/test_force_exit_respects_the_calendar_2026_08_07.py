"""The 15:25 force exit must not fire orders into a shut exchange.

check_session_end gated on the CLOCK alone: `if now_t < kill_threshold: return`. Nothing
asked whether the exchange traded today.

So a position that survived Friday's square-off — a broker reject, an RMS refusal, an IP
block, any of which this system has seen — stayed in open_trades, the scanner kept
running, and at 15:23 on SATURDAY `now_t >= kill_threshold` was true again. It sent exit
orders into a closed exchange, roughly once a second, for two days.

The position is real and does need closing, so this does not go quiet. It escalates once
per closed day, names the symbols, and tells the operator to square off manually. What it
will not do is fire orders that can only be rejected.
"""
import threading
from datetime import datetime

import pytest

from trading.trade_manager import IST, TradeManager

SYMBOL = "NIFTY2680724500CE"


class _Broker:
    def __init__(self):
        self.exits = []

    def close_order(self, symbol, token, qty, side, product_type="CARRYFORWARD", **kw):
        self.exits.append((symbol, qty, side))
        return "EXIT-1"


class _Provider:
    def __init__(self, holidays=(), special=()):
        self.holidays = {str(d) for d in holidays}
        self.special = {str(d) for d in special}

    def is_market_holiday(self, dt=None):
        return (dt or datetime.now()).date().isoformat() in self.holidays

    def is_market_special_session(self, dt=None):
        return (dt or datetime.now()).date().isoformat() in self.special


class _S:
    ut_force_exit_time = "15:25"
    force_exit_flattens_orphans = False


def _trade():
    t = type("T", (), {})()
    t.id = "T1"
    t.trading_symbol = SYMBOL
    t.symbol_token = "1"
    t.status = "OPEN"
    t.execution_status = "OPEN"
    t.current_price = 200.0
    t.protective_order_id = ""
    return t


def _manager(provider, broker=None):
    m = TradeManager.__new__(TradeManager)
    m.broker = broker or _Broker()
    m.mode = "REAL"
    m.product_type = "CARRYFORWARD"
    m._lock = threading.RLock()
    m.open_trades = {"T1": _trade()}
    m.closed_trades = []
    m.orphan_positions = {}
    m.data = provider
    m.save_state = lambda: None
    m._log_ui = lambda *a, **k: None
    m.closed_ids = []
    m.close_trade = lambda tid, px, reason, exit_time=None: m.closed_ids.append(tid)
    return m


def _run(manager, when, settings=None):
    import config.settings as cs

    original = cs.get_settings
    cs.get_settings = lambda: (settings or _S())
    try:
        manager.check_session_end(when)
    finally:
        cs.get_settings = original


# 2026-08-07 is a Friday; 08 Sat, 09 Sun, 10 Mon.
FRIDAY = datetime(2026, 8, 7, 15, 29)
SATURDAY = datetime(2026, 8, 8, 15, 29)
SUNDAY = datetime(2026, 8, 9, 15, 29)
MONDAY = datetime(2026, 8, 10, 15, 29)


def test_a_weekday_force_exit_still_runs():
    m = _manager(_Provider())
    _run(m, FRIDAY)
    assert m.closed_ids == ["T1"], "the force exit stopped working on a normal trading day"


def test_saturday_sends_no_orders():
    """The live defect."""
    m = _manager(_Provider())
    _run(m, SATURDAY)
    assert m.closed_ids == [], "sent exit orders into a shut exchange"
    assert m.broker.exits == []


def test_sunday_sends_no_orders():
    m = _manager(_Provider())
    _run(m, SUNDAY)
    assert m.closed_ids == []


def test_a_holiday_sends_no_orders():
    m = _manager(_Provider(holidays=["2026-08-10"]))
    _run(m, MONDAY)
    assert m.closed_ids == []


def test_a_special_saturday_session_still_squares_off():
    """The exchange does occasionally open on a Saturday; the calendar knows."""
    m = _manager(_Provider(special=["2026-08-08"]))
    _run(m, SATURDAY)
    assert m.closed_ids == ["T1"]


def test_the_closed_day_is_escalated_by_name():
    m = _manager(_Provider())
    logged = []
    m._log_ui = lambda msg, level="info": logged.append(msg)
    _run(m, SATURDAY)
    assert logged, "positions sat through a closed day in silence"
    assert "MARKET CLOSED" in logged[0]
    assert SYMBOL in logged[0]
    assert "Square off at the Zerodha terminal" in logged[0]


def test_the_escalation_is_once_per_day_not_once_per_second():
    """check_session_end re-runs about once a second past the threshold."""
    m = _manager(_Provider())
    logged = []
    m._log_ui = lambda msg, level="info": logged.append(msg)
    for _ in range(6):
        _run(m, SATURDAY)
    assert len(logged) == 1, f"escalated {len(logged)} times on one closed day"


def test_a_second_closed_day_escalates_again():
    """Saturday and Sunday are two separate days of carrying risk."""
    m = _manager(_Provider())
    logged = []
    m._log_ui = lambda msg, level="info": logged.append(msg)
    _run(m, SATURDAY)
    _run(m, SUNDAY)
    assert len(logged) == 2


def test_an_empty_book_stays_quiet_on_a_closed_day():
    m = _manager(_Provider())
    m.open_trades = {}
    logged = []
    m._log_ui = lambda msg, level="info": logged.append(msg)
    _run(m, SATURDAY)
    assert logged == []


def test_an_orphan_alone_is_still_escalated():
    m = _manager(_Provider())
    m.open_trades = {}
    m.orphan_positions = {SYMBOL: {"tradingsymbol": SYMBOL, "netqty": "75"}}
    logged = []
    m._log_ui = lambda msg, level="info": logged.append(msg)
    _run(m, SATURDAY)
    assert logged and SYMBOL in logged[0]


def test_an_unreadable_calendar_fails_OPEN():
    """Refusing to square off on a real trading day is worse than a rejected order."""
    class _Broken:
        def is_market_holiday(self, dt=None):
            raise RuntimeError("calendar unavailable")

    m = _manager(_Broken())
    _run(m, FRIDAY)
    assert m.closed_ids == ["T1"]


def test_no_provider_at_all_still_blocks_a_weekend():
    """A weekend needs no calendar to recognise."""
    m = _manager(None)
    _run(m, SATURDAY)
    assert m.closed_ids == []


def test_no_provider_still_allows_a_weekday():
    m = _manager(None)
    _run(m, FRIDAY)
    assert m.closed_ids == ["T1"]
