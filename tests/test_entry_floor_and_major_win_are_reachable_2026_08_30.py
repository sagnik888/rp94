"""A threshold the market never delivers is an off switch, not a safety rail.

Two guards shipped with rungs above anything the book has ever produced, and both
failures look identical in production: nothing happens, and nothing is logged as wrong.

1. entry_policy's expected-move floor. Compared against the 25th PERCENTILE of the
   historical forward move (entry_policy_forecast_quantile = 0.25) and set to
   0.15 / 0.15 / 0.15 / 0.25. Replaying that computation over the stored candles, the
   rolling q25 the gate reads spans 0.017%-0.063% and its MAXIMUM over the whole history
   is 0.0469 (NIFTY 5min) through 0.1399 (MIDCPNIFTY 15min) -- every maximum below the
   floor it had to clear. Expressed against the raw distribution, 0.15% is the 91st
   percentile of NIFTY 5min forward moves; the gate demanded the 25th percentile clear a
   level only 3-32% of moves reach. It is the LAST gate before an entry, so what it
   refused had already passed everything else. It went live 2026-08-28 and the system
   took ZERO trades that day, against 5 / 10 / 17 / 14 on the four sessions before.

2. MAJOR_WIN_GUARD's flat Rs.3,000 rung across a 6x lot-size spread. Over 26+ sessions
   the largest peak ever recorded is Rs.2,603 on NIFTY (0 of 80 lifecycles reached it)
   and Rs.1,556 on BANKNIFTY (0 of 56). Half the grid ran with that protection
   permanently disarmed, which reads as "it never triggered", not as a fault.

Both are now sized to what the instrument actually delivers, and these tests exist to
stop either drifting back out of reach.
"""
from types import SimpleNamespace

import pytest

from config.settings import Settings, get_settings
from engine.entry_policy import (
    HARD_EXPECTED_MOVE_FLOORS,
    effective_expected_move_floor_pct,
)
from scanner import Scanner


# The largest rolling q25 ever observed per index, on the BINDING 5-minute series,
# replayed the way _forecast_expected_move computes it over data_store/candles.
# A floor at or above these numbers cannot pass, ever.
OBSERVED_MAX_ROLLING_Q25_5MIN = {
    "NIFTY": 0.0469,
    "BANKNIFTY": 0.0662,
    "SENSEX": 0.0522,
    "MIDCPNIFTY": 0.0688,
}


# ── the entry floor has to be reachable ──────────────────────────────────────

@pytest.mark.parametrize("index", sorted(OBSERVED_MAX_ROLLING_Q25_5MIN))
def test_the_hard_floor_is_below_what_the_market_actually_delivers(index):
    """The regression: every hard floor sat ABOVE the maximum the series ever reached."""
    ceiling = OBSERVED_MAX_ROLLING_Q25_5MIN[index]

    assert HARD_EXPECTED_MOVE_FLOORS[index] < ceiling, (
        f"{index}'s floor {HARD_EXPECTED_MOVE_FLOORS[index]} is at or above the largest "
        f"rolling q25 ever observed ({ceiling}) -- the gate can never pass"
    )


@pytest.mark.parametrize("index", sorted(OBSERVED_MAX_ROLLING_Q25_5MIN))
def test_the_configured_floor_is_reachable_too(index):
    configured = Settings().entry_policy_expected_move_floor_pct_by_index[index]
    effective = effective_expected_move_floor_pct(index, configured)

    assert effective < OBSERVED_MAX_ROLLING_Q25_5MIN[index]
    assert effective > 0.0, "a zero floor disables the check rather than calibrating it"


def test_the_readings_that_zeroed_the_session_are_no_longer_all_refused():
    """The seven final-gate readings logged on 2026-08-28, when the book took nothing."""
    cfg = Settings().entry_policy_expected_move_floor_pct_by_index
    observed = [("NIFTY", 0.0447), ("NIFTY", 0.0291), ("MIDCPNIFTY", 0.0278),
                ("NIFTY", 0.0345), ("NIFTY", 0.0221), ("NIFTY", 0.0451),
                ("MIDCPNIFTY", 0.0278)]

    passing = sum(
        1 for idx, reading in observed
        if reading + 1e-9 >= effective_expected_move_floor_pct(idx, cfg[idx])
    )

    assert passing > 0, "the gate still refuses every reading from that session"
    assert passing < len(observed), (
        "the gate now passes everything -- that is disabling it, not calibrating it"
    )


