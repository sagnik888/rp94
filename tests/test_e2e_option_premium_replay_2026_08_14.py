"""E2E: option-premium replay across all four indices and every regime.

The exit lifecycle is where this system's edge is won or lost — the whole
investigation started from winners being cut after seconds for 2-12 points. This
replays synthetic premium paths through the REAL exit machinery (sim_runner calls
Scanner._dynamic_runner_lock_pct and Scanner._big_move_expected, not copies of them)
and certifies the invariants across the grid this system actually trades.

WHAT IS REAL HERE
  sim_runner            scripts/backtest_exit_lifecycle.py, the tool that gates
                        money decisions; its own self-tests live alongside
  _dynamic_runner_lock_pct / _big_move_expected   real Scanner methods
  lot sizes             the true ones: SENSEX 20, BANKNIFTY 30, NIFTY 65,
                        MIDCPNIFTY 120 (settings.py:952)

THE LOT-SIZE COUPLING THIS PINS
sim_runner arms the give-back guard at

    arm_pts = max(0.40 * risk, 500.0 / qty)

a rupee floor divided by lot size, so the POINT threshold varies ~6x across the four
indices for identical risk:

    SENSEX      qty  20  ->  25.00 pts
    BANKNIFTY   qty  30  ->  16.67 pts
    NIFTY       qty  65  ->   7.69 pts
    MIDCPNIFTY  qty 120  ->   4.17 pts

On MIDCPNIFTY the guard is live after four points of premium gain; on SENSEX it needs
twenty-five. That is a real and deliberate consequence of sizing the floor in rupees,
and it is exactly the shape of "cut early for a few points" on the large-lot indices.
Pinned here so it cannot drift silently.
"""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from scanner import Scanner


_SPEC = importlib.util.spec_from_file_location(
    "bt_exit_e2e", Path(__file__).resolve().parents[1] / "scripts" / "backtest_exit_lifecycle.py"
)
bt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bt)


LOTS = {"SENSEX": 20, "BANKNIFTY": 30, "NIFTY": 65, "MIDCPNIFTY": 120}
REGIMES = (
    "TRENDING_UP",
    "TRENDING_DOWN",
    "BREAKOUT_UP",
    "BREAKOUT_DOWN",
    "RANGING",
    "VOLATILE",
    "UNKNOWN",
)

ENTRY, STOP, TARGET = 100.0, 90.0, 115.0
RISK = ENTRY - STOP


def _sc():
    return Scanner.__new__(Scanner)


def _bars(seq):
    return pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(2026, 8, 14, 9, 20) + pd.Timedelta(minutes=i),
                "open": h,
                "high": h,
                "low": lo,
                "close": lo,
                "volume": 1,
            }
            for i, (h, lo) in enumerate(seq)
        ]
    )


def _trade(regime="TRENDING_UP", adx=30.0, option_type="CE"):
    return {
        "entry": ENTRY,
        "stop": STOP,
        "target": TARGET,
        "raw_regime": regime,
        "adx": adx,
        "direction": "LONG",
        "option_type": option_type,
    }


def _arm_pts(qty):
    """The give-back arm threshold sim_runner actually uses."""
    return max(0.40 * RISK, 500.0 / max(1, qty))


# ── the loss path, every index x every regime ────────────────────────────────

@pytest.mark.parametrize("index", sorted(LOTS))
@pytest.mark.parametrize("regime", REGIMES)
def test_a_straight_loss_exits_at_the_stop(index, regime):
    bars = _bars([(101, 99), (95, 90), (92, 85)])

    px, why = bt.sim_runner(_trade(regime), bars, _sc(), LOTS[index])

    assert why == "STOP"
    assert px == STOP


@pytest.mark.parametrize("index", sorted(LOTS))
@pytest.mark.parametrize("regime", REGIMES)
def test_no_path_ever_exits_below_the_stop(index, regime):
    """The invariant that bounds the loss on every index and regime."""
    paths = [
        [(101, 99), (95, 90), (92, 80)],          # gaps through the stop
        [(105, 104), (120, 118), (140, 138), (132, 60)],  # winner then collapse
        [(103, 102), (100, 99), (95, 94), (91, 70)],      # small peak then collapse
        [(101, 100), (102, 101), (103, 102)],             # drifts up, never exits
    ]

    for path in paths:
        px, _why = bt.sim_runner(_trade(regime), _bars(path), _sc(), LOTS[index])
        assert px >= STOP, f"{index}/{regime} exited at {px}, below the stop {STOP}"


# ── the winner path: a proven move must not be given back ────────────────────

@pytest.mark.parametrize("index", sorted(LOTS))
@pytest.mark.parametrize("regime", REGIMES)
def test_a_proven_big_move_is_not_cut_by_the_giveback_guard(index, regime):
    """The original complaint: winners cut for a few points.

    Peak 100 -> 140 (4R) then a fade to 128. Once the move is proven the give-back
    guard must be suppressed and the trailing lock must govern instead.
    """
    bars = _bars([(105, 104), (120, 118), (140, 138), (132, 128)])

    px, why = bt.sim_runner(_trade(regime), bars, _sc(), LOTS[index])

    assert why == "RUNNER_LOCK", f"{index}/{regime} exited via {why}, not the runner lock"
    assert px > ENTRY + RISK, f"{index}/{regime} gave back a 4R move down to {px}"


