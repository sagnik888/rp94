"""Dynamic trade budget: trend phases earn re-entries, range phases lose them."""

import inspect

import pytest

from config.settings import get_settings
from scanner import Scanner


@pytest.fixture(autouse=True)
def _stable_budget_settings():
    settings = get_settings()
    previous_engine = getattr(settings, "scanner_engine", "utbot_consensus")
    settings.scanner_engine = "utbot_consensus"
    yield
    settings.scanner_engine = previous_engine


def _scanner_with_day(day_type, confidence, base=5):
    scanner = Scanner.__new__(Scanner)
    scanner.max_trades_per_index = base
    scanner._cached_timeframe_intel = lambda *a, **k: {
        "session_context": {"day_type": day_type, "confidence": confidence}
    }
    return scanner


def test_trend_day_earns_re_entry_bonus():
    budget, basis = _scanner_with_day("TREND_DAY_UP", 0.80)._dynamic_trade_budget("NIFTY")
    assert budget == 8
    assert "trend-phase" in basis

    budget, _ = _scanner_with_day("TREND_DAY_DOWN", 0.80)._dynamic_trade_budget("NIFTY")
    assert budget == 8


def test_range_phase_trims_budget():
    budget, basis = _scanner_with_day("RANGE_DAY", 0.80)._dynamic_trade_budget("NIFTY")
    assert budget == 3
    assert "range-phase" in basis
    # Floor: never below 2 even with a tiny base.
    budget, _ = _scanner_with_day("RANGE_40MIN", 0.80, base=3)._dynamic_trade_budget("NIFTY")
    assert budget == 2


def test_unconfirmed_day_uses_static_base():
    budget, basis = _scanner_with_day("UNDECIDED", 0.40)._dynamic_trade_budget("NIFTY")
    assert budget == 5
    assert basis == "static"
    # Trend label without enough confidence also stays static.
    budget, basis = _scanner_with_day("TREND_DAY_UP", 0.40)._dynamic_trade_budget("NIFTY")
    assert budget == 5
    assert basis == "static"


def test_toggle_restores_static_budget():
    settings = get_settings()
    previous = getattr(settings, "dynamic_trade_budget_enabled", True)
    settings.dynamic_trade_budget_enabled = False
    try:
        budget, basis = _scanner_with_day("TREND_DAY_UP", 0.90)._dynamic_trade_budget("NIFTY")
        assert budget == 5
        assert basis == "static"
    finally:
        settings.dynamic_trade_budget_enabled = previous


def test_daily_gate_uses_dynamic_budget():
    source = inspect.getsource(Scanner._can_trade_instrument)
    assert "_dynamic_trade_budget" in source
    assert "budget_basis" in source


def test_multi_entry_engine_raises_budget():
    settings = get_settings()
    previous = settings.scanner_engine
    settings.scanner_engine = "quad_prime"
    try:
        # Multi-entry engine on a normal day: 5 + 4.
        budget, basis = _scanner_with_day("UNDECIDED", 0.40)._dynamic_trade_budget("NIFTY")
        assert budget == 9
        assert "multi-engine" in basis
        # Quad-prime trend phase stacks both bonuses: 5 + 4 + 3 = 12.
        budget, basis = _scanner_with_day("TREND_DAY_UP", 0.80)._dynamic_trade_budget("NIFTY")
        assert budget == 12
        assert "multi-engine" in basis and "trend-phase" in basis
        # Confirmed range/chop phase tightens regardless of engine: 5 - 2 = 3.
        budget, basis = _scanner_with_day("RANGE_DAY", 0.80)._dynamic_trade_budget("NIFTY")
        assert budget == 3
        assert "range-phase" in basis
        # The 20 ceiling holds even with a huge operator base.
        budget, _ = _scanner_with_day("TREND_DAY_UP", 0.80, base=18)._dynamic_trade_budget("NIFTY")
        assert budget == 20
    finally:
        settings.scanner_engine = previous
