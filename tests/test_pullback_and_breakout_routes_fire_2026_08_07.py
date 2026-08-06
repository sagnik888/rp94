"""Two entry routes were evaluated on every bar and thrown away.

engine/alt_scanners.py computes three route tests — thrust_ok, breakout_ok, pullback_ok —
and then assigned `route` from thrust_ok ALONE:

    route = ""
    if thrust_ok:
        route = "quality_thrust"
    if not route or not _eligible(route, want, i):
        continue

So a bar that satisfied the pullback conditions (trend intact, 4+ component agreement,
previous candle against the trend, directional close in the top 58% of range, range/ATR
>= 0.55) computed pullback_ok = True and was then skipped by `if not route`. Zero pullback
entries were ever emitted, on any index, on any timeframe. Same for breakouts through this
scanner. The operator's "pullback signals trading" was code that ran and discarded its own
answer.

Strongest evidence first, one route per bar: a thrust that also satisfies the breakout
test is a thrust, not two signals. _eligible keys its cooldown on (route, want), so each
route now gets its own spacing instead of sharing the thrust route's.
"""
import ast
import inspect
import textwrap

import pytest


def _route_block():
    from engine.alt_scanners import AltSignalEngine

    for name in dir(AltSignalEngine):
        attr = getattr(AltSignalEngine, name, None)
        if not callable(attr):
            continue
        try:
            src = inspect.getsource(attr)
        except (OSError, TypeError):
            continue
        if "pullback_ok" in src and "route" in src:
            return src
    raise AssertionError("the quality-hybrid route block could not be located")


def _assigned_routes():
    """Every string literal assigned to `route` in the block."""
    tree = ast.parse(textwrap.dedent(_route_block()))
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "route":
                if isinstance(node.value, ast.Constant) and node.value.value:
                    out.add(node.value.value)
    return out


def test_the_pullback_route_can_actually_be_selected():
    routes = _assigned_routes()
    assert any("pullback" in r for r in routes), (
        "pullback_ok is still computed and discarded — no route is ever assigned from "
        "it, so zero pullback entries can be emitted"
    )


def test_the_breakout_route_can_actually_be_selected():
    routes = _assigned_routes()
    assert any("breakout" in r for r in routes), (
        "breakout_ok is computed and discarded the same way"
    )


def test_the_thrust_route_still_works():
    assert any("thrust" in r for r in _assigned_routes())


def test_all_three_routes_have_distinct_names():
    """_eligible keys its cooldown on (route, want); shared names share a cooldown."""
    routes = _assigned_routes()
    assert len(routes) >= 3, f"expected three distinct routes, found {sorted(routes)}"


def test_one_bar_produces_one_route():
    """A thrust that also satisfies breakout is a thrust, not two signals."""
    tree = ast.parse(textwrap.dedent(_route_block()))
    chains = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        assigns = [
            n for n in node.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "route" for t in n.targets)
        ]
        if assigns and node.orelse:
            chains.append(node)
    assert chains, (
        "the route tests are independent `if`s, so two of them can both assign and the "
        "last one silently wins; they must be an elif chain"
    )


def test_the_three_conditions_are_still_computed():
    """Guard against 'fixing' this by deleting the tests instead of wiring them."""
    block = _route_block()
    for name in ("thrust_ok", "breakout_ok", "pullback_ok"):
        assert f"{name} = (" in block, f"{name} is no longer computed"


def test_the_pullback_conditions_are_unchanged():
    """This wires an existing route up; it must not quietly loosen its bar."""
    block = _route_block()
    start = block.index("pullback_ok = (")
    body = block[start:start + 420]
    for required in ("trend_ok", "previous_against", "directional_candle", "close_loc"):
        assert required in body, f"the pullback test lost its {required} condition"
