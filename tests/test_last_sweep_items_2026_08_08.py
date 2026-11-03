"""The last four price-basis sweep items.

1. STRIKE SNAP WAS UNBOUNDED. Both option token resolvers picked idxmin() on
   |listed - requested| with no limit, so ANY number resolved to a real, tradeable
   contract. A caller passing an option PREMIUM (~200) instead of a spot level (~24,700)
   got the LOWEST listed NIFTY strike -- deep ITM, premium in the thousands -- wrapped in
   a valid symbol and token, and the order pipeline proceeded normally.

2. STRIKE SELECTION FELL BACK TO THE CONTRACT PRICE. `spot = entry_spot or candidate.price`
   and `strike = round(spot / interval) * interval`. candidate.price is a futures LTP on a
   FUT candidate (spot plus basis -> a strike two steps off) or a premium on an OPT one
   (~120 -> a NIFTY "strike" of 100, which finding 1 then snapped to a real contract).

3. THE FROZEN 1R WAS UNREACHABLE. `if frozen > 0 and qty is None` -- but both hot callers
   pass qty explicitly, so 1R re-based on every scaled-in trade, which is precisely what
   the freeze exists to prevent.

4. THE PER-LOT LADDER RESCALED ONLY THE TIER SELECTOR. Four other consumers compared the
   RAW whole-position peak against the same Rs.3,000 rung, so a 3-lot trade peaking at
   Rs.4,500 was SMART_PROFIT_LOCK in one place and MAJOR_WIN_GUARD in another.
"""
import ast
import inspect
from engine.exit_cycle import ExitCycleService
from tests.source_views import scanner_runtime_source
import textwrap
from types import SimpleNamespace

import pytest

from engine.expiry_manager import ExpiryManager


# ── 1. the strike snap is bounded ────────────────────────────────────────────

def _em():
    return ExpiryManager.__new__(ExpiryManager)


@pytest.mark.parametrize("index,step", [
    ("NIFTY", 50.0), ("BANKNIFTY", 100.0), ("SENSEX", 100.0), ("MIDCPNIFTY", 25.0)
])
def test_a_neighbouring_strike_is_still_accepted(index, step):
    """The fallback must keep absorbing a genuinely missing intermediate strike."""
    assert _em()._snap_is_sane(index, 24700.0, 24700.0 + step) is True


def test_a_premium_mistaken_for_a_spot_is_refused():
    """The headline: 200 must not resolve to the lowest listed NIFTY strike."""
    assert _em()._snap_is_sane("NIFTY", 200.0, 20000.0) is False


def test_a_far_snap_is_refused():
    assert _em()._snap_is_sane("NIFTY", 24700.0, 25000.0) is False


def test_the_bound_scales_with_the_index_spacing():
    """BANKNIFTY's 100-point spacing may reach further in absolute points than NIFTY's."""
    assert _em()._snap_is_sane("BANKNIFTY", 57900.0, 58200.0) is True
    assert _em()._snap_is_sane("NIFTY", 24700.0, 25000.0) is False


def test_junk_input_is_refused_not_snapped():
    assert _em()._snap_is_sane("NIFTY", "oops", 24700.0) is False


def test_both_resolvers_consult_the_bound():
    src = inspect.getsource(ExpiryManager)
    assert src.count("_snap_is_sane(") >= 3, (
        "a resolver still snaps to the nearest listed strike without a bound"
    )


# ── 2. a strike is never derived from a contract price ───────────────────────

def test_the_forced_conversion_does_not_fall_back_to_contract_price():
    """Three escapes existed, not one: the strike, the futures price and the premium
    estimate all read `spot or candidate.price`. Converting OPT->FUT that way took an
    option premium (~120) as a futures price."""
    from scanner import Scanner

    src = inspect.getsource(Scanner._force_candidate_instrument_type)
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.strip().startswith("#")
    )
    assert "or candidate.price" not in code, (
        "a contract price can still stand in for an index level"
    )
    assert "entry_spot" in code


