"""E2E certification: 4 indices x 3 timeframes through the REAL reconcile pass.

An external review's standing objection was that the position-ownership fix was
covered only by unit tests on the allocation helper and by source-string assertions,
with no run over the actual grid this system trades. This is that run.

THE GRID
    NIFTY (lot 65), BANKNIFTY (30), SENSEX (20), MIDCPNIFTY (120)
        x  1min, 5min, 15min
    = 12 concurrent trades, three per index, all three on ONE strike per index.

That shape is reachable in the operator's configuration today:
ut_concurrency_guard is False and max_trades_per_index is 12, so nothing prevents
three timeframes stacking on the same contract.

WHAT IT CERTIFIES
Kite reports POSITIONS per tradingsymbol, so the three trades on an index read ONE
row whose netqty is their combined size. reconcile assigned that row's quantity to
each of them, and broker_quantity SIZES orders -- ensure_broker_protection, the
flatten path, _request_broker_exit. Each trade therefore armed a stop for three
times what it held. On options that sells what the account does not own.

Every assertion below runs the real TradeManager.reconcile_broker_positions over a
genuine Kite-shaped payload. The lot sizes are the real ones (settings.py:952), so
the four indices span the true 6x range rather than a uniform stand-in -- a bug that
scaled with lot size would be invisible on a flat grid.

SAFETY
Mode is HISTORICAL, the manager is built with __new__, and the broker is a fake whose
every order method raises. No network, no database, no real broker.
"""
import threading
from datetime import datetime, timedelta

import pytest

from trading.trade_manager import Trade, TradeManager


# The real per-index lot sizes: MIDCPNIFTY 120 vs NIFTY 65 / BANKNIFTY 30 / SENSEX 20.
GRID = {
    "NIFTY": ("NIFTY2681424300CE", 65),
    "BANKNIFTY": ("BANKNIFTY26AUG52000CE", 30),
    "SENSEX": ("SENSEX2681480000CE", 20),
    "MIDCPNIFTY": ("MIDCPNIFTY26AUG12500CE", 120),
}
TIMEFRAMES = ("1min", "5min", "15min")
OPENED_AT = {"1min": 0, "5min": 15, "15min": 30}  # minutes after the session start


class _NoOrdersBroker:
    def __getattr__(self, name):
        def _refuse(*a, **k):
            raise AssertionError(f"reconcile must not call broker.{name} in this scenario")
        return _refuse


class _Sink:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _trade(instrument, timeframe, symbol, lot_size, lots=1):
    t = Trade(
        id=f"{instrument}-{timeframe}",
        instrument=instrument,
        timeframe=timeframe,
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime(2026, 8, 14, 9, 20) + timedelta(minutes=OPENED_AT[timeframe]),
        trailing_stop=90.0,
        current_stop=90.0,
        lots=lots,
        lot_size=lot_size,
        grade="A",
    )
    t.trading_symbol = symbol
    t.symbol_token = f"tok-{instrument}"
    t.status = "OPEN"
    t.execution_status = "OPEN"
    t.broker_quantity = 0
    t.protective_order_id = ""
    t.broker_order_id = ""
    t.pending_execution = False
    t.inst_type = "OPT"
    return t


def _full_grid():
    return [
        _trade(inst, tf, symbol, lot)
        for inst, (symbol, lot) in GRID.items()
        for tf in TIMEFRAMES
    ]


def _manager(trades):
    mgr = TradeManager.__new__(TradeManager)
    mgr.ui_logger = _Sink()
    mgr.mode = "HISTORICAL"
    mgr.broker = _NoOrdersBroker()
    mgr._lock = threading.RLock()
    mgr.open_trades = {t.id: t for t in trades}
    mgr.closed_trades = []
    mgr.orphan_positions = {}
    mgr.product_type = "CARRYFORWARD"
    mgr._partial_exit_unknown_logged = set()
    mgr.save_state = lambda *a, **k: None
    mgr.ensure_broker_protection = lambda *a, **k: True
    return mgr


def _positions(per_index_netqty):
    """One row per contract, as Kite really reports: aggregated across our trades."""
    return {
        "data": [
            {
                "tradingsymbol": GRID[inst][0],
                "netqty": qty,
                "sellavgprice": 0.0,
                "buyavgprice": 100.0,
            }
            for inst, qty in per_index_netqty.items()
        ]
    }


def _full_book():
    """Every index holding all three timeframes: netqty = 3 x lot."""
    return {inst: lot * 3 for inst, (_sym, lot) in GRID.items()}


# ── the whole grid at once ───────────────────────────────────────────────────

