"""Dashboard -> Scanner call contract.

Two production outages in one week came from the same shape of defect: a caller was
updated but the callee was not, so the break only appeared at runtime.

  * main.py passed on_notification= to TradeManager.__init__, which did not accept it,
    so create_system() raised and the scanner never started.
  * dashboard/server.py awaited scanner_ref.update_fyers_token_async(...), which did not
    exist (Scanner only had the sync update_fyers_token), so completing the Fyers OAuth
    login failed with "'Scanner' object has no attribute 'update_fyers_token_async'".

These tests assert every scanner_ref.<attr>(...) the dashboard calls actually exists on
Scanner, and that the ones the server awaits on the scanner loop are coroutines.
"""
import ast
import inspect
import re
from pathlib import Path

import pytest

from scanner import Scanner

ROOT = Path(__file__).resolve().parents[1]
SERVER_SRC = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")


def _scanner_ref_attributes() -> set[str]:
    """Every attribute the dashboard invokes on scanner_ref."""
    tree = ast.parse(SERVER_SRC)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "scanner_ref":
            names.add(fn.attr)
    return names


def test_every_dashboard_call_exists_on_scanner():
    missing = sorted(a for a in _scanner_ref_attributes() if not hasattr(Scanner, a))
    assert missing == [], (
        f"dashboard/server.py calls scanner_ref.{{{', '.join(missing)}}} which do not "
        "exist on Scanner - this fails only at runtime"
    )


def test_fyers_token_update_is_awaitable():
    """The auth callback awaits this on the scanner loop, so it must be a coroutine."""
    assert hasattr(Scanner, "update_fyers_token_async")
    assert inspect.iscoroutinefunction(Scanner.update_fyers_token_async)


def test_awaited_scanner_calls_are_coroutines():
    """Anything handed to _await_on_scanner_loop must be a coroutine function."""
    awaited = set(re.findall(r"_await_on_scanner_loop\(\s*scanner_ref\.(\w+)\(", SERVER_SRC))
    assert awaited, "expected at least one awaited scanner call to guard"
    not_coro = sorted(
        name for name in awaited
        if hasattr(Scanner, name) and not inspect.iscoroutinefunction(getattr(Scanner, name))
    )
    assert not_coro == [], (
        f"_await_on_scanner_loop() is given non-coroutine scanner call(s): {not_coro}"
    )


def test_trade_manager_accepts_the_arguments_main_passes():
    """Guards the create_system() startup crash: constructor must accept its caller's
    keyword arguments."""
    from trading.trade_manager import TradeManager

    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_src)
    kwargs: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "TradeManager"):
            kwargs |= {k.arg for k in node.keywords if k.arg}
    assert kwargs, "expected main.py to construct TradeManager with keyword arguments"
    params = inspect.signature(TradeManager.__init__).parameters
    unexpected = sorted(k for k in kwargs if k not in params)
    assert unexpected == [], f"TradeManager.__init__ does not accept: {unexpected}"
