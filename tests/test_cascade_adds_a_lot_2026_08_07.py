"""A duplicate signal on a chart that is already working adds a lot, not a second trade.

The operator's rule: when the quad/dual scanners fire more than one signal in the SAME
index and SAME timeframe, the system should add ONE LOT to the already-running trade
rather than open a second trade at a different strike or price.

scale_in_trade was built and tested for exactly this and had ZERO production callers, so
the duplicate was simply discarded by the concurrency guard — and on a non-overlapping
quad source it could instead open that second trade, the outcome the rule exists to
prevent.

This wires the seam. Everything about WHETHER an add is allowed still belongs to
scale_in_trade (in profit, with trend, add cap, lot ceiling, rupee budget); this only
decides WHEN to ask, and surfaces the refusal reason.
"""
from types import SimpleNamespace

import pytest

from scanner import Scanner
from engine.execution_service import ExecutionService


def _cand(instrument="NIFTY", tf="5min", direction="LONG"):
    return SimpleNamespace(
        instrument=instrument, timeframe=tf, direction=direction,
        signal_timestamp=None, grade="A", inst_type="OPT",
    )


def _open(instrument="NIFTY", tf="5min", direction="LONG", tid="T1"):
    return SimpleNamespace(
        instrument=instrument, timeframe=tf, direction=direction,
        trading_symbol=f"{instrument}2680724500CE", current_price=240.0,
        entry_price=200.0, id=tid,
    )


def _scanner(open_trades, result=(True, "")):
    s = Scanner.__new__(Scanner)
    calls = []

    def adder(trade_id, add_lots, price, reason):
        calls.append((trade_id, add_lots, price, reason))
        return result

    s.trades = SimpleNamespace(open_trades=dict(open_trades), scale_in_trade=adder)
    s.log_event = lambda *a, **k: None
    s.calls = calls
    return s


def test_a_same_chart_duplicate_adds_a_lot():
    """The operator's headline case."""
    s = _scanner({"T1": _open()})
    added, why = s._offer_cascade_add(_cand())
    assert added is True, f"duplicate was discarded instead of adding a lot ({why})"
    assert s.calls == [("T1", None, 240.0, "CASCADE_ADD")]


def test_the_add_size_is_left_to_the_setting():
    """add_lots=None means scale_in_lots_per_add decides."""
    s = _scanner({"T1": _open()})
    s._offer_cascade_add(_cand())
    assert s.calls[0][1] is None


def test_a_different_timeframe_is_a_separate_idea():
    """4 indices x 3 timeframes is the target shape; a 1min signal is not a 5min add."""
    s = _scanner({"T1": _open(tf="5min")})
    added, _why = s._offer_cascade_add(_cand(tf="1min"))
    assert added is False
    assert s.calls == []


def test_a_different_index_is_never_touched():
    s = _scanner({"T1": _open(instrument="BANKNIFTY")})
    assert s._offer_cascade_add(_cand(instrument="NIFTY"))[0] is False
    assert s.calls == []


def test_the_opposite_direction_is_a_reversal_not_a_reinforcement():
    s = _scanner({"T1": _open(direction="LONG")})
    assert s._offer_cascade_add(_cand(direction="SHORT"))[0] is False
    assert s.calls == []


def test_an_index_suffix_still_matches_the_base():
    """Candidates and trades do not always carry the bare index name."""
    s = _scanner({"T1": _open(instrument="NIFTY 24500 CE")})
    assert s._offer_cascade_add(_cand(instrument="NIFTY"))[0] is True


def test_a_refusal_is_reported_not_swallowed():
    """scale_in_trade owns the rules; its reason has to reach the decision log."""
    s = _scanner({"T1": _open()}, result=(False, "open profit 0.30R below 0.75R"))
    added, why = s._offer_cascade_add(_cand())
    assert added is False
    assert "0.30R" in why


def test_an_empty_book_asks_nobody():
    s = _scanner({})
    assert s._offer_cascade_add(_cand()) == (False, "")
    assert s.calls == []


def test_a_manager_without_scale_in_is_handled():
    """Older/partial managers must not crash the signal path."""
    s = Scanner.__new__(Scanner)
    s.trades = SimpleNamespace(open_trades={"T1": _open()})
    assert s._offer_cascade_add(_cand()) == (False, "")


def test_an_exception_inside_the_add_never_breaks_signal_handling():
    def boom(*a, **k):
        raise RuntimeError("broker down")

    s = Scanner.__new__(Scanner)
    s.trades = SimpleNamespace(open_trades={"T1": _open()}, scale_in_trade=boom)
    s.log_event = lambda *a, **k: None
    assert s._offer_cascade_add(_cand()) == (False, "")


def test_only_the_first_matching_trade_is_offered_the_lot():
    """Two trades on the same chart is itself unusual; do not add to both."""
    s = _scanner({"T1": _open(tid="T1"), "T2": _open(tid="T2")})
    s._offer_cascade_add(_cand())
    assert len(s.calls) == 1


# ── the wiring, not just the helper ──────────────────────────────────────────

def test_the_blocked_duplicate_path_offers_the_add():
    """Three earlier fixes this session tested a helper and missed its call site."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(ExecutionService.coordinate_and_execute)))
    guards = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and any(
            isinstance(c, ast.Attribute) and c.attr == "_prepare_candidate_for_concurrency"
            for c in ast.walk(n.test)
        )
    ]
    assert guards, "the concurrency guard call site moved"
    offered = any(
        isinstance(c, ast.Attribute) and c.attr == "_offer_cascade_add"
        for g in guards for c in ast.walk(g)
    )
    assert offered, (
        "a duplicate blocked by the concurrency guard is still discarded without being "
        "offered to the running trade as an extra lot"
    )
