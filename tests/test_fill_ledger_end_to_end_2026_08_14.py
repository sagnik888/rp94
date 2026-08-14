"""Step 4: drive each ledger path through the REAL code, not past it.

Two of the recording sites shipped wrong while looking right — the partial-exit and
scale-in writes were derived from the per-tradingsymbol aggregate they existed to
replace, and a filled protective stop was recorded nowhere at all. Both passed their
source-inspection tests. So these drive the actual methods with a fake broker and
assert on what the LEDGER ends up holding.

The scenario throughout is the one the ledger exists for: two trades on ONE contract,
where the broker's position row cannot say who owns what.

SAFETY: fake brokers, an isolated database file, HISTORICAL/REAL as each path requires,
no network.
"""
import os
import tempfile
import threading
import uuid
from datetime import datetime, timedelta

import pytest

from trading.trade_manager import Trade, TradeManager


SYMBOL = "NIFTY2681424300CE"
LOT = 75


class _Broker:
    """States per-ORDER fills, the way Kite's order_trades does."""

    def __init__(self, fills=None, stop_status=""):
        self.fills = dict(fills or {})
        self.stop_status = stop_status
        self.protective = []

    def get_fill(self, order_id):
        if order_id in self.fills:
            qty = self.fills[order_id]
            if qty is None:
                return {"state": "UNKNOWN", "reason": "cannot tell"}
            if qty == 0:
                return {"state": "NO_FILL", "quantity": 0}
            return {"state": "FILLED", "quantity": qty, "average_price": 100.0}
        return {"state": "UNKNOWN", "reason": "not found"}

    def get_order_status(self, _oid):
        return {"status": self.stop_status} if self.stop_status else {}

    def place_protective_stop(self, symbol, token, qty, side, trigger, product, **kw):
        self.protective.append({"qty": qty})
        return f"SL-{len(self.protective)}"

    def order_tag(self, trade_id, leg="", attempt=0):
        return f"{trade_id}|{leg}"[:20]

    def cancel_order(self, *a, **k):
        return True

    def find_protective_stop(self, *a, **k):
        return ""


class _Sink:
    def __getattr__(self, _n):
        return lambda *a, **k: None


def _trade(tid, lots=1, minute=0):
    t = Trade(
        id=tid, instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=100.0,
        entry_time=datetime(2026, 8, 14, 9, 20) + timedelta(minutes=minute),
        trailing_stop=90.0, current_stop=90.0, lots=lots, lot_size=LOT, grade="A",
    )
    t.trading_symbol = SYMBOL
    t.symbol_token = "12345"
    t.status = "OPEN"
    t.execution_status = "OPEN"
    t.broker_quantity = lots * LOT
    t.protective_order_id = ""
    t.broker_order_id = f"{tid}-entry"
    t.pending_execution = False
    t.inst_type = "OPT"
    t.is_ghost = False
    return t


@pytest.fixture
def ledger(monkeypatch):
    """An isolated ledger the manager's module-level `db` points at."""
    from data.sqlite_db import DatabaseManager
    import trading.trade_manager as tm

    path = os.path.join(tempfile.gettempdir(), f"e2e_ledger_{uuid.uuid4().hex}.db")
    handle = object.__new__(DatabaseManager)
    handle._init_db(path)
    monkeypatch.setattr(tm, "db", handle)
    yield handle
    try:
        handle.conn.close()
    except Exception:
        pass


def _mgr(trades, broker, mode="REAL"):
    mgr = TradeManager.__new__(TradeManager)
    mgr.ui_logger = _Sink()
    mgr.mode = mode
    mgr.broker = broker
    mgr._lock = threading.RLock()
    mgr.open_trades = {t.id: t for t in trades}
    mgr.closed_trades = []
    mgr.orphan_positions = {}
    mgr.product_type = "CARRYFORWARD"
    mgr._partial_exit_unknown_logged = set()
    mgr._last_protective_sync = {}
    mgr._last_protective_status_poll = {}
    mgr.save_state = lambda *a, **k: None
    return mgr


