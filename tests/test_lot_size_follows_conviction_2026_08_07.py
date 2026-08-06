"""Lot COUNT responded to the market; lot SIZE was a constant.

Cascade and heavy-trend already scale how many trades are taken, but the size of each was
a user-configured number with only those coarse on/off multipliers. The intelligence score
that drives RR, target, exit and (optionally) the stop had no say in it.

Options only. FUT is hard-capped at fut_lots_hard_cap by operator rule and nothing may
enlarge it. Only ABOVE-neutral conviction adds size — shrinking a position because the
evidence is thin is the entry filters' job, not the sizer's.

The compression cap is deliberately exempt. "A quiet locked session is never enlarged" is
a stated safety invariant, and a market score is not permission to override it.

DEFAULT OFF: sizing up is how a good idea becomes an expensive one.
"""
from types import SimpleNamespace

import pytest

from engine.position_sizing import _market_lot_multiplier, resolve_trade_lots


def _s(**kw):
    base = dict(
        market_driven_lot_size=True,
        market_lot_size_max_boost_pct=50.0,
        market_lot_size_max_lots=4,
        auto_risk_sizing_enabled=False,
        cascade_lot_scale_enabled=True,
        heavy_trend_lot_scale_enabled=True,
        fut_lots_hard_cap=1,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _lots(**kw):
    args = dict(
        settings=_s(), inst_type="OPT", requested_lots=2, lot_size=75,
        entry_price=217.50, stop_price=185.0, intel_score=100.0,
    )
    args.update(kw)
    return resolve_trade_lots(**args)


# ── the multiplier ───────────────────────────────────────────────────────────

def test_it_is_inert_unless_switched_on():
    assert _market_lot_multiplier(_s(market_driven_lot_size=False), "OPT", 100.0) == 1.0


def test_strong_conviction_adds_size():
    assert _market_lot_multiplier(_s(), "OPT", 100.0) == pytest.approx(1.5)


def test_neutral_conviction_adds_nothing():
    assert _market_lot_multiplier(_s(), "OPT", 50.0) == pytest.approx(1.0)


def test_weak_conviction_never_shrinks_below_the_configured_size():
    """Below neutral the trade should not be taken larger; shrinking is the filters' job."""
    assert _market_lot_multiplier(_s(), "OPT", 5.0) == pytest.approx(1.0)


def test_futures_are_never_enlarged():
    """FUT is hard-capped at fut_lots_hard_cap by operator rule."""
    assert _market_lot_multiplier(_s(), "FUT", 100.0) == 1.0


def test_no_reading_is_not_a_reason_to_size_up():
    assert _market_lot_multiplier(_s(), "OPT", 0.0) == 1.0
    assert _market_lot_multiplier(_s(), "OPT", None) == 1.0
    assert _market_lot_multiplier(_s(), "OPT", "high") == 1.0


def test_the_boost_span_is_bounded():
    assert _market_lot_multiplier(_s(market_lot_size_max_boost_pct=500.0), "OPT", 100.0) <= 2.0
    assert _market_lot_multiplier(_s(market_lot_size_max_boost_pct=0.0), "OPT", 100.0) == 1.0


# ── through the real sizer ───────────────────────────────────────────────────

def test_conviction_grows_the_resolved_lots():
    assert _lots(requested_lots=2, intel_score=100.0).lots == 3      # 2 * 1.5


def test_a_neutral_market_leaves_the_configured_size_alone():
    assert _lots(requested_lots=2, intel_score=50.0).lots == 2


def test_it_is_off_by_default_end_to_end():
    assert _lots(settings=_s(market_driven_lot_size=False), intel_score=100.0).lots == 2


def test_the_absolute_lot_ceiling_binds():
    assert _lots(requested_lots=4, intel_score=100.0,
                 settings=_s(market_lot_size_max_lots=4)).lots == 4


def test_cap_lots_still_binds():
    """A caller-supplied cap is a real limit, not a suggestion."""
    assert _lots(requested_lots=2, intel_score=100.0, cap_lots=2).lots == 2


def test_the_compression_cap_is_never_overridden():
    """'A quiet locked session is never enlarged' is a safety invariant."""
    for state in ("FORMING", "LOCKED", "BREAKOUT_WATCH", "FAILED_BREAKOUT", "REVERSION_LOCKED"):
        got = _lots(requested_lots=3, intel_score=100.0, compression_state=state)
        assert got.lots == 1, f"{state} was enlarged to {got.lots} lots by a market score"


def test_futures_stay_hard_capped_through_the_sizer():
    got = _lots(inst_type="FUT", requested_lots=3, intel_score=100.0)
    assert got.lots == 1


def test_a_zero_lot_result_is_left_alone():
    assert _lots(requested_lots=0, intel_score=100.0).lots == 0


def test_the_reason_records_what_happened():
    got = _lots(requested_lots=2, intel_score=100.0)
    assert "market conviction" in got.reason and "1.50x" in got.reason


# ── the wiring ───────────────────────────────────────────────────────────────

def test_the_entry_path_supplies_the_score():
    import ast
    import inspect
    import textwrap

    from engine.signal_processor import SignalProcessor

    tree = ast.parse(textwrap.dedent(inspect.getsource(SignalProcessor)))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", "")) == "resolve_trade_lots"
    ]
    assert calls, "the entry path no longer resolves lots through the sizer"
    for c in calls:
        assert any(k.arg == "intel_score" for k in c.keywords), (
            "the sizer accepts a conviction score and the entry path never passes one"
        )