def test_configuration_can_still_only_tighten():
    """The hard floor's whole purpose. Loosening it by settings must stay impossible."""
    assert effective_expected_move_floor_pct("NIFTY", 0.50) == pytest.approx(0.50)
    assert effective_expected_move_floor_pct("NIFTY", 0.001) == pytest.approx(
        HARD_EXPECTED_MOVE_FLOORS["NIFTY"]
    )


# ── the major-win rung has to be reachable, per index ────────────────────────

def _position(index, entry_price, lots, lot_size):
    return SimpleNamespace(
        instrument=index, entry_price=entry_price, lots=lots, lot_size=lot_size,
        quantity=lots * lot_size,
    )


# Median position cost and the largest peak ever recorded, per index, over 26+ sessions.
REAL_POSITIONS = {
    "NIFTY": (139.0, 1, 65, 2603.0),
    "BANKNIFTY": (650.0, 1, 30, 1556.0),
    "SENSEX": (476.0, 1, 20, 3399.0),
    "MIDCPNIFTY": (162.0, 1, 120, 3990.0),
}


@pytest.mark.parametrize("index", sorted(REAL_POSITIONS))
def test_the_major_win_rung_is_reachable_on_every_index(index):
    """The regression: a flat Rs.3,000 was never once reached on NIFTY or BANKNIFTY."""
    price, lots, lot_size, best_peak_ever = REAL_POSITIONS[index]
    sc = Scanner.__new__(Scanner)

    arm = sc._major_win_guard_arm_rs(_position(index, price, lots, lot_size))

    assert arm > 0.0
    assert arm < best_peak_ever, (
        f"{index} arms at Rs.{arm:,.0f} but its best peak in 26+ sessions was "
        f"Rs.{best_peak_ever:,.0f} -- the guard is disarmed on this index"
    )


def test_the_rung_is_sized_to_the_contract_not_flat():
    """A flat number means six different things across a 6x lot-size spread."""
    sc = Scanner.__new__(Scanner)
    arms = {
        index: sc._major_win_guard_arm_rs(_position(index, p, l, ls))
        for index, (p, l, ls, _peak) in REAL_POSITIONS.items()
    }

    assert len(set(round(a) for a in arms.values())) > 1, "the rung is still flat"
    # MIDCPNIFTY costs roughly twice a NIFTY position and its wins run larger, so it
    # must arm higher than NIFTY rather than at the same rupee number.
    assert arms["MIDCPNIFTY"] > arms["NIFTY"]


def test_an_unpriceable_position_falls_back_to_the_flat_rung():
    """Arming an unpriced position is worse than leaving the old behaviour in place."""
    sc = Scanner.__new__(Scanner)
    flat = float(get_settings().major_win_guard_flat_fallback_rs)

    assert sc._major_win_guard_arm_rs(_position("NIFTY", 0.0, 0, 0)) == pytest.approx(flat)
    assert sc._major_win_guard_arm_rs(_position("UNKNOWN_IDX", 139.0, 1, 65)) == pytest.approx(flat)
    assert sc._major_win_guard_arm_rs(None) == pytest.approx(flat)


def test_futures_major_win_uses_a_risk_multiple_not_option_cost_percentages():
    sc = Scanner.__new__(Scanner)
    future = SimpleNamespace(
        instrument="NIFTY", inst_type="FUT", entry_price=25_000.0,
        lots=1, lot_size=25, quantity=25, frozen_initial_risk_rs=2_500.0,
    )

    arm = sc._major_win_guard_arm_rs(future)

    expected = max(
        get_settings().major_win_guard_flat_fallback_rs,
        future.frozen_initial_risk_rs * get_settings().major_win_guard_futures_r_multiple,
    )
    assert arm == pytest.approx(expected)
    assert arm > future.frozen_initial_risk_rs, "a sub-1R peak must not arm a major-win exit"


def test_both_ladders_read_the_same_rung():
    """The session and live ladders priced the same exit differently once already."""
    import inspect

    src = inspect.getsource(Scanner)

    # One definition plus one call in each ladder.
    assert src.count("_major_win_guard_arm_rs(") >= 3, (
        "one of the two major-win call sites is still using a hardcoded rung"
    )
    # Scoped to the MAJOR_WIN sites specifically. scanner.py carries other Rs.3,000
    # literals -- the SMART_PROFIT_LOCK ratio tier at ~15790 is a different rule with
    # the same flat-rupee shape, noted but deliberately not changed here.
    for marker in ("Major Win Protection", "MAJOR_WIN_GUARD"):
        idx = src.find(marker)
        if idx == -1:
            continue
        window = src[max(0, idx - 400): idx + 400]
        assert "3000.0" not in window, (
            f"a flat Rs.3,000 rung survives next to {marker!r}"
        )
