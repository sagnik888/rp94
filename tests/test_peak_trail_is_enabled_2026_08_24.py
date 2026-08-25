"""Keep peak_trail as an opt-in experiment pending held-out validation.

The give-back protection stack had every percentage rule switched off, leaving only
breakeven_stop's FLAT Rs.80 floor, which has no memory of the peak. A trade that peaked
Rs.2,000 was held until it gave back Rs.1,920.

peak_trail is the primitive already built for this and already gated:
`trailed = peak * (1 - peak_trail_giveback_pct)` applied only when
`peak > peak_trail_above_rs` (scanner.py, inside _peak_giveback_cap_exit). The gate is
the point — test_peak_trail_is_gated_to_large_peaks_2026_08_19 records why an UNGATED
percentage is refused: "40% of a Rs.300 peak is Rs.120 - inside the spread. The flat
floor owns this."

IN-SAMPLE EVIDENCE for the opt-in experiment, at gate 600 and giveback 40%, replayed
through the real _peak_giveback_cap_exit over all 94 recorded pnl_paths
(2026-08-12..2026-08-24):

    trail OFF  Rs.501     trail ON  Rs.1,423     +Rs.922
    sessions worse: 0/9          trades changed: 4
    leave-one-day-out: positive for every day removed (min +502)
    bootstrap over days: median +892, 90% CI [+139, +1,762], P(>0) = 99.6%

That replay is too small and selected to justify changing unattended-live defaults.
The implementation remains available behind an explicit flag, while the shipped
default stays OFF until an independent held-out cohort supports it.

WHAT THIS DOES NOT FIX. Peaks under Rs.600 are untouched, which includes both 2026-08-24
losses (SENSEX peaked Rs.533 and booked net -Rs.24; NIFTY peaked Rs.328 and booked
net -Rs.48). Percentaging that range was tried and refused by the tests above, and an
ungated version measured worse on the same corpus once the gate was removed. That case
is still open.
"""
from types import SimpleNamespace

import pytest

from config.settings import Settings, get_settings
from scanner import Scanner


def _sc():
    return Scanner.__new__(Scanner)


def _t():
    return SimpleNamespace(instrument="SENSEX", inst_type="OPT", timeframe="5min")


def _fire(peak, now):
    return Scanner._peak_giveback_cap_exit(
        _sc(), _t(), current_pnl_rs=now, peak_pnl_rs=peak
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "breakeven_stop_enabled", True)
    monkeypatch.setattr(s, "peak_giveback_cap_enabled", False)
    # Behavioral cases exercise the explicit experiment; the default-off contract
    # is covered separately by test_it_ships_disabled_pending_held_out_validation.
    monkeypatch.setattr(s, "peak_trail_enabled", True)


def test_it_ships_disabled_pending_held_out_validation():
    assert Settings(_env_file=None).peak_trail_enabled is False


def test_the_gate_and_giveback_are_unchanged():
    """The opt-in experiment keeps the measured gate and give-back values."""
    s = Settings()
    assert float(s.peak_trail_above_rs) == 600.0
    assert float(s.peak_trail_giveback_pct) == 0.40


def test_a_large_peak_no_longer_rides_back_to_the_floor():
    """Rs.2,000 peak: trail sits at 2000*0.60 = Rs.1,200."""
    assert _fire(peak=2000.0, now=1300.0)[0] is False
    fired, why = _fire(peak=2000.0, now=1100.0)
    assert fired is True
    assert "2,000" in why or "2000" in why


def test_the_gate_still_protects_small_peaks():
    """Below Rs.600 the flat floor owns it — 40% of a small peak is inside the spread."""
    assert _fire(peak=300.0, now=180.0)[0] is False
    assert _fire(peak=450.0, now=200.0)[0] is False   # under the gate: floor governs
    assert _fire(peak=300.0, now=79.0)[0] is True     # the floor still works


def test_todays_sensex_peak_stays_outside_the_conservative_gate():
    """The 2026-08-24 Rs.533 peak remains outside the Rs.600 experiment gate."""
    assert _fire(peak=533.0, now=250.0)[0] is False


def test_it_never_cuts_a_trade_at_its_high():
    for peak in (700.0, 1500.0, 3000.0):
        assert _fire(peak=peak, now=peak)[0] is False


def test_turning_it_off_restores_the_flat_floor(monkeypatch):
    monkeypatch.setattr(get_settings(), "peak_trail_enabled", False)
    assert _fire(peak=2000.0, now=500.0)[0] is False
