"""Two independent defects, both about doing recovery work on every tick.

1. expire_stale_open_trades re-marked an already-marked row.

   `marked += 1` was unconditional for every prior-session trade, so it stayed above
   zero for as long as that trade was open. `marked` gates a save_state() plus
   reconcile_broker_positions(), and reconcile makes a SYNCHRONOUS Kite get_positions()
   call. The function runs from _scan_cycle on the 1s event loop, so a single
   prior-session position meant a blocking broker REST call and a full trades-table
   snapshot every second, around the clock — on the loop that also hosts the 100ms exit
   monitor.

   A prior-session open position is precisely the state this system reaches when the
   15:25 force exit fails (NRML does not auto-square-off): the state where exits matter
   most is the one that starved the exit loop.

2. The auto-execution path passed is_recovery=self.is_warmup.

   is_warmup only means the scanner is still loading data. is_recovery=True makes
   can_open_trade skip BOTH the orphan block and the max-concurrent-positions cap, and
   writes the trade execution_status="OPEN" with pending_execution=False — already
   established rather than awaiting a fill. Auto Mode is not persisted, so an operator
   re-enabling it seconds after a restart lands inside _background_initial_setup, where
   is_warmup is True. A fresh entry is never a recovery.
"""
import ast
import inspect
import textwrap
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from trading.trade_manager import IST, TradeManager


class _Manager:
    """Drives the real expire_stale_open_trades against counting stubs."""

    def __init__(self, trades):
        self.open_trades = {t.id: t for t in trades}
        self._lock = threading.RLock()
        self.saves = 0
        self.reconciles = 0

    def save_state(self):
        self.saves += 1

    def _mark_dirty(self):
        self.saves += 1

    def reconcile_broker_positions(self):
        self.reconciles += 1


def _trade(tid, days_ago=1, status="OPEN"):
    return SimpleNamespace(
        id=tid,
        entry_time=datetime.now(IST) - timedelta(days=days_ago),
        execution_status=status,
        execution_error="",
    )


def _run(mgr, times=1):
    for _ in range(times):
        TradeManager.expire_stale_open_trades(mgr, now=datetime.now(IST))


# ── 1. the re-marking storm ──────────────────────────────────────────────────

def test_the_first_pass_marks_and_reconciles():
    m = _Manager([_trade("T1")])
    _run(m)
    assert m.open_trades["T1"].execution_status == "RECOVERY_REQUIRED"
    assert (m.saves, m.reconciles) == (1, 1)


def test_later_passes_do_no_broker_work():
    """The fix: 600 ticks must not be 600 blocking get_positions() calls."""
    m = _Manager([_trade("T1")])
    _run(m, times=600)
    assert m.reconciles == 1, f"reconciled {m.reconciles} times for one stale trade"
    assert m.saves == 1


def test_the_status_is_still_enforced_on_every_pass():
    """State is idempotent; only the SIDE EFFECTS are transition-gated."""
    m = _Manager([_trade("T1")])
    _run(m)
    m.open_trades["T1"].execution_status = "OPEN"   # something reset it
    _run(m)
    assert m.open_trades["T1"].execution_status == "RECOVERY_REQUIRED"
    assert m.reconciles == 2, "a genuine re-transition must reconcile again"


def test_a_new_stale_trade_still_triggers_a_reconcile():
    m = _Manager([_trade("T1")])
    _run(m, times=5)
    assert m.reconciles == 1
    m.open_trades["T2"] = _trade("T2")
    _run(m)
    assert m.reconciles == 2


def test_a_same_session_trade_is_never_marked():
    m = _Manager([_trade("T1", days_ago=0)])
    _run(m, times=3)
    assert m.open_trades["T1"].execution_status == "OPEN"
    assert (m.saves, m.reconciles) == (0, 0)


def test_the_return_value_counts_transitions_not_rows():
    m = _Manager([_trade("T1")])
    assert TradeManager.expire_stale_open_trades(m, now=datetime.now(IST)) == 1
    assert TradeManager.expire_stale_open_trades(m, now=datetime.now(IST)) == 0


# ── 2. warmup is not recovery ────────────────────────────────────────────────

def test_the_auto_entry_path_never_claims_recovery():
    from scanner import Scanner

    tree = ast.parse(textwrap.dedent(inspect.getsource(Scanner._coordinate_and_execute)))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "open_trade":
            continue
        for kw in node.keywords:
            if kw.arg != "is_recovery":
                continue
            assert isinstance(kw.value, ast.Constant) and kw.value.value is False, (
                "a live entry path still derives is_recovery from something other than "
                "False — is_warmup was the original bug"
            )


def test_is_recovery_still_bypasses_the_caps_this_protects():
    """The premise. If it stops bypassing them, re-read this reasoning."""
    src = inspect.getsource(TradeManager.can_open_trade)
    assert "not is_recovery and self.orphan_positions" in src
    assert "not is_recovery and active_positions >= cap" in src
