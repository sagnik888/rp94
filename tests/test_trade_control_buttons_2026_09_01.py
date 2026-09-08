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

Quote-free REAL exits are intentionally supported. A close is a MARKET order, and the
broker fill is authoritative; disabling an emergency exit during the same feed outage
that removed the quote would trap the operator in an open position. The UI may submit a
zero local mark, the scanner permits it only when a broker-backed fill can reconcile it,
and historical/ghost exits still fail closed instead of inventing a fill.
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


def test_the_open_trade_exit_remains_available_without_a_quote(script):
    """A feed outage must not disable a broker-backed emergency MARKET exit."""
    block = _control_cell(script)
    open_trade = block[block.index("if (isOpenTrade)") : block.index("if (isSignalRow)")]

    assert "exitPrice > 0" in open_trade
    assert "Submit MARKET exit; quote unavailable" in open_trade
    assert "disabled" not in open_trade


def test_both_exit_paths_explain_quote_free_market_reconciliation(script):
    block = _control_cell(script)
    assert block.count("Submit MARKET exit; quote unavailable") == 2
    assert "manual_exit_enabled" in block


def test_the_server_accepts_zero_but_refuses_invalid_or_negative_marks():
    from dashboard.server import _valid_command_payload

    base = {"cmd": "close_trade", "trade_id": "T1"}
    assert _valid_command_payload({**base, "price": 0}) is None
    assert _valid_command_payload({**base, "price": -0.01}) == "invalid_price"
    assert _valid_command_payload({**base, "price": float("nan")}) == "invalid_price"
    assert _valid_command_payload({**base, "price": "bad"}) == "invalid_price"


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