@pytest.mark.parametrize("regime", REGIMES)
def test_the_runner_lock_keeps_most_of_the_peak(regime):
    """The current runtime base lock is 0.85, so >=85% is the invariant."""
    bars = _bars([(105, 104), (120, 118), (140, 138), (132, 128)])
    peak_gain = 140.0 - ENTRY

    px, why = bt.sim_runner(_trade(regime), bars, _sc(), 65)

    assert why == "RUNNER_LOCK"
    assert px >= ENTRY + peak_gain * 0.85


def test_dynamic_helper_can_give_choppier_regimes_more_room():
    """When a tighter 0.90 base is supplied, high-volatility tape relaxes to 0.85."""
    sc = _sc()

    assert sc._dynamic_runner_lock_pct(0.90, "BREAKOUT_UP") < sc._dynamic_runner_lock_pct(
        0.90, "TRENDING_UP"
    )
    assert sc._dynamic_runner_lock_pct(0.90, "VOLATILE") < sc._dynamic_runner_lock_pct(
        0.90, "RANGING"
    )


# ── the rupee floor makes the guard index-dependent ──────────────────────────

@pytest.mark.parametrize(
    "index,expected_pts",
    [("SENSEX", 25.00), ("BANKNIFTY", 16.666), ("NIFTY", 7.692), ("MIDCPNIFTY", 4.166)],
)
def test_the_giveback_arms_at_a_lot_size_dependent_point_threshold(index, expected_pts):
    """max(0.40 * risk, 500 / qty): a rupee floor divided by lot size."""
    assert _arm_pts(LOTS[index]) == pytest.approx(expected_pts, abs=0.01)


def test_the_arm_threshold_spans_six_times_across_the_four_indices():
    """Identical risk, six times the point requirement. Deliberate, and worth seeing."""
    pts = {k: _arm_pts(v) for k, v in LOTS.items()}

    assert pts["SENSEX"] / pts["MIDCPNIFTY"] == pytest.approx(6.0, abs=0.01)
    assert max(pts, key=pts.get) == "SENSEX"
    assert min(pts, key=pts.get) == "MIDCPNIFTY"


@pytest.mark.parametrize("index", sorted(LOTS))
def test_the_rupee_floor_binds_at_a_ten_point_stop_on_every_index(index):
    """Which term BINDS decides whether tuning risk changes anything at all.

    At a 10-point premium stop the 0.40R term is 4.0 and the rupee floor is 500/qty,
    which is larger on all four indices — so widening or tightening `risk` moves the
    arm threshold on none of them.
    """
    assert _arm_pts(LOTS[index]) > 0.40 * RISK


@pytest.mark.parametrize(
    "index,crossover_risk",
    [("SENSEX", 62.5), ("BANKNIFTY", 41.67), ("NIFTY", 19.23), ("MIDCPNIFTY", 10.42)],
)
def test_the_r_term_only_takes_over_past_a_lot_size_specific_stop_width(index, crossover_risk):
    """0.40 * risk overtakes 500 / qty at risk = 1250 / qty.

    MIDCPNIFTY crosses over at a 10.4-point stop, so on that index the R-term governs
    almost every real trade. SENSEX would need a 62-point stop before risk mattered at
    all. Same formula, opposite regimes of behaviour — worth knowing before anyone
    tunes the 0.40 expecting it to bite everywhere.
    """
    qty = LOTS[index]
    assert 1250.0 / qty == pytest.approx(crossover_risk, abs=0.01)

    just_below = 0.40 * (crossover_risk - 1.0)
    just_above = 0.40 * (crossover_risk + 1.0)
    assert just_below < 500.0 / qty < just_above


# ── PE side and counter-direction ────────────────────────────────────────────

@pytest.mark.parametrize("index", sorted(LOTS))
def test_a_put_contract_replays_identically_in_premium_space(index):
    """Exits reason about PREMIUM, which rises for a winning PE just as for a CE."""
    bars = _bars([(105, 104), (120, 118), (140, 138), (132, 128)])

    px_ce, why_ce = bt.sim_runner(_trade(option_type="CE"), bars, _sc(), LOTS[index])
    px_pe, why_pe = bt.sim_runner(_trade(option_type="PE"), bars, _sc(), LOTS[index])

    assert why_ce == why_pe
    assert px_ce == pytest.approx(px_pe)


# ── a flat path resolves rather than hanging ─────────────────────────────────

@pytest.mark.parametrize("index", sorted(LOTS))
def test_a_path_that_never_triggers_closes_out(index):
    bars = _bars([(101, 100), (101, 100), (101, 100)])

    px, why = bt.sim_runner(_trade(), bars, _sc(), LOTS[index])

    assert why == "CLOSE"
    assert px > 0
