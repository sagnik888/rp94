"""Live-audit fixes (2026-07-10 session): reversion wiring, ghost gates, logging.

Findings from the in-market audit:
  1. Reversion/snapback arming only ran on real TradeManager closes — in
     manual/signal mode the (virtual) candidate exits never consulted it, so
     the enabled feature could not arm. Now both paths share
     _maybe_arm_reversion_from_exit; the size-of-event gate still decides.
  2. Ghost trades latched EXIT_PENDING forever behind the manual confirmation
     gate, and session rollover retained them as RECOVERY_REQUIRED (false
     CRITICAL) — ghosts never had broker orders.
  3. Heavy-trend lot-scale margin warning fired per evaluation (74 lines for
     ~4 accepted trades) — now once per (label, inst_type, lots).
  4. Mojibake (double-encoded emoji) repaired in 5 source files.
"""

from __future__ import annotations

import inspect
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytz

from scanner import Scanner
from trading import trade_manager as tm_module
from trading.trade_manager import TradeManager

IST = pytz.timezone("Asia/Kolkata")


# ── 1) Reversion arming shared by real-close AND candidate-exit paths ────────


def _arm_scanner(monkeypatch, *, enabled=True):
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "post_exit_reversion_enabled", enabled)
    sc = Scanner.__new__(Scanner)
    calls = []

    def _arm(instrument, **kw):
        calls.append({"instrument": instrument, **kw})
        return True

    sc._arm_reversion_window = _arm
    return sc, calls


def _exited(direction="LONG", inst_type="OPT", option_type="CE"):
    return SimpleNamespace(direction=direction, inst_type=inst_type, option_type=option_type)


def test_arm_helper_fires_for_major_win(monkeypatch):
    sc, calls = _arm_scanner(monkeypatch)
    ok = sc._maybe_arm_reversion_from_exit(
        "NIFTY",
        exited_like=_exited("LONG"),
        reason="PEAK_THREAT_EXIT",
        pnl=650.0,
        source_tf="5min",
        when=datetime(2026, 7, 10, 12, 28),
    )
    assert ok is True
    assert calls and calls[0]["instrument"] == "NIFTY"
    assert calls[0]["source"] == "major_win"
    assert calls[0]["direction"] == "LONG"


def test_arm_helper_fires_for_oi_pcr_and_stop_loss(monkeypatch):
    sc, calls = _arm_scanner(monkeypatch)
    sc._maybe_arm_reversion_from_exit(
        "NIFTY", exited_like=_exited("SHORT", option_type="PE"), reason="OI_PCR_REVERSAL",
        pnl=-145.0, source_tf="5min",
    )
    sc._maybe_arm_reversion_from_exit(
        "SENSEX", exited_like=_exited("SHORT", option_type="PE"), reason="LOSS_THREAT_EXIT",
        pnl=-93.0, source_tf="15min",
    )
    assert [c["source"] for c in calls] == ["oi_pcr", "stop_loss"]
    assert calls[0]["direction"] == "SHORT"


def test_arm_helper_respects_source_tf_and_toggle(monkeypatch):
    sc, calls = _arm_scanner(monkeypatch)
    # 1min source trades never arm the snapback window.
    assert sc._maybe_arm_reversion_from_exit(
        "NIFTY", exited_like=_exited(), reason="PEAK_THREAT_EXIT", pnl=500.0, source_tf="1min",
    ) is False
    # Losing "major win" token without profit does not arm via the win path.
    assert sc._maybe_arm_reversion_from_exit(
        "NIFTY", exited_like=_exited(), reason="TARGET_HIT", pnl=-10.0, source_tf="5min",
    ) is False
    assert calls == []
    sc2, calls2 = _arm_scanner(monkeypatch, enabled=False)
    assert sc2._maybe_arm_reversion_from_exit(
        "NIFTY", exited_like=_exited(), reason="PEAK_THREAT_EXIT", pnl=500.0, source_tf="5min",
    ) is False
    assert calls2 == []


def test_candidate_exit_path_consults_reversion_arm():
    """create_exit (virtual/manual ledger) must arm, not just real closes."""
    # The helper is nested inside this method; inspect the complete method instead
    # of truncating create_exit at a fixed source-character count.
    src = inspect.getsource(Scanner._check_session_candidates_breaches_locked)
    assert "def create_exit(" in src
    assert "_maybe_arm_reversion_from_exit" in src
    real_close = inspect.getsource(Scanner._close_and_record_internal)
    assert "_maybe_arm_reversion_from_exit" in real_close


# ── 2) Ghost trades: rollover + manual gate ──────────────────────────────────


def _manager(monkeypatch):
    manager = TradeManager.__new__(TradeManager)
    manager.open_trades = {}
    manager.closed_trades = []
    manager._trade_counter = 0
    manager._lock = threading.RLock()
    monkeypatch.setattr(tm_module.db, "save_trades", lambda rows: None)
    return manager


