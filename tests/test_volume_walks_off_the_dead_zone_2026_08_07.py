"""The right edge of an index volume series is a dead zone several bars deep.

Index candles carry no traded volume on the live tick feed. It arrives only with the
periodic Fyers history backfill ("Prioritizing Fyers history for ... to get volume"),
which lags the tape — so the newest few bars hold a rounding error rather than a
measurement, and the count is not one bar.

Captured live on 2026-08-07 at 10:15 IST, 1min frame, all four indices at once:

    NIFTY      tail=[515102, 478060, 210220, 6760, 5655, 0]  avg=495,263  -> 0.011x
    BANKNIFTY  measured_vol=870                              avg=472,822  -> 0.002x
    SENSEX     measured_vol=60                               avg=24,399   -> 0.002x
    MIDCPNIFTY measured_vol=240                              avg=316,548  -> 0.001x

The prior fix stepped back exactly ONE bar, which lands still inside the zone.

The reading that results is not harmless. It reports available=True, so the volume
pillar in the aggregator SCORES a phantom "no participation" into every candidate,
rather than abstaining the way it does when volume is honestly absent.
"""
import numpy as np
import pandas as pd
import pytest

from intelligence.volume_profiler import MAX_STUB_WALKBACK, VolumeProfiler

# The real NIFTY tail, 2026-08-07 10:15 IST.
LIVE_NIFTY_TAIL = [515102, 478060, 210220, 6760, 5655, 0]


def _frame(tail, healthy=500_000, bars=60, freq="1min"):
    """A session of healthy bars ending in `tail`, indexed a full interval in the past.

    The index deliberately ends well before now so the wall-clock "still forming" test
    cannot be what rescues the measurement — only the stub walk-back can.
    """
    vols = [healthy] * (bars - len(tail)) + list(tail)
    end = pd.Timestamp.now().floor("min") - pd.Timedelta(minutes=2)
    idx = pd.date_range(end=end, periods=bars, freq=freq)
    price = np.linspace(24_500.0, 24_600.0, bars)
    return pd.DataFrame(
        {
            "open": price,
            "high": price + 5.0,
            "low": price - 5.0,
            "close": price,
            "volume": [float(v) for v in vols],
        },
        index=idx,
    )


def _measured(tail, **kw):
    return VolumeProfiler().analyze(_frame(tail, **kw), "NIFTY")


# ── the live capture ─────────────────────────────────────────────────────────

def test_the_live_nifty_tail_no_longer_reads_as_no_participation():
    """0.011x was the symptom. Anything near it means the walk-back never happened.

    `available` is asserted alongside the ratio deliberately: the UNAVAILABLE result
    shape carries volume_ratio=1.0 as a neutral sentinel, so a ratio bound ALONE is
    satisfied by the profiler giving up. That is not the outcome under test.
    """
    res = _measured(LIVE_NIFTY_TAIL)
    assert res["available"] is True, "gave up instead of finding the complete bar"
    assert res["volume_ratio"] > 0.25, (
        f"still measuring inside the dead zone: {res['volume_ratio']:.3f}x"
    )


def test_it_measures_the_last_complete_bar_not_the_one_behind_the_stub():
    """210,220 is the newest bar holding a real observation."""
    assert _measured(LIVE_NIFTY_TAIL)["current_volume"] == 210_220


def test_a_single_step_back_would_not_have_been_enough():
    """Guards the actual regression: bar -2 of the live tail is itself a stub."""
    assert LIVE_NIFTY_TAIL[-2] == 5655
    assert _measured(LIVE_NIFTY_TAIL)["current_volume"] != 5655


@pytest.mark.parametrize(
    "name,tail,avg",
    [
        ("BANKNIFTY", [544516, 361596, 271440, 12000, 870, 0], 472_822),
        ("SENSEX", [31966, 19971, 24407, 900, 60, 0], 24_399),
        ("MIDCPNIFTY", [451585, 647909, 232597, 5000, 240, 0], 316_548),
    ],
)
def test_every_index_that_showed_zero_now_reads(name, tail, avg):
    res = VolumeProfiler().analyze(_frame(tail, healthy=avg), name)
    assert res["available"] is True, f"{name} gave up instead of measuring"
    assert res["volume_ratio"] > 0.25, f"{name} still reads {res['volume_ratio']:.3f}x"


# ── the honest-absence case ──────────────────────────────────────────────────

def test_a_dead_feed_reports_absent_rather_than_a_phantom_zero():
    """A run of stubs longer than the backfill lag is a dead feed.

    available=False makes the aggregator's volume pillar abstain. A tiny ratio with
    available=True makes it SCORE the phantom — strictly worse than saying nothing.
    """
    res = _measured([100, 90, 80, 70, 60, 50, 40, 30])
    assert res["available"] is False


def test_the_walkback_is_bounded():
    """Unbounded, a quiet afternoon would be skipped until it found a busy morning."""
    assert MAX_STUB_WALKBACK <= 6


# ── what must NOT change ─────────────────────────────────────────────────────

def test_a_healthy_series_still_measures_its_own_last_bar():
    res = _measured([500_000] * 6)
    assert res["current_volume"] == 500_000
    assert res["available"] is True


def test_a_genuinely_quiet_bar_is_still_measured_not_skipped():
    """A quiet bar runs at tens of percent of the median. Only a stub is ~1%."""
    res = _measured([500_000, 500_000, 500_000, 500_000, 500_000, 150_000])
    assert res["current_volume"] == 150_000, "a quiet bar was mistaken for a stub"


def test_one_trailing_stub_still_steps_back_exactly_one():
    """The original single-bar case must keep working."""
    res = _measured([500_000, 500_000, 500_000, 500_000, 480_000, 300])
    assert res["current_volume"] == 480_000


def test_a_short_frame_does_not_raise():
    df = _frame([500_000] * 3, bars=12)
    assert VolumeProfiler().analyze(df, "NIFTY") is not None


# ── the wiring ───────────────────────────────────────────────────────────────

def test_the_walkback_loop_is_actually_in_the_measuring_path():
    """A helper wired to nothing is the dominant defect pattern in this codebase."""
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(VolumeProfiler.analyze))
    tree = ast.parse(src)
    names = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_stub_at" in names, "the stub test is defined but never consulted"
    assert "MAX_STUB_WALKBACK" in src, "the walk-back bound is not applied here"
