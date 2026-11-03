"""Fixes from the 2026-07-08 crash-day forensics: big synchronized directional
moves must not stay blocked once the evidence confirms.

Fix A — cascade chase extension: the missed-reentry chase cap (0.75 ATR from the
original signal price) kept the 13:35 SENSEX seed out of a -1,185pt crash — by
the time a one-way cascade CONFIRMS, price has always moved past 0.75 ATR.
Cascade-confirmed seeds may chase up to the cascade cap (default 2.0 ATR),
still scalp-locked.

Fix B — census-contradiction yield: the session trend-phase guard vetoed six
midcap SELLs (11:19-12:36) on a stale TREND_DAY_UP label while the session's own
directional census was already 5L/10S bearish; midcap then fell 1.9%. When the
census opposes the label with dominance, the guard yields.
"""
from datetime import datetime
from types import SimpleNamespace

from config.settings import get_settings
from engine.signal_processor import session_trend_guard_census_yield
from scanner import Scanner


# ── Fix B: census yield ─────────────────────────────────────────────────────

def test_census_yield_frees_the_2026_07_08_midcap_sells():
    settings = get_settings()
    # The exact 11:26 tape: census 5L/10S, label TREND_DAY_UP, signal SELL.
    reason = session_trend_guard_census_yield(
        settings,
        census={"LONG": 5, "SHORT": 10},
        signal_type="SELL",
        expected="BUY",
        day_type="TREND_DAY_UP",
    )
    assert reason, "guard must yield on 5L/10S census vs TREND_DAY_UP label"
    assert "10S" in reason and "5L" in reason
    assert "TREND_DAY_UP" in reason


def test_census_yield_respects_min_total_and_dominance():
    settings = get_settings()
    # Too few votes -> guard keeps its veto.
    assert session_trend_guard_census_yield(
        settings, census={"LONG": 2, "SHORT": 3}, signal_type="SELL", expected="BUY"
    ) == ""
    # Enough votes but no dominance (7S vs 5L < 1.5x) -> veto stays.
    assert session_trend_guard_census_yield(
        settings, census={"LONG": 5, "SHORT": 7}, signal_type="SELL", expected="BUY"
    ) == ""
    # Exactly at dominance boundary (9 >= 6*1.5) -> yields.
    assert session_trend_guard_census_yield(
        settings, census={"LONG": 6, "SHORT": 9}, signal_type="SELL", expected="BUY"
    ) != ""
    # Signal on the label side -> not the guard's case, no yield claim.
    assert session_trend_guard_census_yield(
        settings, census={"LONG": 5, "SHORT": 10}, signal_type="BUY", expected="BUY"
    ) == ""


def test_census_yield_is_wired_into_the_trend_guard():
    import ast
    import inspect
    import textwrap
    from engine.signal_processor import SignalProcessor
    tree = ast.parse(textwrap.dedent(inspect.getsource(SignalProcessor.process_best_signal)))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    yields = [node.lineno for node in calls if isinstance(node.func, ast.Name)
              and node.func.id == 'session_trend_guard_census_yield']
    vetoes = [node.lineno for node in calls if isinstance(node.func, ast.Name)
              and node.func.id == '_build_rejected_candidate'
              and any(isinstance(arg, ast.Constant) and arg.value == 'REJECTED - Session Trend Guard'
                      for arg in node.args)]
    # An earlier retry-status allowlist contains the same label; it is not a veto.
    assert yields and vetoes
    assert max(yields) < min(vetoes)


# ── Fix A: cascade chase extension ──────────────────────────────────────────

def _chase_scanner(spot, atr):
    sc = Scanner.__new__(Scanner)
    sc._missed_reentry_current_spot = lambda *a, **k: spot
    sc._missed_reentry_atr = lambda *a, **k: atr
    return sc


def _seed(source_price):
    return SimpleNamespace(
        instrument="SENSEX", timeframe="5min",
        source_signal_price=source_price, entry_spot=source_price,
    )


def test_cascade_chase_admits_the_2026_07_08_sensex_seed():
    """Seed at 77,595 (13:35 block); cascade confirms with spot 77,445
    (moved 150, ATR 100). Normal cap 75 -> blocked; cascade cap 200 -> admitted."""
    now = datetime(2026, 7, 8, 13, 39)
    sc = _chase_scanner(spot=77445.0, atr=100.0)
    seed = _seed(77595.0)

    ok_normal, reason_normal, d1 = sc._missed_reentry_chase_check(seed, "SELL", now)
    assert ok_normal is False and "chase limit" in reason_normal

    ok_cascade, _, d2 = sc._missed_reentry_chase_check(
        seed, "SELL", now, cascade_confirmed=True
    )
    assert ok_cascade is True
    assert d2["cascade_confirmed"] is True
    assert d2["max_chase"] > d1["max_chase"]


def test_cascade_chase_still_bounded_and_direction_checked():
    now = datetime(2026, 7, 8, 13, 39)
    # Moved 250 with ATR 100 -> beyond even the 2.0-ATR cascade cap: blocked.
    sc = _chase_scanner(spot=77345.0, atr=100.0)
    ok, reason, _ = sc._missed_reentry_chase_check(
        _seed(77595.0), "SELL", now, cascade_confirmed=True
    )
    assert ok is False and "chase limit" in reason
    # Move reversed against the signal -> blocked regardless of cascade.
    sc2 = _chase_scanner(spot=77700.0, atr=100.0)
    ok2, reason2, _ = sc2._missed_reentry_chase_check(
        _seed(77595.0), "SELL", now, cascade_confirmed=True
    )
    assert ok2 is False and "no longer in signal direction" in reason2


def test_support_context_threads_cascade_flag_into_chase():
    import inspect
    src = inspect.getsource(Scanner._missed_reentry_support_context)
    assert 'cascade_confirmed=(support_type == "one_way_cascade")' in src
