"""Every configured repaint mitigation was unreachable from the live guard.

Three settings soften a post-entry repaint abort:

  * repaint_abort_skip_when_profitable   — hold a repaint that is already in profit
  * option_repaint_abort_defer_until_close — let a fresh option ride intrabar flicker
  * a stabilization momentum override    — honour the override the entry was granted

All three had call sites in exactly one place: `_check_session_candidates_breaches_locked`,
which manages dashboard/session TradeCandidate rows and never a broker position. The async
tracker `_track_repaint_until_candle_close`, the only guard that touches a real trade,
consulted none of them and market-exited immediately.

So a bought CE up Rs 3,000 that flickered once on the 20s tick was held by the paper
mirror and closed for real. The operator's .env policy applied to a dashboard row.

The profit test could NOT reuse the session helper: `_repaint_abort_held_for_profit` routes
through `_candidate_gross_pnl`, which reads `candidate.price`, and a Trade carries
`entry_price`. Passing a trade raises inside that helper's try/except and it silently
answers False — wired, and never firing. A trade-shaped test honours the same setting.

The mitigation block fails SAFE: if it cannot be evaluated the abort proceeds, because the
tracker's whole body sits under one `except Exception` and an AttributeError in here would
otherwise swallow the abort. An existing test caught exactly that on the first attempt.
"""
import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

from scanner import Scanner


def _scanner(profitable_setting=True):
    s = Scanner.__new__(Scanner)
    s._setting_enabled = lambda value, default=True: bool(value)
    return s


def _trade(entry=100.0, inst_type="OPT", direction="LONG"):
    return SimpleNamespace(
        id="T1", entry_price=entry, inst_type=inst_type, direction=direction
    )


# ── the trade-shaped profit test ─────────────────────────────────────────────

def test_a_bought_option_in_profit_is_held():
    assert Scanner._repaint_abort_profit_hold(_scanner(), _trade(), 130.0) is True


def test_a_bought_option_at_a_loss_is_not_held():
    assert Scanner._repaint_abort_profit_hold(_scanner(), _trade(), 80.0) is False


def test_a_bought_put_profits_on_its_own_premium():
    """Options are always BOUGHT; a PE is not a short and must not invert."""
    put = _trade(inst_type="OPT", direction="SHORT")
    assert Scanner._repaint_abort_profit_hold(_scanner(), put, 130.0) is True


def test_a_futures_short_inverts():
    fut = _trade(entry=24800.0, inst_type="FUT", direction="SHORT")
    assert Scanner._repaint_abort_profit_hold(_scanner(), fut, 24700.0) is True
    assert Scanner._repaint_abort_profit_hold(_scanner(), fut, 24900.0) is False


def test_the_operator_can_switch_it_off():
    s = Scanner.__new__(Scanner)
    s._setting_enabled = lambda value, default=True: False
    assert Scanner._repaint_abort_profit_hold(s, _trade(), 130.0) is False


@pytest.mark.parametrize("bad", [0.0, -1.0, None])
def test_an_unusable_mark_or_entry_does_not_hold(bad):
    assert Scanner._repaint_abort_profit_hold(_scanner(), _trade(), bad) is False
    assert Scanner._repaint_abort_profit_hold(_scanner(), _trade(entry=bad), 130.0) is False


def test_the_session_helper_would_have_silently_failed_on_a_trade():
    """Why a separate helper exists. If this ever starts working, simplify."""
    s = Scanner.__new__(Scanner)
    s._setting_enabled = lambda value, default=True: True
    s._candidate_gross_pnl = Scanner._candidate_gross_pnl.__get__(s)
    s._is_premium_long_candidate = lambda c: False
    assert Scanner._repaint_abort_held_for_profit(s, _trade(), 130.0) is False, (
        "the candidate-shaped helper now works on a Trade — re-check the duplication"
    )


# ── the wiring: the live tracker consults all three ──────────────────────────

def _tracker_calls():
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(Scanner._track_repaint_until_candle_close))
    )
    return {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }


@pytest.mark.parametrize(
    "helper",
    [
        "_post_entry_repaint_override_context",
        "_repaint_abort_profit_hold",
        "_should_defer_option_repaint_abort",
    ],
)
def test_the_live_tracker_consults_each_mitigation(helper):
    assert helper in _tracker_calls(), (
        f"{helper} is still reachable only from the session mirror, so the operator's "
        f"policy does not apply to real money"
    )


def test_the_abort_still_exists():
    """Softening must not become never aborting."""
    src = inspect.getsource(Scanner._track_repaint_until_candle_close)
    assert "REPAINT_ABORT" in src
    assert "_close_and_record" in src


def test_the_mitigation_block_fails_safe():
    """An unevaluable mitigation must let the abort proceed, not swallow it."""
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(Scanner._track_repaint_until_candle_close))
    )
    handlers = [
        h
        for n in ast.walk(tree)
        if isinstance(n, ast.Try)
        for h in n.handlers
        if any(
            isinstance(x, ast.Assign)
            and any(getattr(t, "id", "") == "hold_reason" for t in x.targets)
            for x in ast.walk(h)
        )
    ]
    assert handlers, "a failing mitigation check can still swallow the abort"
