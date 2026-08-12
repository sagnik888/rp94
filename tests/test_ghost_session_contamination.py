"""P1: same-day ghost/historical simulation rows must not block live entry budgets."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytz

from engine.signal_processor import SignalProcessor
from scanner import Scanner
from trading.trade_manager import Trade, TradeManager

IST = pytz.timezone("Asia/Kolkata")


def _trade(
    tid: str,
    *,
    instrument: str = "NIFTY",
    is_ghost: bool = False,
    status: str = "CLOSED",
    direction: str = "SHORT",
    broker_order_id: str = "",
    data_provenance: str = "",
    pnl: float = 0.0,
):
    now = IST.localize(datetime.now().replace(hour=10, minute=0, second=0, microsecond=0))
    t = Trade(
        id=tid,
        instrument=instrument,
        timeframe="5min",
        direction=direction,
        entry_price=100.0,
        entry_time=now,
        trailing_stop=99.0,
        current_stop=99.0,
        lots=1,
        lot_size=65,
        grade="A",
        inst_type="OPT",
        trading_symbol="NIFTY25000CE",
        status=status,
        pnl=pnl,
        is_ghost=is_ghost,
        data_provenance=data_provenance,
        broker_order_id=broker_order_id or "",
    )
    return t


def test_ghost_rows_excluded_from_daily_budget_counters():
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc.trades = TradeManager(broker=None, max_positions=20)
    sc._last_reset_date = None
    sc._trades_today = {}
    sc._losses_today = {}
    sc._last_exit_time = {}
    sc._counted_entry_keys = set()
    sc._session_trade_candidates = {}
    sc.max_consecutive_losses = 3
    sc.index_cooldown_minutes = 0
    sc._dynamic_trade_budget = lambda instrument: (14, "test_budget")
    sc._index_cooldown_applies = lambda *_a: (False, "")
    sc._reversion_window_active = lambda instrument, now=None: False

    ghosts = [
        _trade(f"T{i:04d}_NIFTY_07100055", is_ghost=True, status="CLOSED" if i < 36 else "OPEN")
        for i in range(60)
    ]
    live = _trade("LIVE_NIFTY_1", is_ghost=False, broker_order_id="BRK1", data_provenance="LIVE")
    sc.trades.closed_trades = [g for g in ghosts if g.status == "CLOSED"] + [live]
    sc.trades.open_trades = {g.id: g for g in ghosts if g.status == "OPEN"}

    sc._reset_daily_counters()
    # Only the one live trade counts
    assert sc._trades_today.get("NIFTY", 0) == 1
    ok, reason = sc._can_trade_instrument("NIFTY", datetime.now(IST))
    assert ok is True, reason


def test_purge_removes_ghosts_and_rebuilds_counters():
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc.trades = TradeManager(broker=None, max_positions=20)
    sc._last_reset_date = datetime.now(IST).date()  # pretend already rebuilt contaminated
    sc._trades_today = {"NIFTY": 60}
    sc._losses_today = {}
    sc._last_exit_time = {}
    sc._counted_entry_keys = set()
    sc._session_trade_candidates = {}

    ghost_open = _trade("T0254_NIFTY_07100055", is_ghost=True, status="OPEN", direction="SHORT")
    hist = _trade("H_NIFTY_1", is_ghost=False, status="CLOSED", data_provenance="HISTORICAL")
    live = _trade("LIVE_1", is_ghost=False, status="CLOSED", broker_order_id="B1", data_provenance="LIVE")
    sc.trades.open_trades = {ghost_open.id: ghost_open}
    sc.trades.closed_trades = [hist, live]

    removed = sc._purge_session_contamination(reason="test", force_counter_rebuild=True)
    assert removed["open"] == 1
    assert removed["closed"] >= 1  # hist provenance
    assert ghost_open.id not in sc.trades.open_trades
    assert sc._trades_today.get("NIFTY", 0) == 1


def test_is_live_book_trade_helper():
    ghost = SimpleNamespace(
        is_ghost=True, id="T1", trading_symbol="X", data_provenance="LIVE"
    )
    assert SignalProcessor._is_live_book_trade(ghost) is False
    hist = SimpleNamespace(
        is_ghost=False, id="T2", trading_symbol="X", data_provenance="HISTORICAL"
    )
    assert SignalProcessor._is_live_book_trade(hist) is False
    live = SimpleNamespace(
        is_ghost=False, id="T3", trading_symbol="NIFTYCE", data_provenance="LIVE"
    )
    assert SignalProcessor._is_live_book_trade(live) is True


def test_opposite_open_guard_skips_ghost_short():
    """Ghost T0254 SHORT must not reject a live LONG candidate as opposite trade."""
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc.trades = TradeManager(broker=None, max_positions=20)
    ghost = _trade("T0254_NIFTY_07100055", is_ghost=True, status="OPEN", direction="SHORT")
    sc.trades.open_trades = {ghost.id: ghost}

    # Simulate filter used in process_best_signal open-trade loop
    live_open = [
        t
        for t in sc.trades.open_trades.values()
        if t.instrument == "NIFTY"
        and t.status == "OPEN"
        and SignalProcessor._is_live_book_trade(t)
    ]
    assert live_open == []


def test_hist_to_real_mode_switch_purges_ghosts():
    sc = Scanner.__new__(Scanner)
    sc.mode = "HISTORICAL"
    sc.trades = TradeManager(broker=None, max_positions=20)
    sc.trades.mode = "HISTORICAL"
    sc._last_reset_date = None
    sc._trades_today = {}
    sc._losses_today = {}
    sc._last_exit_time = {}
    sc._counted_entry_keys = set()
    sc._session_trade_candidates = {}
    sc.on_notification = None

    for i in range(5):
        g = _trade(f"T{i}_NIFTY", is_ghost=True, status="OPEN")
        sc.trades.open_trades[g.id] = g
    sc.trades.closed_trades = [
        _trade("H_1", data_provenance="HISTORICAL"),
        _trade("LIVE_1", broker_order_id="B9", data_provenance="LIVE"),
    ]

    # Directly invoke the contamination purge that mode switch runs
    sc.mode = "REAL"
    sc.trades.mode = "REAL"
    sc._purge_session_contamination(reason="mode_HISTORICAL_to_REAL", force_counter_rebuild=True)
    assert not any(bool(getattr(t, "is_ghost", False)) for t in sc.trades.open_trades.values())
    assert sc._trades_today.get("NIFTY", 0) == 1
