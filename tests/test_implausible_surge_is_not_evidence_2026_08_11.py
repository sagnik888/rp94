"""A corrupted volume bar produced a 1,000,000,000,000x surge that cleared every gate.

Found by the stress harness, not in production. `_volume_window_is_homogeneous` was the
only thing standing between a bad bar and the surge gates, and it inspects a
`volume_basis` LABEL column — never magnitudes — so it catches a Fyers cash/futures unit
switch and nothing else. Worse, it returns True for any frame lacking that column, which
includes every stored CSV.

So a single absurd volume produced an unbounded ratio that cleared volume_spike (2.2x),
rare_burst (3.5x) and everything between.

The bound is set from measurement, not intuition. Across every stored series the highest
GENUINE ratio is 310x — SENSEX 1min, whose volume base is thin enough that one busy
minute against a small average is legitimate — and p99.9 runs 4.7x to 69x. A fixed cap
near the typical range would have rejected that real reading, which is the same mistake
as a fixed volume threshold on tick counts. 1000x sits ~3x above the highest real value
and many orders of magnitude below corruption.

Treated as UNMEASURABLE rather than clamped. A number that size is not evidence of
anything, and turning broken data into a verdict is this codebase's recurring failure.
"""
import numpy as np
import pandas as pd
import pytest

from scanner import Scanner


def _frame(last_volume, bars=60, prior=1000.0):
    idx = pd.date_range("2026-08-11 09:15", periods=bars, freq="5min")
    c = np.linspace(24000, 24100, bars)
    df = pd.DataFrame(
        {"open": c, "high": c + 20, "low": c - 20, "close": c,
         "volume": np.full(bars, prior)},
        index=idx,
    )
    df.iloc[-1, df.columns.get_loc("volume")] = last_volume
    return df


def _ratio(last_volume):
    sc = Scanner.__new__(Scanner)
    df = _frame(last_volume)
    sc.candles = type("C", (), {"get_candles": staticmethod(lambda i, t: df)})()
    m = Scanner._candle_thrust_metrics(sc, "NIFTY", "5min", "BUY")
    return None if m is None else m["volume_ratio"]


# ── corruption is refused ────────────────────────────────────────────────────

@pytest.mark.parametrize("vol", [1_001_000.0, 1e9, 1e15, float("inf")])
def test_an_implausible_ratio_abstains(vol):
    assert _ratio(vol) is None, f"{vol} produced a usable surge reading"


def test_the_1e12_case_from_the_stress_harness():
    assert _ratio(1e15) is None


# ── every genuine reading survives ───────────────────────────────────────────

@pytest.mark.parametrize("mult", [1, 2, 3, 10, 50, 310, 999])
def test_real_surges_are_untouched(mult):
    """310x is the highest ratio ever observed in the stored data (SENSEX 1min)."""
    r = _ratio(1000.0 * mult)
    assert r is not None, f"{mult}x was wrongly rejected"
    assert r == pytest.approx(float(mult), rel=0.02)


def test_the_bound_sits_above_the_measured_maximum():
    assert Scanner.SURGE_RATIO_IMPLAUSIBLE >= 310.0 * 3


def test_an_ordinary_bar_is_unaffected():
    assert _ratio(1000.0) == pytest.approx(1.0)


# ── the guard it backs up is label-only, which is why this was needed ────────

def test_the_homogeneity_guard_does_not_look_at_magnitudes():
    """If it ever starts doing so, this bound becomes belt-and-braces rather than the
    only magnitude check."""
    df = _frame(1e15)
    assert Scanner._volume_window_is_homogeneous(df.tail(21)) is True


def test_a_frame_without_a_basis_column_is_assumed_homogeneous():
    """Every stored CSV lacks volume_basis, so the unit guard is a no-op there."""
    df = _frame(1000.0)
    assert "volume_basis" not in df.columns
    assert Scanner._volume_window_is_homogeneous(df) is True
