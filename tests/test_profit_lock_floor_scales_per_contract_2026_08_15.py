"""The give-back floor must mean the same thing on all four indices.

profit_lock_abs_floor_rs was a flat Rs.450 applied identically to NIFTY,
BANKNIFTY, SENSEX and MIDCPNIFTY. Those contracts span 6x on lot size alone —
SENSEX 20, MIDCPNIFTY 120 — so the same rupee line is a completely different
instruction depending on where it lands:

    SENSEX      Rs.500 / 20  = 25.0 points of premium
    MIDCPNIFTY  Rs.500 / 120 =  4.2 points of premium

Entries here come from an ATR flip, which needs room to breathe. Four points of
premium is not room; it is noise, and the trade is cut before the move it was
entered for can develop.

Scaling on position cost fixes it with one number instead of four. 5% reproduces
both reference points independently:

    SENSEX      ~500 premium x 20  = Rs.10,000 cost -> Rs.500   (25.0 pts, today's behaviour)
    MIDCPNIFTY  ~170 premium x 120 = Rs.20,400 cost -> Rs.1,020 ( 8.5 pts, the ask)
    NIFTY       ~120 premium x 65  = Rs. 7,800 cost -> Rs.390   ( 6.0 pts)
    BANKNIFTY   ~250 premium x 30  = Rs. 7,500 cost -> Rs.375   (12.5 pts)

Keeping the flat Rs.450 as the other half of the floor would have left the defect
alive in the two places it was least visible: it binds on NIFTY and BANKNIFTY and
nowhere else, giving them 5.77% and 6.00% of position cost against SENSEX's 4.50%
and MIDCPNIFTY's 2.21%. Rs.450 was never cost coverage in the first place — the
real round trip on these four is Rs.125-202 — so cost coverage is derived from
_round_trip_cost_rs instead.

The effective floor is max(cost x pct, round_trip x multiple). At 2x the round
trip the coverage floor lands at Rs.250-404, under the 5% figure on all four, so
the proportional rule governs uniformly and coverage binds only where a position
really is too small to be worth locking. profit_lock_abs_floor_rs remains the
fallback for a row that cannot be priced or sized — never a floor of zero.
"""
import pytest

from scanner import Scanner


class _T:
    def __init__(self, entry_price, lots, lot_size):
        self.entry_price = entry_price
        self.lots = lots
        self.lot_size = lot_size
        self.quantity = lots * lot_size


def _scanner():
    return Scanner.__new__(Scanner)


# ── the two anchor points the operator gave ──────────────────────────────────

def test_sensex_keeps_the_behaviour_that_already_works():
    """Rs.500 on a 20-lot is 25 points — the reference the operator named."""
    floor = _scanner()._profit_lock_floor_rs(_T(500.0, 1, 20))

    assert floor == pytest.approx(500.0)
    assert floor / 20 == pytest.approx(25.0), "25 points of premium breathing room"


def test_midcpnifty_gets_the_room_it_was_missing():
    """Was Rs.450 (3.75 points). The ask was ~Rs.1,000."""
    floor = _scanner()._profit_lock_floor_rs(_T(170.0, 1, 120))

    assert floor == pytest.approx(1020.0)
    assert floor / 120 == pytest.approx(8.5), "8.5 points, not 3.75"


def test_the_two_indices_now_get_comparable_breathing_room():
    """The defect in one line: the same rupee floor was 6x tighter on MIDCPNIFTY."""
    s = _scanner()
    sensex_pts = s._profit_lock_floor_rs(_T(500.0, 1, 20)) / 20
    midcap_pts = s._profit_lock_floor_rs(_T(170.0, 1, 120)) / 120

    # Both are now a fixed share of premium rather than a fixed rupee amount.
    assert sensex_pts / 500.0 == pytest.approx(midcap_pts / 170.0, rel=1e-6)


# ── all four land on the SAME proportion, which is the whole point ───────────

