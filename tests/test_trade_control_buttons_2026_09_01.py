"""The CTRL column's ENTRY and EXIT buttons must be wired end to end, and honest.

Audited 2026-09-01 at the operator's request. The chain is:

    button (data-action)
      -> delegated click handler (CSP-safe; inline onclick is blocked by script-src)
        -> closeTrade() / executeSignal()
          -> window.sendCommand -> websocket
            -> _valid_command_payload -> command dispatch
              -> Scanner.close_dashboard_trade / Scanner.execute_manual_signal

Every link was verified present. ENTRY is sound: execute_manual_signal returns ten
distinct refusal reasons (auto_mode_enabled, historical_mode, signal_not_found,
already_submitted, already_open, instrument_pref_*, quote_unavailable, a policy reason,
trade_manager_rejected) and notifyTradeCommandResult surfaces each one in the toast, so
a blocked entry always says why.

THE ONE GAP FOUND: the open-trade EXIT button had no disabled state and sent
`Number(row.current_price) || 0`. The server refuses price <= 0 with "invalid_price", so
whenever a trade had no live price -- a stalled or degraded feed, which this build has
had -- the operator was shown a clickable EXIT on an OPEN POSITION that could only fail,
reporting "Exit blocked: invalid_price".

The manual-signal EXIT immediately below it already guarded exactly this
(`manualExitPrice > 0`, disabled, reason in the tooltip). The two paths now match.
"""
import re
from pathlib import Path

import pytest

SCRIPT = Path("dashboard/static/js/trade-table.js")
SERVER = Path("dashboard/server.py")


@pytest.fixture(scope="module")
def script():
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def server():
    return SERVER.read_text(encoding="utf-8")


def _control_cell(script):
    start = script.index("function renderTradeControlCell")
    return script[start : script.index("\nfunction ", start + 10)]


def test_the_open_trade_exit_refuses_to_offer_itself_without_a_price(script):
    """The gap: an EXIT on an open position that the server can only reject."""
    block = _control_cell(script)
    open_trade = block[block.index("if (isOpenTrade)") : block.index("if (isSignalRow)")]

    assert "disabled" in open_trade, (
        "the open-trade EXIT button has no disabled state, so it stays clickable with "
        "no live price and fails server-side with invalid_price"
    )
    assert "exitPrice > 0" in open_trade, "the price is not guarded before offering EXIT"
    assert "Exit unavailable" in open_trade, "a disabled EXIT gives the operator no reason"


def test_both_exit_paths_guard_the_price_the_same_way(script):
    """They sit ten lines apart and disagreed; that is how the gap survived."""
    block = _control_cell(script)
    assert block.count("> 0") >= 2, (
        "only one of the two EXIT paths guards its price"
    )
    assert "manualExitPrice > 0" in block


def test_the_server_still_refuses_a_non_positive_price(server):
    """The UI guard is a courtesy. The server check is the actual protection."""
    assert 'if cmd == "close_trade":' in server
    window = server[server.index('if cmd == "close_trade":') :][:600]
    assert "invalid_price" in window
    assert "price <= 0" in window, (
        "the server no longer refuses a non-positive exit price; the UI guard must not "
        "become the only thing standing between a bad price and an order"
    )


def test_entry_is_offered_only_for_an_accepted_signal(script):
    block = _control_cell(script)
    assert "row.accepted_by_gate" in block, (
        "ENTRY no longer checks that the scanner accepted the signal"
    )
    assert "row.watch_only" in block, "ENTRY is offered on watch-only rows"


def test_both_buttons_reach_a_real_scanner_method(server):
    """A wired button that calls nothing is the failure this audit was asked about."""
    from scanner import Scanner

    assert 'elif cmd == "execute_signal":' in server
    assert 'elif cmd == "close_trade":' in server
    assert hasattr(Scanner, "execute_manual_signal"), (
        "the ENTRY button dispatches to Scanner.execute_manual_signal, which is missing"
    )
    assert hasattr(Scanner, "close_dashboard_trade"), (
        "the EXIT button dispatches to Scanner.close_dashboard_trade, which is missing"
    )


def test_the_click_handler_is_delegated_not_inline(script):
    """A strict script-src CSP silently kills inline onclick; both buttons would die."""
    assert "addEventListener('click'" in script
    for action in ('data-action="close-trade"', 'data-action="execute-signal"'):
        assert action in script, f"{action} is no longer routed by the delegated handler"
    assert "onclick=" not in script, (
        "an inline onclick handler is back; CSP blocks it and the button silently "
        "does nothing"
    )


def test_a_blocked_command_tells_the_operator_why(script, server):
    """Both a validation rejection and a scanner refusal must carry a reason."""
    assert "result.data.reason" in script, (
        "the toast no longer shows the reason, so every failure reads the same"
    )
    assert '"command_rejected", {"cmd": cmd, "reason": validation_error}' in server, (
        "a validation rejection no longer carries its reason to the client"
    )


def test_a_disabled_button_cannot_fire(script):
    """The delegated handler must respect the disabled attribute it renders."""
    handler = script[script.index("addEventListener('click'") :][:700]
    assert "btn.disabled" in handler, (
        "the delegated click handler ignores `disabled`, so a greyed-out EXIT would "
        "still submit"
    )
