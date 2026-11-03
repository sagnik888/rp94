"""One vendor bar must not own the denominator of every volume gate.

Captured live 2026-09-18: the Fyers history backfill delivered a SENSEX 10:22 1min bar of
12,599,042 against a 20,752 session median — 607x, 84.6% of the entire session, arriving
with source=fyers_history and tick_count=0, so no tick-path rebase guard could see it. It
is not rare: 13 of 23 SENSEX sessions carry one.

That single bar lifts the 20-bar mean on every timeframe for the next 20 bars, and the
bars after it then read 0.04x where clean data reads 1.44x — 98% understated, every one
refused by the 1.05x scalp gate. The same shape on 5min (0.11x vs 0.70x) and 15min (0.03x
vs 0.21x).

The guard caps a bar's contribution to the BASELINE at a multiple of the session's own
median. Only the denominator: clipping it can lift a suppressed ratio but never suppress a
real one, and the measured bar is passed through at full size so a genuine surge still
reads as a surge. The numerator is deliberately NOT capped — the outlier bars are
dominated by 09:15 and 15:29, where huge volume is real, and abstaining there would refuse
exactly the opening drive the system exists to catch.
"""
import pandas as pd
import pytest

from intelligence.volume_profiler import (
    BASELINE_OUTLIER_MEDIAN_MULTIPLE,
    BASELINE_OUTLIER_MIN_BARS,
    VolumeProfiler,
)

ORDINARY = 20_000.0
# The real bar, to scale.
VENDOR_SPIKE = 12_599_042.0


def _session(bars: int = 60, *, spike_at: int | None = None, spike: float = VENDOR_SPIKE):
    idx = pd.date_range("2026-09-17 09:15:00", periods=bars, freq="1min")
    volume = [ORDINARY] * bars
    if spike_at is not None:
        volume[spike_at] = spike
    price = pd.Series(range(bars), dtype="float64") * 0.2 + 74_500.0
    return pd.DataFrame(
        {
            "open": price.values,
            "high": (price + 3).values,
            "low": (price - 3).values,
            "close": (price + 1).values,
            "volume": volume,
        },
        index=idx,
    )


# ── the defect ────────────────────────────────────────────────────────────────


def _unguarded(frame):
    """What the 20-bar mean and ratio were before the guard existed."""
    volume = frame["volume"].astype(float)
    avg = float(volume.rolling(20, min_periods=5).mean().iloc[-1])
    return avg, float(volume.iloc[-1]) / avg


def test_a_vendor_spike_does_not_survive_in_the_twenty_bar_mean():
    frame = _session(spike_at=40)
    clean = VolumeProfiler().analyze(_session(), "SENSEX")
    spiked = VolumeProfiler().analyze(frame, "SENSEX")
    unguarded_avg, _ = _unguarded(frame)

    assert spiked["baseline_outliers_clipped"] == 1
    assert unguarded_avg > 30.0 * clean["avg_volume"], "fixture must reproduce the defect"
    # Winsorising leaves a bounded residue by design — the clipped bar still enters the
    # mean at the cap, so one bar can lift a 20-bar mean by at most (multiple-1)/20.
    # Substituting the median instead would erase the residue, but measured over 23
    # sessions it quadrupled the collateral on the healthy indices (NIFTY 7 -> 25,
    # BANKNIFTY 4 -> 46, MIDCPNIFTY 4 -> 41 bars newly clearing the 1.05x scalp gate),
    # so the residue is accepted and bounded here rather than removed.
    ceiling = 1.0 + (BASELINE_OUTLIER_MEDIAN_MULTIPLE - 1.0) / 20.0
    assert clean["avg_volume"] <= spiked["avg_volume"] <= ceiling * clean["avg_volume"]


def test_the_bars_after_a_spike_still_read_their_true_size():
    frame = _session(spike_at=40)
    spiked = VolumeProfiler().analyze(frame, "SENSEX")
    _, unguarded_ratio = _unguarded(frame)

    assert unguarded_ratio < 0.05, "fixture must reproduce the collapse seen live"
    assert spiked["volume_ratio"] > 15.0 * unguarded_ratio
    assert spiked["volume_ratio"] >= 0.6
    assert spiked["available"] is True


def test_the_measured_bar_is_never_capped():
    """The numerator carries a genuine 09:15 open and 15:29 close; only the mean is capped."""
    frame = _session(spike_at=59)

    result = VolumeProfiler().analyze(frame, "SENSEX")

    assert float(result["current_volume"]) == pytest.approx(VENDOR_SPIKE)
    assert result["volume_ratio"] > 10.0, "a real surge must still read as a surge"


def test_the_cap_lifts_a_ratio_but_never_suppresses_one():
    """Direction guarantee: clipping the denominator can only raise the quotient."""
    for spike_at in (20, 30, 40, 50):
        frame = _session(spike_at=spike_at)
        guarded = VolumeProfiler().analyze(frame, "SENSEX")

        active = frame["volume"]
        unguarded_avg = float(active.rolling(20, min_periods=5).mean().iloc[-1])
        unguarded_ratio = float(active.iloc[-1]) / unguarded_avg

        assert guarded["volume_ratio"] >= unguarded_ratio - 1e-9


# ── the guard must be inert when nothing is wrong ─────────────────────────────


def test_an_ordinary_session_is_untouched():
    result = VolumeProfiler().analyze(_session(), "NIFTY")

    assert result["baseline_outliers_clipped"] == 0
    assert result["available"] is True


def test_a_bar_below_the_multiple_is_not_clipped():
    """A genuine 5x bar is ordinary intraday behaviour and must stay in the mean."""
    below = (BASELINE_OUTLIER_MEDIAN_MULTIPLE - 1.0) * ORDINARY

    result = VolumeProfiler().analyze(_session(spike_at=40, spike=below), "NIFTY")

    assert result["baseline_outliers_clipped"] == 0


# ── the cap itself ────────────────────────────────────────────────────────────


def test_the_cap_is_relative_to_the_instrument_not_an_absolute_figure():
    small = VolumeProfiler().analyze(_session(), "SENSEX")
    big_frame = _session()
    big_frame["volume"] = big_frame["volume"] * 25.0
    big = VolumeProfiler().analyze(big_frame, "NIFTY")

    assert big["baseline_outlier_cap"] == pytest.approx(25.0 * small["baseline_outlier_cap"], rel=0.01)
    assert small["baseline_outlier_cap"] == pytest.approx(
        BASELINE_OUTLIER_MEDIAN_MULTIPLE * ORDINARY, rel=0.01
    )


def test_a_thin_session_borrows_the_windows_median():
    """A 15min frame has ~2 bars at 09:45; its own median is noise, warmup rows are not."""
    warmup = _session(bars=40)
    warmup.index = warmup.index - pd.Timedelta(days=1)
    today = _session(bars=3, spike_at=1)
    window = pd.concat([warmup, today])

    result = VolumeProfiler().analyze(window, "SENSEX")

    assert len(today) < BASELINE_OUTLIER_MIN_BARS
    assert result["baseline_outlier_cap"] > 0, "the guard must not switch off early in a session"
    assert result["baseline_outliers_clipped"] == 1


def test_the_keys_exist_even_when_volume_is_unavailable():
    empty = VolumeProfiler()._empty_result()

    assert empty["baseline_outliers_clipped"] == 0
    assert empty["baseline_outlier_cap"] == 0