def test_the_forced_conversion_refuses_without_a_spot():
    """No genuine index level means no strike, not a guessed one."""
    from scanner import Scanner

    tree = ast.parse(textwrap.dedent(inspect.getsource(Scanner._force_candidate_instrument_type)))
    guards = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.If) and "spot" in ast.dump(n.test)
        and any(isinstance(x, ast.Return) for x in ast.walk(n))
    ]
    assert guards, "no early return when the index spot is unavailable"


# ── 3. the frozen 1R survives a scale-in ─────────────────────────────────────

def _scanner_for_risk(frozen, whole_qty):
    from scanner import Scanner

    s = Scanner.__new__(Scanner)
    s._trade_like_quantity = lambda t: whole_qty
    return s


def _trade(frozen=1170.0):
    return SimpleNamespace(frozen_initial_risk_rs=frozen, entry_price=120.0,
                           lots=2, lot_size=65, inst_type="OPT")


def test_the_freeze_holds_when_the_caller_names_the_whole_position():
    """Both hot callers pass qty explicitly; `qty is None` made the freeze unreachable."""
    from scanner import Scanner

    s = _scanner_for_risk(1170.0, 130)
    assert Scanner._trade_like_initial_risk_rs(s, _trade(), qty=130) == pytest.approx(1170.0)


def test_the_freeze_holds_when_no_quantity_is_named():
    from scanner import Scanner

    s = _scanner_for_risk(1170.0, 130)
    assert Scanner._trade_like_initial_risk_rs(s, _trade(), qty=None) == pytest.approx(1170.0)


def test_an_unfrozen_trade_is_unaffected():
    from scanner import Scanner

    s = _scanner_for_risk(0.0, 130)
    s._trade_like_entry_price = lambda t: 0.0
    s._ensure_trade_like_initial_stop = lambda t: 0.0
    assert Scanner._trade_like_initial_risk_rs(s, _trade(frozen=0.0), qty=130) == 0.0


# ── 4. one per-lot tier selector, used everywhere ────────────────────────────

def test_the_ladder_helper_is_off_by_default():
    from scanner import Scanner

    s = Scanner.__new__(Scanner)
    s._setting_enabled = lambda v, d=True: False
    assert Scanner._profit_ladder_pnl(s, SimpleNamespace(), 4500.0) == pytest.approx(4500.0)


def test_the_ladder_helper_divides_per_lot_when_enabled():
    from scanner import Scanner

    s = Scanner.__new__(Scanner)
    s._setting_enabled = lambda v, d=True: True
    s._profit_lock_lot_count = lambda t, q: 3
    assert Scanner._profit_ladder_pnl(s, SimpleNamespace(), 4500.0) == pytest.approx(1500.0)


def test_every_hard_three_thousand_check_goes_through_the_helper():
    """A 3-lot Rs.4,500 peak must not be SMART_PROFIT_LOCK here and MAJOR_WIN there."""
    from scanner import Scanner

    # Scoped to the four PROFIT-LOCK consumers the finding named. A separate net-P&L
    # floor further up has its own 0.80/0.70/0.60 tiers and its own question about
    # per-lot scaling; banning the string globally would have swept that in unexamined.
    for fn in (Scanner._check_session_candidates_breaches_locked, ExitCycleService.update_active_trades):
        code = "\n".join(
            ln for ln in inspect.getsource(fn).splitlines() if not ln.strip().startswith("#")
        )
        assert "peak_pnl >= 3000.0" not in code and "previous_peak >= 3000.0" not in code, (
            f"{fn.__name__} still compares the RAW whole-position peak to 3000"
        )
    assert scanner_runtime_source().count("_profit_ladder_pnl(") >= 4


def test_a_broken_settings_object_does_not_break_the_ladder():
    from scanner import Scanner

    s = Scanner.__new__(Scanner)
    assert Scanner._profit_ladder_pnl(s, SimpleNamespace(), 4500.0) == pytest.approx(4500.0)
    assert Scanner._profit_ladder_pnl(s, SimpleNamespace(), "junk") == 0.0
