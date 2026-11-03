"""MACD acceleration decides how patient an entry bid can be.

The broker chose MARKET or LIMIT on CONFIDENCE alone — "a limit is only meaningful when
the conviction is high enough that missing the fill beats paying the spread". That weighs
how GOOD the setup is and never how fast the premium is leaving the bid, so a strong
signal into an accelerating move rested a limit behind the market.

Urgency RAISES the confidence bar a limit must clear. It never lowers it, so this can only
make the system less willing to wait, never more — a missed fill and a paid spread are
both real costs, and only one of them is reversible.

DEFAULT OFF: it changes fills.
"""
from types import SimpleNamespace

import pytest

from engine.order_pricing import limit_confidence_penalty, momentum_urgency
from engine.execution_service import ExecutionService


def _s(on=True, penalty=0.15):
    return SimpleNamespace(
        macd_aware_entry_pricing=on, macd_entry_urgency_max_penalty=penalty
    )


# ── reading the histogram ────────────────────────────────────────────────────

def test_an_expanding_histogram_with_the_trade_is_urgent():
    """The move is still accelerating; a resting limit gets left behind."""
    assert momentum_urgency(12.0, 6.0, "LONG") == pytest.approx(0.5)


def test_a_contracting_histogram_is_not_urgent():
    """Still positive but decelerating — patience is cheap."""
    assert momentum_urgency(6.0, 12.0, "LONG") == 0.0


def test_momentum_against_the_trade_creates_no_urgency():
    """That is a reason to reconsider the entry, not to pay the spread faster."""
    assert momentum_urgency(-12.0, -6.0, "LONG") == 0.0
    assert momentum_urgency(12.0, 6.0, "SHORT") == 0.0


def test_a_short_reads_a_falling_histogram_as_its_favour():
    assert momentum_urgency(-12.0, -6.0, "SHORT") == pytest.approx(0.5)


def test_a_flat_histogram_is_not_urgent():
    assert momentum_urgency(8.0, 8.0, "LONG") == 0.0


def test_urgency_is_scale_free_across_indices():
    """A NIFTY histogram of 12 and a MIDCPNIFTY one of 40 must read the same.

    Per-index constants are how several thresholds in this system ended up wrong.
    """
    assert momentum_urgency(12.0, 6.0, "LONG") == pytest.approx(
        momentum_urgency(40.0, 20.0, "LONG")
    )


def test_it_is_bounded_to_one():
    assert momentum_urgency(1000.0, 0.001, "LONG") <= 1.0


def test_junk_readings_are_not_urgent():
    assert momentum_urgency(None, None, "LONG") == 0.0
    assert momentum_urgency("fast", 1.0, "LONG") == 0.0
    assert momentum_urgency(0.0, 0.0, "") == 0.0


# ── turning urgency into a threshold shift ───────────────────────────────────

def test_it_is_inert_unless_switched_on():
    assert limit_confidence_penalty(1.0, _s(on=False)) == 0.0
    assert limit_confidence_penalty(1.0, SimpleNamespace()) == 0.0


def test_full_urgency_costs_the_whole_configured_penalty():
    assert limit_confidence_penalty(1.0, _s(penalty=0.15)) == pytest.approx(0.15)


def test_half_urgency_costs_half():
    assert limit_confidence_penalty(0.5, _s(penalty=0.15)) == pytest.approx(0.075)


def test_no_urgency_leaves_the_threshold_untouched():
    assert limit_confidence_penalty(0.0, _s()) == 0.0


def test_the_penalty_is_bounded():
    assert limit_confidence_penalty(1.0, _s(penalty=5.0)) <= 0.5


def test_the_penalty_can_never_be_negative():
    """A negative would LOWER the bar and make the system more willing to rest a limit."""
    assert limit_confidence_penalty(-1.0, _s()) == 0.0
    assert limit_confidence_penalty(1.0, _s(penalty=-1.0)) == 0.0


# ── the wiring, end to end ───────────────────────────────────────────────────

def test_the_broker_applies_the_penalty_to_its_threshold():
    import inspect

    from trading.kite_broker import KiteBroker

    src = inspect.getsource(KiteBroker._submit_entry)
    assert "momentum_urgency" in src
    assert "limit_confidence_penalty" in src
    assert "threshold +=" in src, "the penalty is computed and not applied"


def test_place_order_forwards_it():
    import inspect

    from trading.kite_broker import KiteBroker

    assert "momentum_urgency" in inspect.getsource(KiteBroker.place_order)


def test_the_trade_manager_supplies_it():
    import inspect

    from trading.trade_manager import TradeManager

    src = inspect.getsource(TradeManager)
    assert "momentum_urgency=float(" in src
    assert "entry_momentum_urgency" in src


def test_both_live_entry_paths_pass_it():
    """Two open_trade call sites in _coordinate_and_execute; one wired is not wired."""
    import ast
    import inspect
    import textwrap

    from scanner import Scanner

    tree = ast.parse(textwrap.dedent(inspect.getsource(ExecutionService.coordinate_and_execute)))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "open_trade"
    ]
    assert calls, "the live entry path no longer opens trades here"
    for c in calls:
        assert any(k.arg == "entry_momentum_urgency" for k in c.keywords), (
            "a live entry path never supplies the momentum reading"
        )


def test_a_scanner_with_no_macd_history_reports_no_urgency():
    """First bar after a restart must not make every entry take the market."""
    from scanner import Scanner

    s = Scanner.__new__(Scanner)
    s._timeframe_macd = {}
    s._timeframe_intel_key = lambda i, tf: f"{i}_{tf}"
    cand = SimpleNamespace(instrument="NIFTY", timeframe="5min", direction="LONG")
    assert s._entry_momentum_urgency(cand) == 0.0


def test_the_scanner_reads_its_own_timeframe():
    from scanner import Scanner

    s = Scanner.__new__(Scanner)
    s._timeframe_intel_key = lambda i, tf: f"{i}_{tf}"
    s._timeframe_macd = {"NIFTY_1min": (12.0, 6.0), "NIFTY_5min": (6.0, 12.0)}
    accelerating = SimpleNamespace(instrument="NIFTY", timeframe="1min", direction="LONG")
    stalling = SimpleNamespace(instrument="NIFTY", timeframe="5min", direction="LONG")
    assert s._entry_momentum_urgency(accelerating) > 0.0
    assert s._entry_momentum_urgency(stalling) == 0.0
