"""The deep-red rupee ladder already scales with size — pinned so it stays that way.

This was flagged as a gap by a name search (`rupee_guard` has 0 occurrences here), but
the CAPABILITY is present under different names and has been since 2026-07-10. Porting a
second scaling on top would have produced two competing ladders on the same decision.

What exists: rupee_loss_band_scale_per_lot (default True) multiplies the ladder's arm,
step and force levels by the LOT COUNT, so a 2-lot book breathes twice as far before the
deep-red ladder arms and bigger size is never cut on one-lot premium noise. MIDCPNIFTY
gets a further rupee_loss_band_midcap_relax_pct (default 20%) on top, because its lot is
120 against NIFTY 65, BANKNIFTY 30 and SENSEX 20 — the same rupee loss is a far smaller
premium move there.

These tests exist because that scaling is invisible at one lot, which is the only size
this account has traded so far. It becomes load-bearing at the multi-lot configuration,
which is exactly when a silent regression would go unnoticed.
"""
import pytest

from scanner import Scanner


class _Trade:
    def __init__(self, lots=1, instrument="NIFTY", inst_type="OPT"):
        self.lots = lots
        self.instrument = instrument
        self.inst_type = inst_type
        self.lot_size = 65
        self.id = "TR1"
        self.entry_price = 200.0


class _S:
    rupee_loss_band_enabled = True
    rupee_loss_band_opt_only = False
    rupee_loss_band_start_rs_opt = 1000.0
    rupee_loss_band_size_rs_opt = 500.0
    rupee_loss_band_min_severity_opt = 4
    rupee_loss_band_force_loss_rs_opt = 4000.0
    rupee_loss_band_start_rs = 1000.0
    rupee_loss_band_size_rs = 500.0
    rupee_loss_band_min_severity = 4
    rupee_loss_band_scale_per_lot = True
    rupee_loss_band_midcap_relax_pct = 20.0


def _scanner():
    s = Scanner.__new__(Scanner)
    s._setting_enabled = lambda v, d=True: bool(v)
    s._rupee_loss_band_track_slow_bleed = lambda *a, **k: {}
    s._loss_guard_severity = lambda *a, **k: (0, [], {})
    s._rupee_loss_band_freefall_flags = lambda *a, **k: ([], {})
    return s


def _decision(trade, pnl, settings=None):
    import scanner as scanner_mod

    original = scanner_mod.get_settings
    scanner_mod.get_settings = lambda: (settings or _S())
    try:
        return Scanner._rupee_loss_band_guard_decision(
            _scanner(), trade, current_pnl_rs=pnl, current_price=180.0,
        )
    finally:
        scanner_mod.get_settings = original


def _arm_level_from_reason(reason: str) -> float:
    """The ladder start quoted in the 'below start' reason string."""
    assert "ladder start Rs." in reason, reason
    return float(reason.split("ladder start Rs.")[1].split()[0])


def test_a_single_lot_arms_at_the_configured_level():
    _fired, _band, reason = _decision(_Trade(lots=1), -900.0)
    assert _arm_level_from_reason(reason) == pytest.approx(1000.0)


def test_two_lots_breathe_twice_as_far_before_arming():
    """Otherwise a 2-lot book is cut on what is one-lot premium noise."""
    _fired, _band, reason = _decision(_Trade(lots=2), -1900.0)
    assert _arm_level_from_reason(reason) == pytest.approx(2000.0)


def test_three_lots_scale_linearly():
    _fired, _band, reason = _decision(_Trade(lots=3), -2900.0)
    assert _arm_level_from_reason(reason) == pytest.approx(3000.0)


def test_midcpnifty_gets_a_further_relaxation():
    """Lot 120 against NIFTY 65: the same rupees are a much smaller premium move."""
    _fired, _band, reason = _decision(_Trade(lots=1, instrument="MIDCPNIFTY"), -1100.0)
    assert _arm_level_from_reason(reason) == pytest.approx(1200.0)


def test_the_midcap_relaxation_compounds_with_lot_count():
    _fired, _band, reason = _decision(_Trade(lots=2, instrument="MIDCPNIFTY"), -2300.0)
    assert _arm_level_from_reason(reason) == pytest.approx(2400.0)


def test_scaling_can_be_switched_off():
    class _Flat(_S):
        rupee_loss_band_scale_per_lot = False
        rupee_loss_band_midcap_relax_pct = 0.0

    _fired, _band, reason = _decision(_Trade(lots=3), -900.0, settings=_Flat())
    assert _arm_level_from_reason(reason) == pytest.approx(1000.0)


def test_a_winner_never_arms_the_ladder():
    fired, _band, reason = _decision(_Trade(lots=3), 500.0)
    assert fired is False and reason == "winner"


def test_a_loss_below_the_scaled_arm_does_not_fire():
    """The point of scaling: 1500 arms a 1-lot book but not a 2-lot one."""
    fired_one, _b, _r = _decision(_Trade(lots=1), -1500.0)
    fired_two, _b2, reason_two = _decision(_Trade(lots=2), -1500.0)
    assert fired_two is False, "a 2-lot book was cut at a 1-lot threshold"
    assert "< ladder start" in reason_two
    # The 1-lot case is past its arm, so it reaches the severity machinery instead of
    # being refused outright — that is the behaviour difference this scaling creates.
    assert "< ladder start" not in _r
