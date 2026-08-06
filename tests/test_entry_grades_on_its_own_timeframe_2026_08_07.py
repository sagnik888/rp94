"""A 1-minute candidate must not be graded on the 5-minute regime.

self.latest_regimes is keyed by INSTRUMENT and its own declaration says so —
"5min instrument regime for dashboard/legacy gates" (scanner.py:268). Entry grading read
that map directly, so a 1min or 15min candidate carried a regime label computed from
5-minute candles.

The per-timeframe answer was already there. _cache_timeframe_intel writes
_timeframe_regimes[instrument_tf] for every timeframe it computes, and _get_trade_regime
already does the correct cascade — own timeframe, then the 5min label, then UNKNOWN. It
was only ever used for managing trades already open, never at the entry seam.

This matters most exactly when the timeframes disagree, which is when a regime label is
worth having at all: a 1min RANGING pocket inside a 5min STRONG_UPTREND is the setup that
should be graded differently, and it was not.
"""
import ast
from types import SimpleNamespace

import pytest

from scanner import Scanner


def _scanner(tf_regimes=None, instrument_regime="STRONG_UPTREND"):
    s = Scanner.__new__(Scanner)
    s._timeframe_regimes = dict(tf_regimes or {})
    s.latest_regimes = {"NIFTY": instrument_regime}
    return s


def _trade_like(tf):
    return SimpleNamespace(instrument="NIFTY", timeframe=tf)


def test_the_own_timeframe_regime_wins_when_it_exists():
    s = _scanner({"NIFTY_1min": "RANGING"})
    assert s._get_trade_regime(_trade_like("1min")) == "RANGING", (
        "a 1min candidate was graded on the 5min label while a 1min regime existed"
    )


def test_the_five_minute_label_is_the_fallback():
    s = _scanner({})
    assert s._get_trade_regime(_trade_like("5min")) == "STRONG_UPTREND"


def test_a_missing_own_timeframe_falls_back_rather_than_blanking():
    """Losing the regime entirely would be worse than using a related one."""
    s = _scanner({"NIFTY_5min": "STRONG_UPTREND"})
    assert s._get_trade_regime(_trade_like("15min")) == "STRONG_UPTREND"


def test_nothing_known_returns_the_default():
    s = Scanner.__new__(Scanner)
    s._timeframe_regimes = {}
    s.latest_regimes = {}
    assert s._get_trade_regime(_trade_like("5min"), default="") == ""


def test_timeframes_that_disagree_give_different_answers():
    """The whole point: one label per instrument cannot express this."""
    s = _scanner({
        "NIFTY_1min": "RANGING",
        "NIFTY_5min": "STRONG_UPTREND",
        "NIFTY_15min": "TRENDING_UP",
    })
    got = {tf: s._get_trade_regime(_trade_like(tf)) for tf in ("1min", "5min", "15min")}
    assert got == {
        "1min": "RANGING", "5min": "STRONG_UPTREND", "15min": "TRENDING_UP",
    }


# ── the entry seam, not just the accessor ────────────────────────────────────

def _regime_assignments():
    """Every `setattr(candidate, "market_regime", ...)` in Scanner, with its expression.

    There are several; a first draft matched only the first textual occurrence and so
    tested an assignment it had not changed.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(Scanner)))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "setattr"):
            continue
        if len(node.args) < 3:
            continue
        target = node.args[1]
        if isinstance(target, ast.Constant) and target.value == "market_regime":
            out.append(node.args[2])
    return out


def test_entry_grading_uses_the_own_timeframe_accessor():
    """At least one assignment must consult the own-timeframe regime."""
    exprs = _regime_assignments()
    assert exprs, "no market_regime assignment found at all"
    uses = [
        e for e in exprs
        if any(
            isinstance(c, ast.Attribute) and c.attr == "_get_trade_regime"
            for c in ast.walk(e)
        )
    ]
    assert uses, (
        "no entry-grading path consults the candidate's own-timeframe regime; they all "
        "still take the instrument-only 5min label"
    )


def test_no_entry_assignment_falls_back_to_the_bare_instrument_label():
    """latest_regimes.get(instrument) is the 5min label — the defect being fixed."""
    exprs = _regime_assignments()
    bare = []
    for e in exprs:
        for c in ast.walk(e):
            if (isinstance(c, ast.Attribute) and c.attr == "latest_regimes"):
                bare.append(ast.dump(e)[:80])
    assert not bare, f"still reading the 5min instrument label directly: {bare}"


def test_a_seeded_regime_still_wins():
    """A candidate that already carries a regime keeps it; this only fills the gap."""
    exprs = _regime_assignments()
    seeded = [
        e for e in exprs
        if any(
            isinstance(c, ast.Name) and c.id == "seed" for c in ast.walk(e)
        )
    ]
    assert seeded, "the seed's own regime is no longer preferred"


def test_the_per_timeframe_map_is_still_populated():
    """The accessor is only as good as the map behind it."""
    import inspect

    src = inspect.getsource(Scanner._cache_timeframe_intel)
    assert "_timeframe_regimes[key] = regime" in src
