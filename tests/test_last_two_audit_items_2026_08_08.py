"""The final two audit items.

1. CASCADE DUPLICATES WERE DISCARDED UPSTREAM OF THE ADD.

   The operator's rule: a second signal on an index+timeframe that already has a working
   trade must ADD ONE LOT, never open a second trade at another strike.

   `_offer_cascade_add` is reached from `_coordinate_and_execute`, i.e. only for
   candidates `process_best_signal` actually RETURNS. But process_best_signal already
   dropped the duplicate itself: for an open same-index trade, `same_tf and
   source_overlaps` emitted "REJECTED - Existing Active Trade" and returned [].

   Source overlap is the ORDINARY case — the same engine re-firing on the same chart is
   exactly what a cascade confirming itself looks like — so the rule produced nothing at
   all in its most likely shape. The blocked duplicate now gets offered to the running
   trade before it is discarded.

   Gated on auto_mode for the same reason as the scanner-side call site: scale_in_trade
   reaches broker.place_order, and manual mode means the operator approves every entry.

2. STARTUP PUT THE SIGNAL LEDGER INTO THE LIVE POSITION BOOK.

   On launch the scanner rebuilds the day's session candidates into Trade objects so the
   trades panel is populated. They are RECONSTRUCTIONS, not confirmed broker positions —
   broker_order_id is set to the trading SYMBOL because no order exists.

   Unmarked, they entered the live book as real exposure. `can_open_trade` counts
   `active_positions` excluding ghosts, so three reconstructed lifecycles consumed half of
   MAX_CONCURRENT_POSITIONS=6 before a single real trade was placed. The block is guarded
   on `mode == "HISTORICAL"` and .env pins TRADING_MODE to HISTORICAL, so it runs on EVERY
   launch — including the one that arms REAL at 09:00.
"""
import ast
import inspect
import textwrap

import pytest


# ── 1. the cascade add is offered upstream too ───────────────────────────────

def _process_best_signal_src():
    from engine.signal_processor import SignalProcessor

    return textwrap.dedent(inspect.getsource(SignalProcessor.process_best_signal))


def test_the_upstream_duplicate_gate_offers_the_add():
    src = _process_best_signal_src()
    assert "_offer_cascade_add" in src, (
        "a same-timeframe duplicate is still discarded before the add is ever offered, "
        "which is the commonest cascade shape"
    )


def test_the_upstream_offer_is_gated_on_auto_mode():
    """scale_in_trade reaches broker.place_order; manual mode must approve entries."""
    src = _process_best_signal_src()
    tree = ast.parse(src)
    offers = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "adder"
    ]
    assert offers, "the cascade adder is not called upstream"
    assert "auto_mode" in src, "the upstream cascade add is not gated on auto mode"


def test_the_upstream_offer_tolerates_a_scanner_without_the_seam():
    """Partial scanners are a real pattern; a missing attribute must not raise."""
    src = _process_best_signal_src()
    assert 'getattr(scanner, "_offer_cascade_add", None)' in src


def test_a_successful_add_does_not_also_open_a_trade():
    """The whole point: one more lot INSTEAD of a second strike."""
    src = _process_best_signal_src()
    marker = "CASCADE_ADD"
    assert marker in src
    window = src[src.index(marker):src.index(marker) + 200]
    assert "return []" in window, "an accepted add still let a new trade through"


def test_the_scanner_side_seam_still_exists():
    """Both gates must offer it; this one was fixed first."""
    from scanner import Scanner

    assert "_offer_cascade_add" in inspect.getsource(Scanner._coordinate_and_execute)


# ── 2. reconstructed rows are ghosts ─────────────────────────────────────────

def _reconstruction_branch():
    """The `if self.mode == "HISTORICAL":` block in Scanner.__init__."""
    from scanner import Scanner

    tree = ast.parse(textwrap.dedent(inspect.getsource(Scanner.__init__)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        dumped = ast.dump(node.test)
        if "HISTORICAL" in dumped and "mode" in dumped:
            return node
    raise AssertionError("the session-ledger reconstruction branch was not found")


def _trade_call(branch):
    for n in ast.walk(branch):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Trade":
            return n
    raise AssertionError("no Trade(...) construction in the reconstruction branch")


def test_reconstructed_rows_are_marked_as_ghosts():
    kwargs = {k.arg: k.value for k in _trade_call(_reconstruction_branch()).keywords}
    assert "is_ghost" in kwargs, (
        "session-ledger reconstructions enter the live book as real exposure and eat the "
        "concurrency budget"
    )
    assert isinstance(kwargs["is_ghost"], ast.Constant)
    assert kwargs["is_ghost"].value is True


def test_reconstructed_rows_record_where_they_came_from():
    kwargs = {k.arg: k.value for k in _trade_call(_reconstruction_branch()).keywords}
    assert "data_provenance" in kwargs


def test_the_position_cap_really_does_exclude_ghosts():
    """The premise. If can_open_trade stops excluding them, this fix stops working."""
    from trading.trade_manager import TradeManager

    src = inspect.getsource(TradeManager.can_open_trade)
    assert 'is_ghost' in src and "active_positions" in src


def test_a_ghost_does_not_consume_a_position_slot():
    import threading
    from types import SimpleNamespace

    from trading.trade_manager import TradeManager

    m = TradeManager.__new__(TradeManager)
    m._lock = threading.RLock()
    m.orphan_positions = {}
    m._effective_max_positions = lambda: 2
    m.open_trades = {
        f"g{i}": SimpleNamespace(is_ghost=True, status="OPEN") for i in range(5)
    }
    ok, reason = TradeManager.can_open_trade(m)
    assert ok is True, f"five ghosts blocked a real entry: {reason}"


def test_real_positions_still_consume_the_cap():
    import threading
    from types import SimpleNamespace

    from trading.trade_manager import TradeManager

    m = TradeManager.__new__(TradeManager)
    m._lock = threading.RLock()
    m.orphan_positions = {}
    m._effective_max_positions = lambda: 2
    m.open_trades = {
        f"r{i}": SimpleNamespace(is_ghost=False, status="OPEN") for i in range(2)
    }
    ok, reason = TradeManager.can_open_trade(m)
    assert ok is False and "Max positions" in reason
