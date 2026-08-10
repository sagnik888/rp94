"""An off-market bar manufactured a 4x-7x volume surge out of a collapsed denominator.

The time-of-day ratio compares the current bar against the same clock-time on other days:

    same_slot = df[(df.index.time == last_ts.time()) & (df.index.date != last_ts.date())]
    tod_avg   = same_slot.tail(10).mean()
    relative_volume_ratio = measured_vol / tod_avg

The only guards were `len(same_slot) >= 2` and `tod_avg > 0`. Neither asks whether that
slot has enough activity to BE a yardstick. Off-session slots — bars the builder creates
after the close, on a reconnect, or from a stray out-of-hours tick — carry a handful of
ticks on every day, so tod_avg collapses to ~2 while the current bar holds 10-40. The
ratio is then 5x-20x with nothing unusual happening.

Observed live off-market as 4x-7x on the intel panel. Reproduced here:

    off-market ticks on last bar    display ratio    is_surge
                               2             0.91       False
                              10             4.55       TRUE
                              20            10.00       TRUE
                              40            16.67       TRUE

This is not cosmetic. is_surge feeds volume_spike_entry_min_volume_ratio,
one_way_cascade_min_volume_ratio and quality_hybrid_thrust_volume_ratio, and
effective_volume_ratio is max(rolling, time_of_day) — so the inflated number is the one
those gates see.

The fix asks the denominator to be supported: a time-of-day average far below the day's
typical bar is not a yardstick, so the read falls back to ROLLING_AVG instead of
publishing a surge. `active_mask` could not be reused for this — it is a same-DATE filter,
not a market-hours one.

A genuine in-session surge is untouched: real session slots carry full history, so their
time-of-day average sits near the daily median and stays confident.
"""
import numpy as np
import pandas as pd
import pytest

from intelligence.volume_profiler import VolumeProfiler

SESSION_START = pd.Timedelta(hours=9, minutes=15)
SESSION_END = pd.Timedelta(hours=15, minutes=25)


def _frame(off_market_ticks=None, last_slot_ticks=None, days=6, seed=7):
    """Cash-index shape: no traded volume, tick_count as the live builder fills it."""
    rs = np.random.RandomState(seed)
    rows = []
    for day in range(days):
        base = pd.Timestamp("2026-08-04") + pd.Timedelta(days=day)
        slots = pd.date_range(base + SESSION_START, base + SESSION_END, freq="5min")
        for i, t in enumerate(slots):
            frac = i / max(1, len(slots) - 1)
            dens = 1.6 - 1.2 * np.sin(np.pi * frac)          # U-shaped, like real tape
            rows.append((t, 24000 + i, 24010 + i, 23990 + i, 24000 + i, 0.0,
                         max(1, int(rs.poisson(120 * dens)))))
        if off_market_ticks is not None:
            for t in pd.date_range(base + pd.Timedelta(hours=15, minutes=40),
                                   periods=3, freq="5min"):
                n = off_market_ticks if day == days - 1 else max(1, int(rs.poisson(2)))
                rows.append((t, 24100, 24100, 24100, 24100, 0.0, n))
    df = pd.DataFrame(
        rows, columns=["timestamp", "open", "high", "low", "close", "volume", "tick_count"]
    ).set_index("timestamp").sort_index()
    if last_slot_ticks is not None:
        df.iloc[-1, df.columns.get_loc("tick_count")] = last_slot_ticks
    return df


def _read(**kw):
    return VolumeProfiler().analyze(_frame(**kw), instrument="NIFTY")


# ── the bug ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ticks", [10, 20, 40, 80])
def test_an_unsupported_slot_does_not_manufacture_a_surge(ticks):
    r = _read(off_market_ticks=ticks)
    assert r["is_surge"] is False, (
        f"{ticks} off-market ticks reported a surge "
        f"(display={r['display_volume_ratio']}x)"
    )


@pytest.mark.parametrize("ticks", [10, 20, 40, 80])
def test_the_published_ratio_stays_plausible_off_market(ticks):
    r = _read(off_market_ticks=ticks)
    assert r["display_volume_ratio"] <= 3.0, (
        f"panel would show {r['display_volume_ratio']}x on a quiet off-market bar"
    )


@pytest.mark.parametrize("ticks", [10, 20, 40, 80])
def test_the_gated_ratio_stays_plausible_too(ticks):
    """effective_volume_ratio is max(rolling, time_of_day) and is what the entry gates
    consume, so clamping only the display value would fix the symptom and not the risk."""
    r = _read(off_market_ticks=ticks)
    assert r["effective_volume_ratio"] <= 3.0


def test_an_unsupported_slot_falls_back_to_the_rolling_basis():
    r = _read(off_market_ticks=40)
    assert "TIME_OF_DAY" not in str(r.get("volume_ratio_basis", "")).upper()


# ── what must keep working ───────────────────────────────────────────────────

def test_a_normal_session_close_read_is_unchanged():
    r = _read()
    assert r["available"] is True
    assert r["is_surge"] is False
    assert 0.3 <= r["display_volume_ratio"] <= 2.0


def test_a_real_in_session_surge_still_fires():
    """The guard must not silence a genuine spike on a well-supported slot."""
    df = _frame()
    df.iloc[-1, df.columns.get_loc("tick_count")] = int(df["tick_count"].median() * 6)
    r = VolumeProfiler().analyze(df, instrument="NIFTY")
    assert r["is_surge"] is True, "a real 6x in-session spike was suppressed"
    assert r["effective_volume_ratio"] >= 3.0


def test_the_time_of_day_basis_is_still_used_when_the_slot_is_supported():
    r = _read()
    assert "TIME_OF_DAY" in str(r.get("volume_ratio_basis", "")).upper()


def test_traded_volume_frames_are_unaffected():
    """Futures/options frames carry real volume and never take the proxy path."""
    df = _frame()
    df["volume"] = df["tick_count"] * 1000.0
    r = VolumeProfiler().analyze(df, instrument="NIFTY")
    assert r["is_proxy"] is False
    assert r["available"] is True


def test_a_short_frame_does_not_crash():
    df = _frame(days=1).head(8)
    r = VolumeProfiler().analyze(df, instrument="NIFTY")
    assert isinstance(r, dict) and "display_volume_ratio" in r