@pytest.mark.parametrize("name,premium,lot", [
    ("NIFTY", 120.0, 65),
    ("BANKNIFTY", 250.0, 30),
    ("SENSEX", 500.0, 20),
    ("MIDCPNIFTY", 170.0, 120),
])
def test_every_index_gets_the_same_share_of_position_cost(name, premium, lot):
    """The flat Rs.450 used to bind on NIFTY and BANKNIFTY and only on those two,
    handing them 5.77% and 6.00% while SENSEX got 4.50% and MIDCPNIFTY 2.21% --
    the same unevenness the percentage exists to remove, surviving where it was
    least visible. Cost coverage is derived now, so it no longer distorts."""
    t = _T(premium, 1, lot)
    t.instrument = name

    floor = _scanner()._profit_lock_floor_rs(t)

    assert floor / (premium * lot) * 100 == pytest.approx(5.0, abs=0.01)


def test_cost_coverage_binds_only_when_the_position_is_genuinely_tiny():
    """The real round trip is Rs.125-202 on these contracts, not Rs.450, so the
    coverage floor sits below 5% everywhere and only takes over far underneath."""
    s = _scanner()
    tiny = _T(5.0, 1, 20)   # Rs.100 of premium at risk; 5% would be Rs.5

    assert s._profit_lock_floor_rs(tiny) > 5.0, "must still clear the round trip"


def test_the_floor_is_the_larger_of_the_two_rules():
    s = _scanner()

    assert s._profit_lock_floor_rs(_T(900.0, 1, 120)) == pytest.approx(5400.0)


def test_more_lots_raise_the_floor_proportionally():
    """Two lots put twice the money at risk and earn twice the breathing room."""
    s = _scanner()

    assert s._profit_lock_floor_rs(_T(170.0, 2, 120)) == pytest.approx(2040.0)


# ── it degrades safely ───────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [0.0, None, -5.0])
def test_an_unpriced_row_falls_back_to_the_flat_floor(bad):
    """Never a floor of zero — that would let anything cut anything."""
    assert _scanner()._profit_lock_floor_rs(_T(bad, 1, 120)) == pytest.approx(450.0)


def test_an_unsized_row_falls_back_to_the_flat_floor():
    t = _T(170.0, 0, 0)

    assert _scanner()._profit_lock_floor_rs(t) == pytest.approx(450.0)


def test_no_trade_at_all_falls_back_to_the_flat_floor():
    assert _scanner()._profit_lock_floor_rs(None) == pytest.approx(450.0)


def test_quantity_is_used_when_lots_are_missing():
    t = _T(170.0, 1, 120)
    t.lots = 0
    t.lot_size = 0  # quantity survives as the authoritative size

    assert _scanner()._profit_lock_floor_rs(t) == pytest.approx(1020.0)


def test_setting_the_percentage_to_zero_restores_the_flat_floor(monkeypatch):
    import scanner as scanner_module

    settings = scanner_module.get_settings()
    monkeypatch.setattr(settings, "profit_lock_cost_pct", 0.0, raising=False)

    assert _scanner()._profit_lock_floor_rs(_T(170.0, 1, 120)) == pytest.approx(450.0)


# ── the collapse check honours the per-contract floor ────────────────────────

def test_the_collapse_check_uses_the_contract_floor_when_given_one():
    """A MIDCPNIFTY trade peaking at Rs.900 is BELOW its Rs.1,020 floor, so the
    keep-floor may not fire on it at all — under the flat Rs.450 it would have."""
    s = _scanner()

    assert s._smart_lock_floor_collapse(900.0, 400.0, 0.0, floor_rs=1020.0) is False
    assert s._smart_lock_floor_collapse(900.0, 400.0, 0.0) is True


def test_a_genuine_collapse_still_fires_on_the_contract_floor():
    s = _scanner()

    assert s._smart_lock_floor_collapse(2500.0, 900.0, 0.0, floor_rs=1020.0) is True


@pytest.mark.parametrize("ignored", [None, 0.0, -1.0])
def test_an_absent_floor_override_keeps_the_configured_default(ignored):
    """Existing callers that pass nothing must behave exactly as before."""
    s = _scanner()

    assert s._smart_lock_floor_collapse(900.0, 449.0, 0.0, floor_rs=ignored) is True
    assert s._smart_lock_floor_collapse(900.0, 451.0, 0.0, floor_rs=ignored) is False


# ── the LOWEST locks: the arming rungs ───────────────────────────────────────
#
# A flat rupee arm bites hardest here. profit_lock_nonscalp_min_peak_rs = Rs.250 is
# 12.5 points of premium on a 20-lot SENSEX and 2.1 points on a 120-lot MIDCPNIFTY —
# inside the noise of an ATR flip, so MIDCPNIFTY armed a lock before its entry had
# room to prove anything.

