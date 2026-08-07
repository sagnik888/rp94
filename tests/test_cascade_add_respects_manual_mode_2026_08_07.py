"""The cascade add reached the broker in MANUAL mode.

When a same-chart duplicate is blocked by the concurrency guard, the system offers it to
the running trade as one more lot. That is the operator's rule and it is right.

But `_offer_cascade_add` is called 51 lines ABOVE the gate that stops every ordinary
entry in manual mode:

    if not self.auto_mode and not is_warmup:
        ...
        continue   # Don't take the trade in manual mode

and nothing below it re-checks. `trading/trade_manager.py` contains NO reference to
`auto_mode` anywhere, and `scale_in_trade` branches only on `self.mode == "REAL"` before
`_run_scale_in` places a live BUY market order on the running trade's contract.

So an operator running with auto mode off, expecting to approve every entry from the
toast, gets a lot added to a live position with no approval at all.

It is inert TODAY only because `scale_in_enabled` is False — which is precisely the
switch this feature was built to have turned on. This is a landmine, not a live bug, and
the time to defuse it is before the switch is flipped.
"""
import ast
import inspect
import textwrap

import pytest

from scanner import Scanner


def _concurrency_block():
    """The `if not self._prepare_candidate_for_concurrency(sig):` body, as an AST node."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(Scanner._coordinate_and_execute)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if "_prepare_candidate_for_concurrency" in ast.dump(node.test):
            return node
    raise AssertionError("the concurrency-guard block was not found")


def _auto_mode_guards(node):
    """Every `if not self.auto_mode ...` inside this node."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.If) and "auto_mode" in ast.dump(n.test):
            out.append(n)
    return out


def test_the_cascade_add_is_behind_an_auto_mode_gate():
    guards = _auto_mode_guards(_concurrency_block())
    assert guards, (
        "the cascade add runs before the manual-mode gate and places a live broker "
        "order with no operator approval"
    )


def test_the_gate_is_evaluated_before_the_add_is_offered():
    """A gate below the call would not stop the order."""
    block = _concurrency_block()
    guard_line = min(g.lineno for g in _auto_mode_guards(block))
    offer_line = min(
        n.lineno for n in ast.walk(block)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_offer_cascade_add"
    )
    assert guard_line < offer_line, "the add is offered before auto mode is checked"


def test_warmup_is_treated_like_the_entry_path():
    """The entry gate is `not auto_mode and not is_warmup`; this must match it."""
    guards = _auto_mode_guards(_concurrency_block())
    assert any("is_warmup" in ast.dump(g.test) for g in guards), (
        "the cascade gate does not match the entry gate's warmup handling"
    )


def test_the_offer_still_happens_in_auto_mode():
    """The feature must survive the fix: auto mode still gets the add."""
    block = _concurrency_block()
    offers = [
        n for n in ast.walk(block)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_offer_cascade_add"
    ]
    assert offers, "the cascade add was removed rather than gated"


def test_trade_manager_still_does_not_know_about_auto_mode():
    """The premise of this test: the gate MUST live at the scanner call site.

    If trade_manager ever learns about auto_mode, this test's reasoning changes and
    someone should re-read it rather than trust the gate above.
    """
    import trading.trade_manager as tm

    src = inspect.getsource(tm)
    assert "auto_mode" not in src, (
        "trade_manager now references auto_mode — re-check where the cascade gate belongs"
    )


def test_the_scale_in_path_really_does_reach_the_broker():
    """Why the gate matters: this is not a paper path.

    scale_in_trade -> _request_broker_scale_in -> _run_scale_in -> broker.place_order,
    with no auto_mode check anywhere along it.
    """
    from trading.trade_manager import TradeManager

    chain = [
        (TradeManager.scale_in_trade, "_request_broker_scale_in"),
        (TradeManager._request_broker_scale_in, "_run_scale_in"),
    ]
    for fn, callee in chain:
        assert callee in inspect.getsource(fn), f"{fn.__name__} no longer calls {callee}"

    run_src = inspect.getsource(TradeManager._run_scale_in)
    assert "self.broker.place_order(" in run_src, (
        "_run_scale_in no longer places a broker order — re-check whether the manual-mode "
        "gate at the scanner call site is still the right place for this guard"
    )