def test_twelve_concurrent_trades_each_keep_their_own_quantity():
    trades = _full_grid()
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(_full_book()))

    for t in trades:
        expected = GRID[t.instrument][1]
        assert t.broker_quantity == expected, (
            f"{t.id}: got {t.broker_quantity}, expected its own {expected} "
            f"(aggregate would be {expected * 3})"
        )


def test_no_trade_anywhere_on_the_grid_holds_the_aggregate():
    trades = _full_grid()
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(_full_book()))

    for t in trades:
        assert t.broker_quantity != GRID[t.instrument][1] * 3


@pytest.mark.parametrize("instrument", sorted(GRID))
def test_each_index_totals_back_to_its_own_broker_row(instrument):
    """Per index the split must be exact: no lot invented, none lost."""
    trades = _full_grid()
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(_full_book()))

    mine = [t for t in trades if t.instrument == instrument]
    assert sum(t.broker_quantity for t in mine) == GRID[instrument][1] * 3


@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_each_timeframe_is_sized_on_its_own_index(timeframe):
    """A 5min NIFTY trade must not inherit MIDCPNIFTY's 120."""
    trades = _full_grid()
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions(_full_book()))

    for t in [t for t in trades if t.timeframe == timeframe]:
        assert t.broker_quantity == GRID[t.instrument][1]


# ── cross-index isolation ────────────────────────────────────────────────────

def test_one_index_going_flat_does_not_disturb_the_others():
    """SENSEX squared off at the broker; the other nine trades are untouched."""
    trades = _full_grid()
    mgr = _manager(trades)
    book = _full_book()
    book["SENSEX"] = 0

    mgr.reconcile_broker_positions(_positions(book))

    for t in trades:
        if t.instrument == "SENSEX":
            continue
        assert t.broker_quantity == GRID[t.instrument][1], (
            f"{t.id} was disturbed by an unrelated index going flat"
        )


def test_an_index_absent_from_the_book_does_not_take_another_indexs_row():
    """Only NIFTY is reported; nothing else may adopt its quantity."""
    trades = _full_grid()
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(_positions({"NIFTY": 65 * 3}))

    for t in trades:
        if t.instrument == "NIFTY":
            assert t.broker_quantity == 65
        else:
            assert t.broker_quantity == 0, f"{t.id} adopted a foreign position row"


def test_a_partially_filled_index_never_over_arms_any_of_its_trades():
    """BANKNIFTY holds only two lots' worth against three trades claiming one each."""
    trades = _full_grid()
    mgr = _manager(trades)
    book = _full_book()
    book["BANKNIFTY"] = 30 * 2

    mgr.reconcile_broker_positions(_positions(book))

    bn = [t for t in trades if t.instrument == "BANKNIFTY"]
    assert sum(t.broker_quantity for t in bn) <= 30 * 2
    for t in bn:
        assert t.broker_quantity <= 30


# ── multi-lot trades across the grid ─────────────────────────────────────────

def test_multi_lot_trades_get_their_own_intent_not_an_even_split():
    """One timeframe carries two lots; the split follows intent, not headcount."""
    trades = []
    for inst, (symbol, lot) in GRID.items():
        trades.append(_trade(inst, "1min", symbol, lot, lots=1))
        trades.append(_trade(inst, "5min", symbol, lot, lots=2))
        trades.append(_trade(inst, "15min", symbol, lot, lots=1))
    mgr = _manager(trades)

    mgr.reconcile_broker_positions(
        _positions({inst: lot * 4 for inst, (_s, lot) in GRID.items()})
    )

    for t in trades:
        lot = GRID[t.instrument][1]
        expected = lot * (2 if t.timeframe == "5min" else 1)
        assert t.broker_quantity == expected, f"{t.id}: {t.broker_quantity} != {expected}"


# ── the grid does not transact ───────────────────────────────────────────────

def test_a_healthy_grid_sends_no_orders():
    """Any broker order method raises; reaching one fails the test."""
    mgr = _manager(_full_grid())

    mgr.reconcile_broker_positions(_positions(_full_book()))


# ── proof the grid reproduces the defect ─────────────────────────────────────

def test_the_grid_catches_the_original_defect(monkeypatch):
    """Restore the pre-fix assignment and confirm all 12 trades inflate."""
    trades = _full_grid()
    mgr = _manager(trades)

    monkeypatch.setattr(
        TradeManager,
        "_allocate_broker_quantity",
        staticmethod(lambda group, net, ledger=None: {str(tid): abs(int(net or 0)) for tid, _ in group}),
    )

    mgr.reconcile_broker_positions(_positions(_full_book()))

    for t in trades:
        assert t.broker_quantity == GRID[t.instrument][1] * 3, (
            "the grid no longer reproduces the defect, so it certifies nothing"
        )
