"""Zero-volume bars must not be averaged into the surge denominator.

Index history carries volume 0 — that was the original outage. Now that live index bars
carry real volume via the futures fallback, a lookback at SESSION OPEN is mostly
zero-volume historical bars plus a handful of live ones. Averaging the zeros drags the
prior-bar mean toward zero and fabricates a ratio out of nothing:

    15 zero-volume history + 6 IDENTICAL live bars -> 4.0x
    18 zero-volume history + 3 IDENTICAL live bars -> 10.0x

Both clear power_move_volume_mult (2.5), which is the only route a 1min signal has into
a live entry — so this manufactured an entry at the open from bars that showed no change
in participation whatsoever.

The mixed-unit volume_basis guard cannot catch this: it is not a unit mismatch, it is
absent measurements being counted as measurements of zero.
"""
import numpy as np
import pandas as pd
import pytest

from scanner import Scanner


class _Candles:
    def __init__(self, df):
        self.df = df

    def get_candles(self, instrument, timeframe):
        return self.df


def _window(hist_bars, live_bars, live_vol=500.0, last_vol=None, basis="kite_index_futures"):
    n = hist_bars + live_bars
    idx = pd.date_range("2026-08-03 09:15", periods=n, freq="1min")
    base = 24000 + np.arange(n) * 0.5
    vols = [0.0] * hist_bars + [live_vol] * live_bars
    if last_vol is not None:
        vols[-1] = last_vol
    df = pd.DataFrame(
        {"open": base, "high": base + 4, "low": base - 4, "close": base + 2, "volume": vols},
        index=idx,
    )
    df["volume_basis"] = [None] * hist_bars + [basis] * live_bars
    return df


def _metrics(df):
    sc = Scanner.__new__(Scanner)
    sc.candles = _Candles(df)
    return Scanner._candle_thrust_metrics(sc, "NIFTY", "1min", "BUY")


@pytest.mark.parametrize("hist,live", [(5, 16), (10, 11), (15, 6)])
def test_zero_volume_history_does_not_manufacture_a_surge(hist, live):
    """Every live bar is identical, so the honest ratio is 1.0."""
    m = _metrics(_window(hist, live))
    assert m is not None
    assert m["volume_ratio"] == pytest.approx(1.0), (
        f"{hist} empty history bars inflated the ratio to {m['volume_ratio']:.2f}"
    )


def test_too_few_measured_bars_reads_as_unmeasurable_not_as_a_breakout():
    """Three live bars is not enough to characterise participation."""
    assert _metrics(_window(18, 3)) is None


def test_a_genuine_surge_still_registers():
    m = _metrics(_window(0, 21, live_vol=500.0, last_vol=3000.0))
    assert m is not None and m["volume_ratio"] == pytest.approx(6.0)


def test_a_genuine_surge_registers_even_alongside_empty_history():
    """The fix must not suppress a real surge just because history is empty."""
    m = _metrics(_window(10, 11, live_vol=500.0, last_vol=2500.0))
    assert m is not None and m["volume_ratio"] == pytest.approx(5.0)


def test_an_all_empty_window_stays_unmeasurable():
    """Genuinely absent volume must keep failing closed, as it did before."""
    assert _metrics(_window(21, 0)) is None