def _trade(tid, *, is_ghost, days_ago=0, direction="LONG"):
    t = tm_module.Trade(
        id=tid,
        instrument="NIFTY",
        timeframe="5min",
        direction=direction,
        entry_price=100.0,
        current_price=100.0,
        entry_time=datetime.now(IST) - timedelta(days=days_ago),
        trailing_stop=95.0,
        current_stop=95.0,
        grade="B+",
        lots=1,
        lot_size=10,
    )
    t.is_ghost = is_ghost
    t.status = "OPEN"
    return t


def test_reset_session_closes_ghosts_and_keeps_real_reconciliation(monkeypatch):
    manager = _manager(monkeypatch)
    manager.open_trades = {
        "G1": _trade("G1", is_ghost=True, days_ago=0),   # premarket ghost, same day
        "G2": _trade("G2", is_ghost=True, days_ago=1),   # stale ghost
        "R1": _trade("R1", is_ghost=False, days_ago=1),  # real previous-session
        "R2": _trade("R2", is_ghost=False, days_ago=0),  # real same-day
    }
    out = manager.reset_session(datetime.now(IST).date())
    # Ghosts closed as stale; real trades retained.
    assert set(manager.open_trades) == {"R1", "R2"}
    assert out["active_retained"] == 2
    ghost_rows = [t for t in manager.closed_trades if t.id in {"G1", "G2"}]
    assert len(ghost_rows) == 2
    for t in ghost_rows:
        assert t.status == "CLOSED"
        assert t.exit_reason == "STALE_SESSION_END"
    assert manager.open_trades["R1"].execution_status == "RECOVERY_REQUIRED"
    assert manager.open_trades["R2"].execution_status != "RECOVERY_REQUIRED"


def test_manual_exit_gate_bypassed_for_ghosts():
    sc = Scanner.__new__(Scanner)
    sc.auto_mode = False
    sc._reset_daily_counters = lambda: None
    ghost = SimpleNamespace(
        instrument="NIFTY",
        is_ghost=True,
        pending_exit=False,
        execution_status="OPEN",
        exit_reason="",
        entry_price=100.0,
        direction="LONG",
        target=110.0,
        trailing_stop=95.0,
        rr_ratio=1.5,
        quantity=10,
        lots=1,
        lot_size=10,
    )
    sc.trades = SimpleNamespace(open_trades={"g": ghost})
    closed = []
    sc._close_and_record_internal = lambda tid, px, reason, exit_time=None: closed.append(tid)
    sc._close_and_record("g", 105.0, "PEAK_THREAT_EXIT")
    assert closed == ["g"], "ghost exits must finalize immediately, never latch EXIT_PENDING"
    assert ghost.pending_exit is False


def test_manual_exit_gate_still_latches_real_trades():
    sc = Scanner.__new__(Scanner)
    sc.auto_mode = False
    sc._reset_daily_counters = lambda: None
    real = SimpleNamespace(
        instrument="NIFTY",
        is_ghost=False,
        pending_exit=False,
        execution_status="OPEN",
        exit_reason="",
        entry_price=100.0,
        direction="LONG",
        target=110.0,
        trailing_stop=95.0,
        rr_ratio=1.5,
        quantity=10,
        lots=1,
        lot_size=10,
    )
    sc.trades = SimpleNamespace(open_trades={"r": real})
    closed = []
    sc._close_and_record_internal = lambda tid, px, reason, exit_time=None: closed.append(tid)
    sc._close_and_record("r", 105.0, "PEAK_THREAT_EXIT")
    assert closed == []  # latched for manual confirmation instead
    assert real.pending_exit is True
    assert real.execution_status == "EXIT_PENDING"


# ── 3) Lot-scale warning fires once per key ──────────────────────────────────


def test_heavy_trend_margin_warning_once_per_key():
    from config.settings import Settings
    from engine import position_sizing as ps

    s = Settings(
        auto_risk_sizing_enabled=False,
        risk_based_lots_enabled=False,
        heavy_trend_lot_scale_enabled=True,
        heavy_trend_lot_multiplier_opt=2.0,
    )
    before = len(ps._LOT_SCALE_WARNED)
    for _ in range(3):
        ps.resolve_trade_lots(
            settings=s,
            inst_type="OPT",
            requested_lots=7,  # unique base → unique warn key for this test
            lot_size=65,
            entry_price=100.0,
            stop_price=92.0,
            heavy_trend_scale=True,
        )
    added = len(ps._LOT_SCALE_WARNED) - before
    assert added == 1, "same (label, inst_type, lots) must register exactly one warning key"


# ── 4) Sources stay mojibake-free ────────────────────────────────────────────


def test_no_mojibake_in_repaired_sources():
    files = [
        "engine/scanner_chart_mixin.py",
        "engine/scanner_recalculation.py",
        "engine/signal_processor.py",
        "scanner.py",
        "data/candle_builder.py",
        "trading/trade_manager.py",
    ]
    for path in files:
        text = open(path, encoding="utf-8").read()
        assert "ðŸ" not in text, f"double-encoded emoji reintroduced in {path}"


# ── 5) Per-index timeout is a designed degradation, not an ERROR ─────────────


def test_index_timeout_logs_warning_not_hanging_error():
    import scanner as scanner_module

    full = inspect.getsource(scanner_module)
    assert "might be hanging" not in full
    assert "per-index budget" in full
