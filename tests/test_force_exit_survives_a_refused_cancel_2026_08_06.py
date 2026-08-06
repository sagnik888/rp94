"""Five defects found on the EOD force-exit and reconcile paths.

1. emergency_flatten_trade bound `cancelled` inside the try, so a refused stop cancel past
   the block deadline fell through to `if not cancelled` with the name unbound and raised
   UnboundLocalError. check_session_end has no per-trade try, so that took every remaining
   trade in the snapshot with it — and the orphan sweep after them.

2. The orphan sweep was gated on `if self.trades.open_trades:`, and an orphan makes
   can_open_trade refuse EVERY entry, so on a day that starts with one — the case the
   sweep was written for — open_trades stays empty and check_session_end never runs.

3. The orphan flatten had no throttle on its SUCCESS path. check_session_end runs about
   once a second past the force-exit time and orphan_positions only refreshes every 5s, so
   the same square-off went four or five times: a bought option turned into a naked short.

4. reconcile_broker_positions discarded the result of its protective-stop cancel, cleared
   the id and closed the row anyway — leaving an SL-M that nothing owns, to fire later
   against a flat book.

5. That same call was unguarded, so a lease-held PermissionError aborted the whole
   reconcile pass including the orphan detection that follows it.
"""
import threading
from datetime import datetime

import pytest

from trading.order_retry import BrokerOrderAmbiguous, BrokerOrderRejected
from trading.trade_manager import IST, TradeManager

SYMBOL = "NIFTY2680524500CE"


def _trade(**kw):
    t = type("T", (), {})()
    t.id = kw.get("id", "T1")
    t.protective_order_id = kw.get("stop", "SLM-9001")
    t.trading_symbol = SYMBOL
    t.symbol_token = "1"
    t.quantity = 75
    t.broker_quantity = 75
    t.direction = "LONG"
    t.inst_type = "OPT"
    t.status = "OPEN"
    t.execution_status = kw.get("execution_status", "RECOVERY_REQUIRED")
    t.gtt_trigger_id = kw.get("gtt", "")
    t.entry_price = 217.5
    t.current_price = 200.0
    t.exit_reason = ""
    return t


def _manager(broker):
    m = TradeManager.__new__(TradeManager)
    m.broker = broker
    m.mode = "REAL"
    m.product_type = "CARRYFORWARD"
    m._lock = threading.RLock()
    m.open_trades = {}
    m.closed_trades = []
    m.orphan_positions = {}
    m.save_state = lambda: None
    m._log_ui = lambda *a, **k: None
    m._mark_recovery = lambda t, r: None
    return m


# ── 1. the crash that aborted the force exit ─────────────────────────────────

class _RefusingBroker:
    def __init__(self, exc=None):
        self.exc = exc or BrokerOrderRejected("account lease held elsewhere", terminal=True)
        self.closes = []

    def cancel_order(self, oid, variety="NORMAL"):
        raise self.exc

    def close_order(self, *a, **k):
        self.closes.append(a)
        return "EXIT-1"


def _flatten_manager(broker, withhold):
    m = _manager(broker)
    m._withhold_flatten = lambda trade, reason: withhold
    m._unowned_protective_stop_blocks_flatten = lambda t, r: False
    m._release_gtt_fallback = lambda t, r: True
    return m


def test_a_refused_cancel_past_the_deadline_does_not_crash():
    """The exact repro: refused cancel, deadline elapsed, so 'exit anyway'."""
    m = _flatten_manager(_RefusingBroker(), withhold=False)
    m.emergency_flatten_trade(_trade(), "EOD force exit")   # must not raise


def test_a_refused_cancel_before_the_deadline_still_withholds():
    """Withholding is the default; the deadline is the opt-in escape."""
    broker = _RefusingBroker()
    m = _flatten_manager(broker, withhold=True)
    assert m.emergency_flatten_trade(_trade(), "EOD force exit") is False
    assert broker.closes == [], "flattened on top of a stop that may still be resting"


def test_a_permission_error_is_caught_too():
    m = _flatten_manager(_RefusingBroker(PermissionError("lease held")), withhold=False)
    m.emergency_flatten_trade(_trade(), "EOD force exit")


def test_an_unproven_cancel_keeps_the_stop_id():
    """Clearing it is how an SL-M ends up owned by nobody."""
    m = _flatten_manager(_RefusingBroker(), withhold=False)
    recorded = []
    m._mark_recovery = lambda t, r: recorded.append(r)
    trade = _trade()
    m.emergency_flatten_trade(trade, "EOD force exit")
    assert trade.protective_order_id == "SLM-9001"
    assert recorded and "SLM-9001" in recorded[0]
    assert "UNCONFIRMED" in recorded[0]


def test_a_proven_cancel_clears_the_stop_id():
    class _OK(_RefusingBroker):
        def cancel_order(self, oid, variety="NORMAL"):
            return True

    m = _flatten_manager(_OK(), withhold=False)
    trade = _trade()
    m.emergency_flatten_trade(trade, "EOD force exit")
    assert trade.protective_order_id == ""


def test_a_refusal_is_not_double_counted_against_the_deadline():
    """One failure, one withhold decision — not two."""
    broker = _RefusingBroker()
    m = _flatten_manager(broker, withhold=False)
    calls = []
    m._withhold_flatten = lambda trade, reason: calls.append(reason) or False
    m.emergency_flatten_trade(_trade(), "EOD force exit")
    assert len(calls) == 1, f"withhold ran {len(calls)} times for one refusal"


