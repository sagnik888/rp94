"""Four places that inverted the sign for a bought PE.

This system BUYS options. A bearish view is a bought PE, stored with
direction="SHORT", and it profits when its PREMIUM RISES exactly as a CE does. The live
book holds two OPT shapes only: 54 LONG/CE and 26 SHORT/PE.

Any code that reads `direction` WITHOUT first checking inst_type therefore gets the PE
half of the book backwards. The circuit-breaker instance of this was fixed separately;
these are the rest, found by the same price-basis sweep.

  1. scale-in "is it earned"   -- a LOSING PE read as +1.11R and qualified for an add,
                                  the averaging-down its own docstring forbids
  2. session runner trail      -- abs() on the premium move turned a retracement into a
                                  "gain", locking a stop ABOVE entry and booking a
                                  losing trade as a profit lock
  3. live unrealised P&L       -- a winning PE reported as a loser, feeding the distress
                                  arm and the is_losing flag on the OI/PCR reversal vote
  4. exit-ladder quantity      -- not a sign bug, but the same family of "wrong basis":
                                  P&L sized on INTENDED rather than CONFIRMED fill
"""
import ast
import inspect
import textwrap

import pytest


# ── 1. scale-in must refuse a losing PE ──────────────────────────────────────

def _earned_signed(inst_type, direction, entry, mark):
    """Reproduce the sign decision in _scale_in_is_earned."""
    return (
        (mark - entry)
        if (str(inst_type or "").upper() == "OPT" or str(direction).upper() == "LONG")
        else (entry - mark)
    )


def test_a_losing_bought_pe_shows_a_loss():
    assert _earned_signed("OPT", "SHORT", 120.0, 100.0) == pytest.approx(-20.0)


def test_a_winning_bought_pe_shows_a_profit():
    assert _earned_signed("OPT", "SHORT", 120.0, 140.0) == pytest.approx(20.0)


def test_a_futures_short_still_inverts():
    assert _earned_signed("FUT", "SHORT", 24800.0, 24700.0) == pytest.approx(100.0)


def test_the_scale_in_gate_branches_on_inst_type():
    from trading.trade_manager import TradeManager

    src = inspect.getsource(TradeManager._scale_in_is_earned)
    assert 'inst_type' in src and 'OPT' in src, (
        "the add gate still reads direction alone, so a losing PE qualifies"
    )


def test_the_scale_in_docstring_still_forbids_averaging_down():
    """The premise. If this intent changes, the test above is arguing for nothing."""
    from trading.trade_manager import TradeManager

    doc = inspect.getdoc(TradeManager._scale_in_is_earned) or ""
    assert "averaging down" in doc.lower()


# ── 2. the session runner trail must not lock on an adverse move ─────────────

def _runner_branch():
    from scanner import Scanner

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(Scanner._check_session_candidates_breaches_locked))
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "runner_mode" in ast.dump(node.test):
            return node
    raise AssertionError("session runner-mode branch not found")


def _current_gain_assignment():
    """The `current_gain_pts = ...` statement in the session runner trail.

    Scoped to this ONE assignment on purpose. The enclosing branch also computes
    `current_move = abs(...)` for the runner-ARMING test, and that abs() is correct --
    it is a magnitude, gated by an explicit is_correct_direction check. A test that
    banned abs() across the whole branch would fail against correct code.
    """
    from scanner import Scanner

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(Scanner._check_session_candidates_breaches_locked))
    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(getattr(t, "id", "") == "current_gain_pts" for t in node.targets):
            return node
    raise AssertionError("current_gain_pts assignment not found")


def test_the_session_runner_gain_is_signed_not_absolute():
    """abs() made a retracement look like a gain."""
    node = _current_gain_assignment()
    calls = {
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "abs" not in calls, (
        "abs() on the premium move: a LOSING runner is handed a stop above entry and "
        "booked as a profit lock"
    )
    assert "max" in calls, "an adverse move must floor to zero locked gain"


def test_the_session_runner_gain_reads_the_direction():
    assert "price_direction" in ast.dump(_current_gain_assignment())


def test_the_runner_arming_abs_is_left_alone():
    """That abs() is a magnitude and is gated by is_correct_direction. Do not 'fix' it."""
    from scanner import Scanner

    src = inspect.getsource(Scanner._check_session_candidates_breaches_locked)
    assert "is_correct_direction" in src
    assert "current_move = abs(" in src


def _runner_gain(current, entry, direction="LONG"):
    """Reproduce the corrected gain calculation."""
    return max(0.0, (current - entry) if direction == "LONG" else (entry - current))


def test_an_adverse_move_locks_nothing():
    """Premium 165.05 -> 158 on a bought option is a LOSS, not 7.05 of gain."""
    assert _runner_gain(158.0, 165.05) == 0.0


def test_a_favourable_move_still_locks():
    assert _runner_gain(190.0, 165.05) == pytest.approx(24.95)


# ── 3. live unrealised P&L ───────────────────────────────────────────────────

def test_the_unrealised_fallback_branches_on_inst_type():
    from engine.exit_cycle import ExitCycleService

    src = inspect.getsource(ExitCycleService.update_active_trades)
    marker = "signed = (mark_px - entry_px)"
    assert marker in src
    window = src[src.index(marker):src.index(marker) + 400]
    assert "OPT" in window, "a winning bought PE is still reported as a loser"


# ── 4. exit ladders size on the CONFIRMED fill ───────────────────────────────

def test_the_exit_ladder_uses_the_confirmed_fill_quantity():
    from engine.exit_cycle import ExitCycleService

    src = inspect.getsource(ExitCycleService.update_active_trades)
    assert "pnl_quantity" in src, (
        "the exit ladders size P&L on the INTENDED quantity, so a partial fill arms the "
        "rupee loss band on money that was never at risk"
    )


def test_pnl_quantity_still_prefers_the_broker_fill():
    """The premise that fix rests on."""
    from trading.trade_manager import Trade

    doc = inspect.getdoc(Trade.pnl_quantity.fget) or ""
    assert "broker" in doc.lower() and "intended" in doc.lower()


# ── 5. SENSEX has its own strike interval ────────────────────────────────────

def test_sensex_no_longer_inherits_niftys_strike_interval():
    import inspect as _i

    import engine.apex55.config as cfg

    attr = cfg.ApexConfig.strike_interval
    src = _i.getsource(getattr(attr, 'fget', attr))
    assert '"Sensex"' in src, "SENSEX falls through to NIFTY's 50-point spacing"
    entry = [ln for ln in src.splitlines() if '"Sensex"' in ln and ":" in ln]
    assert entry and "100.0" in entry[0], f"SENSEX spacing is not 100: {entry}"