def _priced(name, premium, lot):
    t = _T(premium, 1, lot)
    t.instrument = name
    return t


@pytest.mark.parametrize("name,premium,lot", [
    ("NIFTY", 120.0, 65),
    ("BANKNIFTY", 250.0, 30),
    ("SENSEX", 500.0, 20),
    ("MIDCPNIFTY", 170.0, 120),
])
def test_every_index_arms_at_the_same_share_of_position_cost(name, premium, lot):
    s = _scanner()

    arm = s._profit_lock_arm_base_rs(_priced(name, premium, lot), 250.0, 2.5)

    assert arm / (premium * lot) * 100 == pytest.approx(2.5, abs=0.01)


def test_sensex_arming_is_unchanged_because_it_is_the_anchor():
    """Rs.250 of a Rs.10,000 position IS 2.5% — the reference behaviour is preserved."""
    arm = _scanner()._profit_lock_arm_base_rs(_priced("SENSEX", 500.0, 20), 250.0, 2.5)

    assert arm == pytest.approx(250.0)


def test_midcpnifty_no_longer_arms_inside_the_noise():
    """Was 2.1 points of premium. An ATR flip needs more room than that."""
    t = _priced("MIDCPNIFTY", 170.0, 120)

    arm = _scanner()._profit_lock_arm_base_rs(t, 250.0, 2.5)

    assert arm == pytest.approx(510.0)
    assert arm / 120 == pytest.approx(4.25), "4.25 points, not 2.08"


def test_the_scalp_rung_scales_too():
    s = _scanner()

    assert s._profit_lock_arm_base_rs(_priced("SENSEX", 500.0, 20), 500.0, 5.0) == pytest.approx(500.0)
    assert s._profit_lock_arm_base_rs(_priced("MIDCPNIFTY", 170.0, 120), 500.0, 5.0) == pytest.approx(1020.0)


@pytest.mark.parametrize("pct", [0.0, -1.0])
def test_zero_percent_returns_the_flat_rupee_base(pct):
    """The escape hatch back to the previous behaviour."""
    assert _scanner()._profit_lock_arm_base_rs(_priced("MIDCPNIFTY", 170.0, 120), 250.0, pct) == 250.0


def test_an_unpriced_row_arms_on_the_flat_base():
    assert _scanner()._profit_lock_arm_base_rs(_T(0.0, 1, 120), 250.0, 2.5) == 250.0
    assert _scanner()._profit_lock_arm_base_rs(None, 250.0, 2.5) == 250.0


# ── position cost is the shared basis ────────────────────────────────────────

def test_position_cost_is_premium_times_quantity():
    assert Scanner._position_cost_rs(_T(170.0, 1, 120)) == pytest.approx(20400.0)
    assert Scanner._position_cost_rs(_T(170.0, 2, 120)) == pytest.approx(40800.0)


def test_futures_use_entry_stop_risk_not_full_notional_for_profit_locks():
    future = _T(25_000.0, 1, 50)
    future.inst_type = "FUT"
    future.initial_stop = 24_950.0
    future.trailing_stop = 24_950.0

    # Old calculation: 25,000 x 50 = Rs.1.25m and a 5% lock floor of Rs.62,500,
    # which never arms on normal intraday futures P&L.  The actual entry-stop risk is
    # 50 points x 50 = Rs.2,500.
    assert Scanner._position_cost_rs(future) == pytest.approx(2_500.0)
    # The normal round-trip coverage floor remains, but the threshold is reachable.
    assert _scanner()._profit_lock_floor_rs(future) == pytest.approx(400.0)


def test_futures_prefer_the_frozen_entry_risk_when_available():
    future = _T(25_000.0, 1, 50)
    future.inst_type = "FUT"
    future.initial_stop = 24_950.0
    future.frozen_initial_risk_rs = 3_100.0

    assert Scanner._position_cost_rs(future) == pytest.approx(3_100.0)


@pytest.mark.parametrize("bad", [None, 0.0, -1.0])
def test_position_cost_is_zero_when_it_cannot_be_computed(bad):
    assert Scanner._position_cost_rs(_T(bad, 1, 120)) == 0.0
