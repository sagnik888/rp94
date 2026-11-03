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
from engine.execution_service import ExecutionService


def _concurrency_block():
    """The `if not self._prepare_candidate_for_concurrency(sig):` body, as an AST node."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(ExecutionService.coordinate_and_execute)))
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


def test_trade_manager_never_BRANCHES_on_auto_mode():
    """The gate MUST stay at the scanner call site.

    This began life as `"auto_mode" not in src` -- a tripwire whose own docstring said
    that if trade_manager ever learned about auto_mode, someone should re-read the
    reasoning rather than trust the gate. On 2026-09-01 it fired, and the reasoning was
    re-read.

    What changed: forensics needs to stamp each row AUTO or MANUAL, because recording
    only auto-executed trades left an operator running manually with a permanently empty
    flight recorder. So `_record_trade_forensics` reads auto_mode as a LABEL.

    That does not touch this test's concern. The hazard is `scale_in_trade` placing a
    live BUY on a running position with no operator approval, which requires auto_mode to
    be part of a DECISION. So the assertion is now the property that actually matters:
    auto_mode may be READ, but never branched on.

    Honest accounting of the trade, because an earlier version of this docstring claimed
    the new form was "stronger" and that was checked and found false. Compared case by
    case against the old string check:

        forensics label (the exception)   old REJECTS   new allows   <- the only change
        `if not self.auto_mode: return`   old rejects   new rejects
        a bare read outside forensics     old rejects   new rejects (via the test below)
        a gate under a different name     old ALLOWS    new ALLOWS   <- neither catches

    So this is not stricter. It is the old guard minus one documented exception, with
    test_auto_mode_in_trade_manager_is_confined_to_forensics covering the gap that
    removing the blanket string check opened. Both were verified by inserting the
    offending code and watching them fail.
    """
    import trading.trade_manager as tm

    tree = ast.parse(textwrap.dedent(inspect.getsource(tm)))
    branches = []
    for node in ast.walk(tree):
        test = getattr(node, "test", None)
        if test is None:
            continue
        if isinstance(node, ast.IfExp):
            continue  # a ternary picking a label is not control flow
        if "auto_mode" in ast.dump(test):
            branches.append(type(node).__name__)

    assert not branches, (
        "trade_manager now BRANCHES on auto_mode "
        f"({', '.join(branches)}) — the manual-mode gate must stay at the scanner call "
        "site, or a cascade add can reach the broker without operator approval"
    )


def test_auto_mode_in_trade_manager_is_confined_to_forensics():
    """And it may only be read where it was allowed in, not spread further."""
    import trading.trade_manager as tm

    source = inspect.getsource(tm).splitlines()
    users = []
    current = ""
    for line in source:
        stripped = line.strip()
        if stripped.startswith("def ") or stripped.startswith("async def "):
            current = stripped.split("(")[0].replace("async def ", "").replace("def ", "")
        if "auto_mode" in line:
            users.append(current)

    assert set(users) <= {"_record_trade_forensics"}, (
        f"auto_mode is now read in {sorted(set(users))}; it was admitted to "
        "trade_manager for the forensics stamp only"
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