# ── the leg that shipped wrong: a protective stop firing ─────────────────────

def test_a_filled_protective_stop_reaches_the_ledger(ledger):
    """The commonest real exit on a shared contract, previously recorded nowhere."""
    t = _trade("T1")
    t.protective_order_id = "SL-1"
    broker = _Broker(fills={"SL-1": LOT}, stop_status="COMPLETE")
    mgr = _mgr([t], broker)
    ledger.record_order_fill("T1-entry", "T1", LOT, tradingsymbol=SYMBOL, leg="ENTRY")

    mgr.ensure_broker_protection(t)

    assert ledger.owned_quantity("T1") == 0, (
        "the stop sold this trade's quantity; the ledger still reports it held"
    )


def test_a_sibling_is_untouched_when_one_stop_fires(ledger):
    """The whole point: the aggregate cannot say whose stop fired. The ledger can."""
    t1, t2 = _trade("T1"), _trade("T2", minute=15)
    t1.protective_order_id = "SL-1"
    broker = _Broker(fills={"SL-1": LOT}, stop_status="COMPLETE")
    mgr = _mgr([t1, t2], broker)
    for tid in ("T1", "T2"):
        ledger.record_order_fill(f"{tid}-entry", tid, LOT, tradingsymbol=SYMBOL, leg="ENTRY")

    mgr.ensure_broker_protection(t1)

    assert ledger.owned_quantity("T1") == 0
    assert ledger.owned_quantity("T2") == LOT, "a sibling's stop closed this trade"


def test_an_unknown_stop_fill_leaves_the_ledger_alone(ledger):
    """Silence beats a guess: the older splitting rule is worse but honest."""
    t = _trade("T1")
    t.protective_order_id = "SL-1"
    broker = _Broker(fills={"SL-1": None}, stop_status="COMPLETE")   # UNKNOWN
    mgr = _mgr([t], broker)
    ledger.record_order_fill("T1-entry", "T1", LOT, tradingsymbol=SYMBOL, leg="ENTRY")

    mgr.ensure_broker_protection(t)

    assert ledger.owned_quantity("T1") == LOT, "an unknown fill was recorded as a real one"


# ── the ledger drives the allocation, end to end ─────────────────────────────

def _positions(netqty):
    return {"data": [{"tradingsymbol": SYMBOL, "netqty": netqty,
                      "sellavgprice": 0.0, "buyavgprice": 100.0}]}


def test_reconcile_sizes_from_the_ledger_after_a_sibling_exits(ledger):
    """T2 has closed. The row says 75 and cannot say whose it is; the ledger can."""
    t1, t2 = _trade("T1"), _trade("T2", minute=15)
    t1.broker_quantity = 0
    t2.broker_quantity = 0
    mgr = _mgr([t1, t2], _Broker(), mode="HISTORICAL")
    ledger.record_order_fill("T1-entry", "T1", LOT, tradingsymbol=SYMBOL, leg="ENTRY")
    ledger.record_order_fill("T2-entry", "T2", LOT, tradingsymbol=SYMBOL, leg="ENTRY")
    ledger.record_order_fill("T2-exit", "T2", -LOT, tradingsymbol=SYMBOL, leg="FULL_EXIT")

    mgr.reconcile_broker_positions(_positions(LOT))

    assert t1.broker_quantity == LOT, "the surviving trade lost its quantity"
    assert t2.broker_quantity == 0, "a closed trade was handed the remainder"


def test_the_oldest_first_rule_would_have_got_that_backwards(ledger):
    """Proof the scenario discriminates: without the ledger, T1 (oldest) takes it.

    Here the CLOSED trade is the older one, so the fallback rule hands the remaining
    quantity to the trade that has already gone.
    """
    t_old, t_new = _trade("T-old", minute=0), _trade("T-new", minute=30)
    t_old.broker_quantity = 0
    t_new.broker_quantity = 0
    mgr = _mgr([t_old, t_new], _Broker(), mode="HISTORICAL")

    mgr.reconcile_broker_positions(_positions(LOT))   # ledger silent

    assert t_old.broker_quantity == LOT
    assert t_new.broker_quantity == 0