# ── 2. the gate that made the orphan sweep unreachable ───────────────────────

def test_the_session_end_gate_opens_for_orphans_with_no_local_trades():
    """An orphan blocks every entry, so open_trades is empty exactly when it matters."""
    import ast
    import inspect
    import textwrap

    import scanner as scanner_module

    src = textwrap.dedent(inspect.getsource(scanner_module.Scanner))
    assert "self.trades.open_trades or getattr(self.trades, \"orphan_positions\", None)" in src, (
        "check_session_end is still gated on local rows alone, so on a day that starts "
        "with an orphan the sweep never runs"
    )


# ── 3. the square-off that repeated ──────────────────────────────────────────

class _CountingBroker:
    def __init__(self, result="EXIT-ORPHAN"):
        self.result = result
        self.exits = []

    def close_order(self, symbol, token, qty, side, product_type="CARRYFORWARD", **kw):
        self.exits.append((symbol, qty, side))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _On:
    force_exit_flattens_orphans = True
    ut_force_exit_time = "15:25"


def _orphan_manager(broker):
    m = _manager(broker)
    m.orphan_positions = {SYMBOL: {"tradingsymbol": SYMBOL, "netqty": "75", "symboltoken": "1"}}
    return m


def _run_session_end(manager, settings, passes=1):
    import config.settings as cs

    original = cs.get_settings
    cs.get_settings = lambda: settings
    try:
        for _ in range(passes):
            manager.check_session_end(datetime.now(IST).replace(hour=15, minute=29))
    finally:
        cs.get_settings = original


def test_the_orphan_square_off_is_sent_once_not_once_per_pass():
    broker = _CountingBroker()
    _run_session_end(_orphan_manager(broker), _On(), passes=6)
    assert broker.exits == [(SYMBOL, 75, "SELL")], (
        f"sent {len(broker.exits)} square-offs for one orphan — a bought option becomes "
        "a naked short in a product the broker will not close"
    )


def test_a_refused_square_off_is_not_retried_every_second_either():
    broker = _CountingBroker(BrokerOrderRejected("no session", terminal=True))
    _run_session_end(_orphan_manager(broker), _On(), passes=5)
    assert len(broker.exits) == 1


def test_an_ambiguous_square_off_is_never_re_sent():
    broker = _CountingBroker(BrokerOrderAmbiguous("read timed out"))
    _run_session_end(_orphan_manager(broker), _On(), passes=5)
    assert len(broker.exits) == 1, "re-sent an exit that may already be live"


def test_two_orphans_each_get_their_own_attempt():
    broker = _CountingBroker()
    m = _orphan_manager(broker)
    m.orphan_positions["BANKNIFTY26AUG52000PE"] = {
        "tradingsymbol": "BANKNIFTY26AUG52000PE", "netqty": "-35", "symboltoken": "2",
    }
    _run_session_end(m, _On(), passes=4)
    assert sorted(e[0] for e in broker.exits) == [
        "BANKNIFTY26AUG52000PE", SYMBOL,
    ]
    assert ("BANKNIFTY26AUG52000PE", 35, "BUY") in broker.exits


# ── 6. one bad row must not take the whole book with it ──────────────────────

class _Exploding:
    """Fails on the first trade only."""

    def __init__(self):
        self.exits = []

    def close_order(self, symbol, token, qty, side, product_type="CARRYFORWARD", **kw):
        self.exits.append(symbol)
        return "EXIT-1"


def _book_manager(broker, boom_on="T1"):
    m = _manager(broker)
    closed = []
    m.close_trade = lambda tid, px, reason, exit_time=None: (
        (_ for _ in ()).throw(RuntimeError("boom")) if tid == boom_on else closed.append(tid)
    )
    m.closed_ids = closed
    m.open_trades = {
        "T1": _trade(id="T1", execution_status="OPEN"),
        "T2": _trade(id="T2", execution_status="OPEN"),
        "T3": _trade(id="T3", execution_status="OPEN"),
    }
    return m


def test_one_failing_exit_does_not_abandon_the_rest_of_the_book():
    broker = _Exploding()
    m = _book_manager(broker)
    _run_session_end(m, _On())
    assert sorted(m.closed_ids) == ["T2", "T3"], (
        "a raise on the first trade took every later position down with it"
    )


def test_a_failing_exit_is_escalated_by_name():
    broker = _Exploding()
    m = _book_manager(broker)
    logged = []
    m._log_ui = lambda msg, level="info": logged.append(msg)
    _run_session_end(m, _On())
    assert any("FORCE EXIT FAILED" in msg for msg in logged)
    assert any("Square off manually" in msg for msg in logged)


def test_the_orphan_sweep_still_runs_when_every_local_exit_failed():
    """The sweep is the last line of defence; it must not depend on the loop succeeding."""
    broker = _CountingBroker()
    m = _book_manager(broker, boom_on=None)
    m.close_trade = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    m.orphan_positions = {SYMBOL: {"tradingsymbol": SYMBOL, "netqty": "75", "symboltoken": "1"}}
    _run_session_end(m, _On())
    assert broker.exits == [(SYMBOL, 75, "SELL")], "the orphan sweep was skipped"
