"""Exit-side regime reads overrode the trade's own timeframe with the 5-minute label.

`latest_regimes` is keyed by INSTRUMENT with no timeframe, and it is written on the
5-minute pass. Two exit-side sites read it FIRST and only fell back to the `regime`
argument:

    live_regime = latest_regimes.get(base)
    live_regime = live_regime or regime          # <- own timeframe was the fallback

Every caller derives that argument from `_get_trade_regime(trade)`, i.e. the correct
per-timeframe value. So a 15-minute trade was always judged on the 5-minute label.

It cost money in both directions:

  * a 5-minute flip against a profitable 15-minute position fired LIVE_REVERSAL_EXIT and
    cut a winner the trade's own timeframe still supported;
  * a 5-minute label that merely looked aligned relaxed the end-of-day give-back, letting
    a 15-minute winner hand back more of its peak on evidence from a different chart.

This file's own borrowed-label rule is explicit that a sampled cross-timeframe regime
"must NOT be used to grant a privilege".

The instrument-level label stays as a FALLBACK for callers that genuinely have no
per-trade regime.
"""
import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

from scanner import Scanner


def _scanner(latest):
    s = Scanner.__new__(Scanner)
    s.latest_regimes = dict(latest)
    s._regime_opposes_direction = lambda view, regime: False
    return s


def _trade(instrument="NIFTY", timeframe="15min", market_regime=""):
    return SimpleNamespace(
        instrument=instrument, timeframe=timeframe, market_regime=market_regime,
        direction="LONG", inst_type="OPT",
    )


def _ctx(latest, regime):
    return Scanner._live_direction_context(_scanner(latest), _trade(), "LONG", regime)


# ── precedence ───────────────────────────────────────────────────────────────

def test_the_trades_own_regime_wins():
    ctx = _ctx({"NIFTY": "TRENDING_DOWN"}, "TRENDING_UP")
    assert ctx["live_regime"] == "TRENDING_UP", (
        "a 15-minute trade was judged on the instrument-level 5-minute label"
    )


def test_the_instrument_label_is_still_the_fallback():
    ctx = _ctx({"NIFTY": "TRENDING_DOWN"}, "")
    assert ctx["live_regime"] == "TRENDING_DOWN"


def test_the_entry_regime_is_the_last_resort():
    s = _scanner({})
    ctx = Scanner._live_direction_context(s, _trade(market_regime="VOLATILE"), "LONG", "")
    assert ctx["live_regime"] == "VOLATILE"


def test_nothing_at_all_is_unknown():
    s = _scanner({})
    assert Scanner._live_direction_context(s, _trade(), "LONG", "")["live_regime"] == "UNKNOWN"


def test_a_missing_latest_regimes_does_not_raise():
    s = Scanner.__new__(Scanner)
    s._regime_opposes_direction = lambda view, regime: False
    assert Scanner._live_direction_context(s, _trade(), "LONG", "TRENDING_UP")[
        "live_regime"
    ] == "TRENDING_UP"


# ── both sites, not just one ─────────────────────────────────────────────────

def _own_regime_first(fn):
    """True when the `regime` argument is the FIRST operand of the or-chain."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        if getattr(node.targets[0], "id", "") != "live_regime":
            continue
        value = node.value
        if isinstance(value, ast.Call):          # the str(...) wrapper
            value = value.args[0] if value.args else value
        if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
            first = value.values[0]
            if isinstance(first, ast.Name) and first.id == "regime":
                return True
    return False


def test_the_direction_context_prefers_the_trade_regime():
    assert _own_regime_first(Scanner._live_direction_context)


def test_the_giveback_exit_prefers_the_trade_regime():
    """The second site repeated the same precedence and had to be fixed too."""
    assert _own_regime_first(Scanner._peak_giveback_or_reversal_exit)


def test_no_exit_site_still_reads_the_instrument_label_first():
    """Guards against a third copy appearing."""
    for fn in (Scanner._live_direction_context, Scanner._peak_giveback_or_reversal_exit):
        src = textwrap.dedent(inspect.getsource(fn))
        assert "live_regime or regime" not in src, (
            f"{fn.__name__} still lets the instrument-level label win"
        )