def test_with_the_ledger_the_surviving_trade_gets_it(ledger):
    """Same shape, ledger populated: the answer inverts to the correct one."""
    t_old, t_new = _trade("T-old", minute=0), _trade("T-new", minute=30)
    t_old.broker_quantity = 0
    t_new.broker_quantity = 0
    mgr = _mgr([t_old, t_new], _Broker(), mode="HISTORICAL")
    for tid in ("T-old", "T-new"):
        ledger.record_order_fill(f"{tid}-entry", tid, LOT, tradingsymbol=SYMBOL, leg="ENTRY")
    ledger.record_order_fill("old-exit", "T-old", -LOT, tradingsymbol=SYMBOL, leg="FULL_EXIT")

    mgr.reconcile_broker_positions(_positions(LOT))

    assert t_old.broker_quantity == 0, "the closed trade was handed the remainder again"
    assert t_new.broker_quantity == LOT


# ── restart ──────────────────────────────────────────────────────────────────

def test_attribution_survives_a_restart(ledger):
    """A restart loses in-memory state; the ledger is what makes it recoverable."""
    from data.sqlite_db import DatabaseManager
    import trading.trade_manager as tm

    ledger.record_order_fill("T1-entry", "T1", LOT, tradingsymbol=SYMBOL, leg="ENTRY")
    ledger.record_order_fill("T2-entry", "T2", LOT, tradingsymbol=SYMBOL, leg="ENTRY")
    ledger.record_order_fill("T2-exit", "T2", -LOT, tradingsymbol=SYMBOL, leg="FULL_EXIT")
    path = str(ledger.db_path)
    ledger.conn.close()

    reopened = object.__new__(DatabaseManager)
    reopened._init_db(path)
    tm.db = reopened
    try:
        t1, t2 = _trade("T1"), _trade("T2", minute=15)
        t1.broker_quantity = 0
        t2.broker_quantity = 0
        mgr = _mgr([t1, t2], _Broker(), mode="HISTORICAL")

        mgr.reconcile_broker_positions(_positions(LOT))

        assert t1.broker_quantity == LOT
        assert t2.broker_quantity == 0
    finally:
        reopened.conn.close()


# ── a missing acknowledgement degrades, never lies ───────────────────────────

def test_a_trade_with_no_ledger_row_falls_back_rather_than_reading_zero(ledger):
    """The missing-ack case: absent must not be read as flat and left unprotected."""
    t1, t2 = _trade("T1"), _trade("T2", minute=15)
    t1.broker_quantity = 0
    t2.broker_quantity = 0
    mgr = _mgr([t1, t2], _Broker(), mode="HISTORICAL")
    ledger.record_order_fill("T1-entry", "T1", LOT, tradingsymbol=SYMBOL, leg="ENTRY")
    # T2 never acknowledged: no row at all.

    mgr.reconcile_broker_positions(_positions(LOT * 2))

    assert t1.broker_quantity == LOT, "the recorded trade lost its own quantity"
    assert t2.broker_quantity == LOT, "an unrecorded trade was read as flat"


def test_the_broker_row_still_caps_a_stale_ledger(ledger):
    """Attribution is the ledger's; total exposure is the broker's."""
    t1, t2 = _trade("T1"), _trade("T2", minute=15)
    t1.broker_quantity = 0
    t2.broker_quantity = 0
    mgr = _mgr([t1, t2], _Broker(), mode="HISTORICAL")
    for tid in ("T1", "T2"):
        ledger.record_order_fill(f"{tid}-entry", tid, LOT, tradingsymbol=SYMBOL, leg="ENTRY")

    mgr.reconcile_broker_positions(_positions(LOT))   # broker holds only one lot

    total = t1.broker_quantity + t2.broker_quantity
    assert total <= LOT, f"armed for {total} against a {LOT} position"
