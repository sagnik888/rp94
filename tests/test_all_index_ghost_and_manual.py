"""All 4 indices share ghost-budget fix; manual path remains available with AUTO off."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytz

from scanner import Scanner
from trading.trade_manager import Trade, TradeManager

IST = pytz.timezone("Asia/Kolkata")
INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]


def _mk(tid, inst, **kw):
    now = IST.localize(datetime.now().replace(hour=11, minute=0, second=0, microsecond=0))
    base = dict(
        id=tid,
        instrument=inst,
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        entry_time=now,
        trailing_stop=99.0,
        current_stop=99.0,
        lots=1,
        lot_size=50,
        grade="A",
        inst_type="OPT",
        trading_symbol=f"{inst}CE",
        status="CLOSED",
        pnl=0.0,
        is_ghost=False,
        data_provenance="",
        broker_order_id="",
    )
    base.update(kw)
    return Trade(**base)


def _scanner_stub():
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc.trades = TradeManager(broker=None, max_positions=50)
    sc._last_reset_date = None
    sc._trades_today = {}
    sc._losses_today = {}
    sc._last_exit_time = {}
    sc._counted_entry_keys = set()
    sc._session_trade_candidates = {}
    sc.max_consecutive_losses = 5
    sc.index_cooldown_minutes = 0
    sc._dynamic_trade_budget = lambda i: (14, "quad")
    sc._index_cooldown_applies = lambda i: (False, "")
    sc._reversion_window_active = lambda i, now=None: False
    return sc


def test_sixty_ghosts_each_index_do_not_block_any_budget():
    sc = _scanner_stub()
    for inst in INDICES:
        for i in range(60):
            sc.trades.closed_trades.append(_mk(f"T{i}_{inst}", inst, is_ghost=True))
            sc.trades.open_trades[f"GO_{inst}_{i}"] = _mk(
                f"GO_{inst}_{i}", inst, is_ghost=True, status="OPEN"
            )
    sc._reset_daily_counters()
    for inst in INDICES:
        assert sc._trades_today.get(inst, 0) == 0
        ok, reason = sc._can_trade_instrument(inst, datetime.now(IST))
        assert ok is True, f"{inst} blocked: {reason}"


def test_helpers_not_nifty_only():
    # Logic keys off instrument argument / trade.instrument — no hard-coded index filter.
    src = Path("scanner.py").read_text(encoding="utf-8")
    # purge / budget methods must not special-case a single index
    assert "def _is_non_live_contamination_trade" in src
    assert "def _trade_counts_for_daily_budget" in src
    assert "def _purge_session_contamination" in src
    assert "self._trades_today.get(instrument" in src


def test_manual_mode_path_intact_when_auto_off():
    src = Path("scanner.py").read_text(encoding="utf-8")
    assert "if not self.auto_mode and not is_warmup:" in src
    assert "accepted_by_gate" in src
    assert "async def execute_manual_signal" in src
    assert 'exec_type="M"' in src or "exec_type='M'" in src
    assert "def _update_active_trades" in src
    assert "if self.auto_mode:" in src  # execute_manual blocks only when auto ON
    # Dashboard wiring
    js = Path("dashboard/static/js/trade-table.js").read_text(encoding="utf-8")
    assert "execute_signal" in js
    assert "accepted_by_gate" in js
    server = Path("dashboard/server.py").read_text(encoding="utf-8")
    assert "execute_manual_signal" in server
    assert 'cmd == "execute_signal"' in server or 'cmd == "execute_signal"' in server


def test_manual_execute_rejects_only_when_auto_on():
    import asyncio

    sc = _scanner_stub()
    sc.auto_mode = True

    out = asyncio.run(sc.execute_manual_signal(signal_id="x", lifecycle_id=""))
    assert out.get("status") == "blocked"
    assert out.get("reason") == "auto_mode_enabled"

    sc.auto_mode = False
    out2 = asyncio.run(sc.execute_manual_signal(signal_id="missing", lifecycle_id=""))
    # With auto off, does not hit auto block — missing signal is normal.
    assert out2.get("reason") != "auto_mode_enabled"
    assert out2.get("status") == "blocked"
    assert out2.get("reason") == "signal_not_found"
