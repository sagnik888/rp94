"""The funding warning used a futures margin figure for an options account.

`entry_funds_warning_floor` was a flat Rs.50,000 constant, and the warning called it
"the floor for one index lot". This system BUYS options: one lot costs
premium x lot_size, and the four indices differ by 6x on lot size alone.

    SENSEX      lot  20  ->  Rs. 2,400 at a Rs.120 ATM premium
    BANKNIFTY   lot  30  ->  Rs. 3,600
    NIFTY       lot  65  ->  Rs. 7,800
    MIDCPNIFTY  lot 120  ->  Rs.14,400   (and Rs.48,000 at a Rs.400 premium)

So the real requirement spans roughly Rs.2,000 to Rs.48,000 depending on index and
premium. A flat Rs.50,000 declared an account unfundable when it could in fact buy
several of those entries — reported wrongly to this operator on 2026-08-08, overstating
what they needed to deposit by about 20x.

The question that matters is "can I afford ANY entry", so the floor is now the MINIMUM
one-lot cost across the ACTIVE indices, using real lot sizes from instruments.json. The
ATM premium is an assumption and is named in the message so it can be judged against the
day's actual chain.
"""
from types import SimpleNamespace

import pytest

from engine.safe_mode import cheapest_one_lot_cost

LOTS = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20, "MIDCPNIFTY": 120}


def _settings(active=None, premium=120.0, override=0.0):
    return SimpleNamespace(
        entry_funds_warning_floor=override,
        entry_funds_assumed_atm_premium=premium,
        active_indices=active or list(LOTS),
        get_instruments=lambda: {
            "indices": {k: {"lot_size": v} for k, v in LOTS.items()}
        },
    )


# ── the cost is a real lot cost ──────────────────────────────────────────────

def test_it_uses_the_cheapest_active_index():
    cost, detail = cheapest_one_lot_cost(_settings())
    assert cost == pytest.approx(20 * 120)          # SENSEX
    assert "SENSEX" in detail


def test_it_is_nowhere_near_the_old_flat_floor():
    """The bug: Rs.50,000 overstated the requirement by ~20x."""
    cost, _ = cheapest_one_lot_cost(_settings())
    assert cost < 5_000


def test_dropping_the_cheap_index_raises_the_bar():
    cost, detail = cheapest_one_lot_cost(_settings(active=["NIFTY", "MIDCPNIFTY"]))
    assert cost == pytest.approx(65 * 120)          # NIFTY is now cheapest
    assert "NIFTY" in detail


def test_a_richer_premium_costs_more():
    cost, _ = cheapest_one_lot_cost(_settings(active=["MIDCPNIFTY"], premium=400.0))
    assert cost == pytest.approx(120 * 400)         # Rs.48,000, the dear end


def test_the_message_names_its_assumption():
    """The premium is a guess; the operator must be able to see and judge it."""
    _, detail = cheapest_one_lot_cost(_settings())
    assert "120" in detail and "lot" not in detail.lower().split()[0]
    assert "assumed" in detail


# ── the override still works ─────────────────────────────────────────────────

def test_an_explicit_floor_wins():
    cost, detail = cheapest_one_lot_cost(_settings(override=75000.0))
    assert cost == 75000.0
    assert "operator floor" in detail


def test_zero_means_derive():
    cost, _ = cheapest_one_lot_cost(_settings(override=0.0))
    assert cost == pytest.approx(2400.0)


# ── it degrades safely ───────────────────────────────────────────────────────

def test_unreadable_instruments_reports_no_floor():
    """No data is not a funding problem; it must not invent one."""
    s = _settings()
    s.get_instruments = lambda: (_ for _ in ()).throw(RuntimeError("no file"))
    assert cheapest_one_lot_cost(s) == (0.0, "")


def test_a_junk_lot_size_is_skipped():
    s = _settings(active=["NIFTY", "SENSEX"])
    s.get_instruments = lambda: {"indices": {"NIFTY": {"lot_size": 65},
                                             "SENSEX": {"lot_size": "oops"}}}
    cost, detail = cheapest_one_lot_cost(s)
    assert cost == pytest.approx(65 * 120) and "NIFTY" in detail


def test_a_junk_premium_falls_back_to_the_default():
    s = _settings()
    s.entry_funds_assumed_atm_premium = "cheap"
    assert cheapest_one_lot_cost(s)[0] == pytest.approx(2400.0)


# ── the live config ──────────────────────────────────────────────────────────

def test_the_live_config_derives_a_sane_floor():
    from config.settings import get_settings

    cost, detail = cheapest_one_lot_cost(get_settings())
    assert 1_000 <= cost <= 20_000, f"derived {cost:,.0f} ({detail})"
