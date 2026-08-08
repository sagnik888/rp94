"""The intelligence volume pillar was the one index-volume consumer with no unit guard.

Index volume switches mid-session between Fyers cash-market SHARES and the broker's
near-month FUTURES CONTRACTS when Fyers drops. Both are legitimate; they are not the same
quantity, and they differ by roughly 1000x — a measured 0.006 ratio for NIFTY and 0.001
for SENSEX.

Two homogeneity guards already existed. `Scanner._volume_window_is_homogeneous` is wired
to the power-move and thrust detectors; `MarketDataProvider.volume_series_is_homogeneous`
is wired to a dashboard-only panel. `VolumeProfiler` — the pillar that actually feeds the
intelligence score — called neither and never read `volume_basis` at all.

On a Fyers RECOVERY the numerator flips back to shares while the 20-bar mean is still
contracts, so `vol_ratio` reads in the hundreds: `is_surge` fires, `surge_level` goes
EXTREME, and ±0.20 of intelligence score is injected in whatever direction the last bar
happened to close.

The guard is duplicated rather than imported because intelligence must not depend on
scanner. Its NaN rule is copied exactly and was learned the hard way: backfill rows carry
no basis, `float('nan')` is TRUTHY, and treating it as a distinct unit marked every
post-backfill window mixed and killed the surge signal the guard existed to protect.
"""
import numpy as np
import pandas as pd
import pytest

from intelligence.volume_profiler import VolumeProfiler, volume_window_is_homogeneous


def _frame(bases, vols=None, bars=60):
    """A healthy session whose trailing bars carry `bases`."""
    vols = vols or [500_000.0] * bars
    basis = [""] * (bars - len(bases)) + list(bases)
    end = pd.Timestamp.now().floor("min") - pd.Timedelta(minutes=2)
    idx = pd.date_range(end=end, periods=bars, freq="1min")
    price = np.linspace(24_500.0, 24_600.0, bars)
    return pd.DataFrame(
        {
            "open": price, "high": price + 5.0, "low": price - 5.0, "close": price,
            "volume": vols, "volume_basis": basis,
        },
        index=idx,
    )


# ── the rule itself ──────────────────────────────────────────────────────────

def test_one_basis_is_homogeneous():
    assert volume_window_is_homogeneous(_frame(["fyers_index"] * 60)) is True


def test_two_bases_are_not():
    assert volume_window_is_homogeneous(
        _frame(["fyers_index"] * 30 + ["kite_index_futures"] * 30)
    ) is False


def test_a_resampled_mixed_bucket_is_not():
    assert volume_window_is_homogeneous(_frame(["fyers_index"] * 59 + ["MIXED"])) is False


def test_a_missing_column_is_permissive():
    df = _frame(["fyers_index"] * 60).drop(columns=["volume_basis"])
    assert volume_window_is_homogeneous(df) is True


@pytest.mark.parametrize("blank", ["", "nan", "NaN", "None", float("nan"), None])
def test_unknown_is_not_a_unit(blank):
    """Backfill rows carry no basis. NaN is truthy — this is the bug that was fixed once."""
    assert volume_window_is_homogeneous(_frame([blank] * 30 + ["fyers_index"] * 30)) is True


def test_it_matches_the_scanner_implementation():
    """Duplicated on purpose; they must not drift."""
    from scanner import Scanner

    for bases in (
        ["fyers_index"] * 60,
        ["fyers_index"] * 30 + ["kite_index_futures"] * 30,
        ["fyers_index"] * 59 + ["MIXED"],
        [""] * 30 + ["fyers_index"] * 30,
        [float("nan")] * 60,
    ):
        df = _frame(bases)
        assert volume_window_is_homogeneous(df) == Scanner._volume_window_is_homogeneous(df)


# ── the pillar abstains ──────────────────────────────────────────────────────

def test_a_unit_flip_does_not_manufacture_a_surge():
    """The live shape: 20-bar mean in contracts, newest bars back in shares."""
    vols = [500.0] * 40 + [500_000.0] * 20        # contracts -> shares, ~1000x
    bases = ["kite_index_futures"] * 40 + ["fyers_index"] * 20
    res = VolumeProfiler().analyze(_frame(bases, vols), "NIFTY")
    assert res["available"] is False, (
        f"scored a {res['volume_ratio']}x surge out of a feed failover"
    )
    assert res.get("is_surge") is not True


def test_the_result_says_why_it_abstained():
    """`source` collapses to UNAVAILABLE for every cause, so the reason carries it.

    A mixed-unit window is a very different problem from a dead feed, and the operator
    needs to tell them apart.
    """
    vols = [500.0] * 40 + [500_000.0] * 20
    bases = ["kite_index_futures"] * 40 + ["fyers_index"] * 20
    res = VolumeProfiler().analyze(_frame(bases, vols), "NIFTY")
    assert res["source"] == "UNAVAILABLE"
    assert "mixes units" in res["reason"]


def test_a_dead_feed_still_reports_its_own_reason():
    """The mixed-unit reason must not swallow the ordinary absence case."""
    res = VolumeProfiler().analyze(_frame(["fyers_index"] * 60, [0.0] * 60), "NIFTY")
    assert res["available"] is False
    assert "mixes units" not in res["reason"]


def test_a_single_basis_window_still_measures():
    res = VolumeProfiler().analyze(_frame(["fyers_index"] * 60), "NIFTY")
    assert res["available"] is True


def test_a_frame_without_the_column_still_measures():
    """Historical and synthetic frames must not all go dark."""
    df = _frame(["fyers_index"] * 60).drop(columns=["volume_basis"])
    assert VolumeProfiler().analyze(df, "NIFTY")["available"] is True


def test_a_backfilled_window_still_measures():
    """Blank basis rows are the normal post-backfill state, not a unit change."""
    df = _frame([""] * 40 + ["fyers_index"] * 20)
    assert VolumeProfiler().analyze(df, "NIFTY")["available"] is True
