"""A repaint abort closed a bought OPTION at the INDEX SPOT price.

The async repaint tracker force-exits a position whose arrow vanished inside its source
candle. It priced that exit with:

    price = self.candles.get_latest_price(candidate.instrument) or trade.current_price

`candidate.instrument` is the INDEX name — candidates are built with the index as
`instrument` and the option premium as `price` — so that call returns the index SPOT.
Nothing downstream checks the basis: `_close_and_record` takes whatever float it is given.

NIFTY 25000CE bought at 120, spot 24850, 75 qty:

    recorded exit  = 24850
    recorded P&L   = (24850 - 120) * 75 = Rs 18,547,500

written into the trade book. In manual mode the operator is shown that as the exit price
to confirm. The tracker is started for ANY intracandle entry — there is no inst_type
filter on it — so every bought option that repaints hits this.

This is the "two sides on different bases" pattern: index points where option premium
was required.
"""
from types import SimpleNamespace

import pytest

from scanner import Scanner


def _scanner(spot=24850.0):
    s = Scanner.__new__(Scanner)
    s.candles = SimpleNamespace(get_latest_price=lambda inst: spot)
    return s


def _opt(current=None, entry=120.0):
    return SimpleNamespace(
        inst_type="OPT", symbol="NIFTY25000CE", entry_price=entry, current_price=current
    )


CAND = SimpleNamespace(instrument="NIFTY")


# ── options mark on their own premium ────────────────────────────────────────

def test_an_option_never_marks_at_the_index_spot():
    price = _scanner()._repaint_abort_exit_price(_opt(current=96.0), CAND)
    assert price == 96.0, f"marked the option at {price}, the index spot is 24850"


def test_the_spot_is_not_used_even_when_the_option_mark_is_missing():
    """Falls back to entry, which books ~flat, not a fabricated 18-lakh gain."""
    assert _scanner()._repaint_abort_exit_price(_opt(current=None), CAND) == 120.0


def test_a_zero_option_mark_falls_through_to_entry():
    assert _scanner()._repaint_abort_exit_price(_opt(current=0.0), CAND) == 120.0


def test_nothing_usable_returns_zero_so_the_caller_can_decline():
    got = _scanner()._repaint_abort_exit_price(_opt(current=0.0, entry=0.0), CAND)
    assert got == 0.0


def test_a_junk_mark_does_not_raise():
    assert _scanner()._repaint_abort_exit_price(
        SimpleNamespace(inst_type="OPT", symbol="X", current_price="nope", entry_price=120.0),
        CAND,
    ) == 120.0


# ── the index path is unchanged ──────────────────────────────────────────────

def test_a_future_marks_on_the_live_futures_price():
    """Changed 2026-08-08: this asserted the index SPOT, which is the other half of the
    same bug. A future differs from spot by the BASIS -- tens of points on NIFTY -- so
    booking a futures exit at spot misprices the recorded P&L by basis x quantity."""
    s = _scanner()
    s._get_live_fut_ltp_cached = lambda t: 24875.0
    fut = SimpleNamespace(inst_type="FUT", symbol="NIFTYFUT", entry_price=24800.0,
                          current_price=24810.0, entry_spot=24790.0)
    assert s._repaint_abort_exit_price(fut, CAND) == 24875.0


def test_a_future_without_a_live_quote_holds_the_entry_basis():
    """Falls back to the spot level mapped through the entry basis, not raw spot."""
    s = _scanner(spot=24850.0)
    s._get_live_fut_ltp_cached = lambda t: 0.0
    fut = SimpleNamespace(inst_type="FUT", symbol="NIFTYFUT", entry_price=24800.0,
                          current_price=24810.0, entry_spot=24790.0)
    # basis at entry = 24800 - 24790 = +10, so spot 24850 -> future 24860
    assert s._repaint_abort_exit_price(fut, CAND) == pytest.approx(24860.0)


def test_a_non_option_falls_back_when_every_feed_is_dry():
    s = Scanner.__new__(Scanner)
    s.candles = SimpleNamespace(get_latest_price=lambda inst: 0.0)
    s._get_live_fut_ltp_cached = lambda t: 0.0
    fut = SimpleNamespace(inst_type="FUT", symbol="NIFTYFUT", entry_price=24800.0,
                          current_price=24810.0, entry_spot=24790.0)
    assert s._repaint_abort_exit_price(fut, CAND) == 24810.0


def test_a_raising_feed_does_not_break_the_abort():
    s = Scanner.__new__(Scanner)

    def _boom(_):
        raise RuntimeError("feed down")

    s.candles = SimpleNamespace(get_latest_price=_boom)
    s._get_live_fut_ltp_cached = lambda t: 0.0
    fut = SimpleNamespace(inst_type="FUT", symbol="F", entry_price=24800.0,
                          current_price=24810.0, entry_spot=24790.0)
    assert s._repaint_abort_exit_price(fut, CAND) == 24810.0


# ── the wiring ───────────────────────────────────────────────────────────────

def test_the_tracker_uses_the_helper_and_not_the_raw_spot():
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(Scanner._track_repaint_until_candle_close))
    tree = ast.parse(src)
    calls = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "_repaint_abort_exit_price" in calls, "the abort still prices its own exit"
    assert "get_latest_price" not in calls, (
        "the tracker still reaches for the index spot directly"
    )


def test_a_zero_mark_does_not_book_an_exit():
    """Booking 0.0 would record a total loss; leave it to the exit engines instead."""
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(Scanner._track_repaint_until_candle_close))
    tree = ast.parse(src)
    guarded = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Compare)
        and isinstance(n.test.left, ast.Name)
        and n.test.left.id == "price"
    ]
    assert guarded, "no guard between an unusable mark and _close_and_record"
