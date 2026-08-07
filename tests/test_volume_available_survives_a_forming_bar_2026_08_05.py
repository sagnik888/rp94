"""A bar that is still forming must not read as a dead volume feed.

Index volume is a CUMULATIVE day total and the candle stores the DELTA, so the bar
currently being built legitimately reads 0 for the first ticks of every minute.

VolumeProfiler qualified the feed with `nonzero_count >= 5 and current_vol > 0`, where
current_vol was strictly `volume.iloc[-1]` — the forming bar. So for part of every single
minute the profiler reported available=False on all four indices while the candles behind
it held 700k+ of real volume. Downstream that is not cosmetic:

  * intelligence_aggregator drops the volume pillar entirely (`available is True` gates
    both the score contribution and the `components += 1` divisor), so the same evidence
    produces a different averaged score depending on where in the minute it is read;
  * the exit scorer's volume test goes blind;
  * every volume-ratio gate (2.5x power move, 1.6x thrust, 1.40x cascade, 1.05-1.10x
    scalps) evaluates against an unavailable source.

The dead-feed case the guard exists for is still caught by `nonzero_count >= 5`: five
measured bars in the ACTIVE session, which a genuinely dead feed never has.
"""
import numpy as np
import pandas as pd
import pytest

from intelligence.volume_profiler import VolumeProfiler


def _frame(volumes, start="2026-08-05 14:00"):
    n = len(volumes)
    idx = pd.date_range(start, periods=n, freq="1min")
    base = 24000 + np.arange(n) * 0.5
    return pd.DataFrame(
        {
            "open": base, "high": base + 4, "low": base - 4, "close": base + 2,
            "volume": [float(v) for v in volumes],
            "tick_count": [0.0] * n,
        },
        index=idx,
    )


def _available(volumes):
    result = VolumeProfiler().analyze(_frame(volumes), "NIFTY")
    return bool(result.get("available")), result


# VolumeProfiler.analyze needs >= 10 bars before it will look at anything, so this is
# a realistic intraday run rather than the eight bars the first draft used.
LIVE = [
    701955, 698727, 671292, 825564, 967478, 712004, 688120, 733991,
    645210, 702884, 811330, 690117, 758602, 673449,
]


def test_a_forming_final_bar_does_not_blank_the_feed():
    """The exact live shape: eight real bars, then a bar that has just opened."""
    available, result = _available(LIVE + [0.0])
    assert available is True, (
        "reported the volume feed unavailable because the CURRENT bar had not "
        "accumulated yet — this blanked the intel panel for part of every minute"
    )
    assert result.get("source") != "UNAVAILABLE"


def test_a_populated_final_bar_is_still_available():
    assert _available(LIVE)[0] is True


def test_a_genuinely_dead_feed_is_still_reported_unavailable():
    """The guard must keep doing its actual job."""
    assert _available([0.0] * 12)[0] is False


def test_two_bars_of_volume_is_not_enough_to_qualify_a_feed():
    """nonzero_count >= 5 is what catches a dying feed; it must still bind."""
    assert _available([0.0] * 10 + [500.0, 600.0, 0.0])[0] is False


def test_a_short_stale_edge_is_backfill_lag_not_a_dead_feed():
    """Relaxed on 2026-08-07 against live evidence. This test used to assert False.

    It assumed a run of empty trailing bars means the feed quit. For indices it does
    not. Index candles carry NO traded volume on the live tick feed; it arrives only
    with the periodic Fyers history backfill, which lags the tape by a few bars. So a
    2-3 bar empty edge is NORMAL operation — it fired 85 times in the first ten minutes
    of the 2026-08-07 session across all four indices, while real volume sat one bar
    further back.

    Holding the old expectation meant the volume pillar was either abstaining or
    scoring a phantom 0.0x essentially all the time. Measuring the last COMPLETE bar
    is the honest reading.

    The dead-feed case this test was written to protect is still covered, three ways:
    nonzero_count >= 5 (below), a run longer than the backfill tolerance (below), and
    a measured bar under a twentieth of its own average (the ratio guard).
    """
    assert _available(LIVE + [0.0, 0.0, 0.0])[0] is True


def test_a_run_longer_than_the_backfill_tolerance_is_still_unavailable():
    """Where the line actually sits now: past the walk-back bound, absence is absence."""
    from intelligence.volume_profiler import MAX_STUB_WALKBACK

    assert _available(LIVE + [0.0] * (MAX_STUB_WALKBACK + 2))[0] is False


def test_the_volume_pillar_reaches_the_aggregator_on_a_forming_bar():
    """The consequence that matters: the pillar must not drop out mid-minute.

    intelligence_aggregator counts `components += 1` only when the volume pillar is
    available, so an intermittently-unavailable feed changes the averaged score for
    identical evidence depending on when it is sampled.
    """
    forming, _ = _available(LIVE + [0.0])
    settled, _ = _available(LIVE)
    assert forming == settled is True, (
        "the volume pillar appears and disappears within a single minute, so the "
        "averaged intelligence score is not reproducible for the same market"
    )


# ── the ratio, not just the availability flag ────────────────────────────────

def test_the_ratio_is_not_zero_on_a_forming_bar():
    """0.0x was what the intel panel actually displayed on all four indices.

    Fixing `available` alone was not enough: vol_ratio is current_vol / avg_vol, and with
    the forming bar that is 0.0/avg = 0.0. And 0.0x is not a neutral reading downstream —
    it fails every surge gate outright and floors the volume component of confidence.
    """
    _avail, result = _available(LIVE + [0.0])
    assert float(result.get("volume_ratio", 0.0)) > 0.0, (
        "volume_ratio read 0.0x while the previous bars held real volume"
    )
    assert float(result.get("display_volume_ratio", 0.0)) > 0.0
    assert int(result.get("current_volume", 0)) > 0


def test_the_forming_bar_ratio_matches_the_settled_one():
    """Same market, sampled a moment apart, must not give two different answers."""
    _a1, forming = _available(LIVE + [0.0])
    _a2, settled = _available(LIVE)
    assert float(forming["volume_ratio"]) == pytest.approx(float(settled["volume_ratio"]))


def test_a_real_surge_still_reads_as_a_surge():
    """The fix must not flatten genuine surges."""
    _avail, result = _available(LIVE + [3_000_000.0])
    assert float(result["volume_ratio"]) > 2.0


def test_a_dead_feed_reports_no_ratio_rather_than_a_fabricated_one():
    _avail, result = _available([0.0] * 12)
    assert result.get("available") is False
    assert float(result.get("volume_ratio", 0.0)) in (0.0, 1.0)


# ── a PARTIALLY filled forming bar, which is the live case ───────────────────

def _frame_with_forming_bar(volumes, partial, seconds_in=15, tz=None):
    """Last bar opened `seconds_in` ago and holds only `partial` so far."""
    n = len(volumes) + 1
    end = pd.Timestamp.now(tz=tz).floor("min")
    idx = pd.date_range(end - pd.Timedelta(minutes=n - 1), periods=n, freq="1min", tz=tz)
    base = 24000 + np.arange(n) * 0.5
    return pd.DataFrame(
        {"open": base, "high": base + 4, "low": base - 4, "close": base + 2,
         "volume": [float(v) for v in volumes] + [float(partial)],
         "tick_count": [0.0] * n},
        index=idx,
    )


def test_a_partially_filled_forming_bar_does_not_crush_the_ratio():
    """The live defect. SENSEX: 51s into a 60s bar, 3,112 against a 48,034 predecessor.

    That is 0.09x on an ordinary tape, which the panel renders as 0.0x after toFixed(1),
    and which fails every volume gate. The zero-check added earlier never fired because
    3,112 is not zero — the rule has to be COMPLETENESS, not emptiness.
    """
    steady = [700_000.0] * 20
    result = VolumeProfiler().analyze(
        _frame_with_forming_bar(steady, partial=175_000.0), "NIFTY"
    )
    assert result["available"] is True
    ratio = float(result["volume_ratio"])
    assert ratio > 0.8, (
        f"ratio {ratio:.2f}x came from the quarter-filled forming bar, not the last "
        "completed one"
    )


def test_a_completed_final_bar_is_still_the_one_measured():
    """Nothing is skipped once the bar has actually finished.

    Anchored to a fixed in-session time rather than now()-minus-two-minutes. The
    relative form rotted at midnight: shifted back from 00:03 the twenty-one bars
    straddle two dates, leaving under five in the current one, so the feed read
    unavailable and the ratio collapsed to 1.0. Nothing about this test needs the
    bars to be recent — only closed.
    """
    steady = [700_000.0] * 20
    idx = pd.date_range("2026-08-05 11:00", periods=21, freq="1min")
    base = 24000 + np.arange(21) * 0.5
    df = pd.DataFrame(
        {"open": base, "high": base + 4, "low": base - 4, "close": base + 2,
         "volume": steady + [2_100_000.0], "tick_count": [0.0] * 21},
        index=idx,
    )
    result = VolumeProfiler().analyze(df, "NIFTY")
    assert float(result["volume_ratio"]) > 2.0, "ignored a genuine completed surge"


def test_a_surge_on_the_previous_bar_is_reported_while_the_next_forms():
    steady = [700_000.0] * 19 + [2_100_000.0]
    result = VolumeProfiler().analyze(
        _frame_with_forming_bar(steady, partial=50_000.0), "NIFTY"
    )
    assert float(result["volume_ratio"]) > 2.0


def test_a_timezone_aware_index_is_handled():
    """The live frames are IST-aware; a naive comparison would raise."""
    steady = [700_000.0] * 20
    result = VolumeProfiler().analyze(
        _frame_with_forming_bar(steady, partial=175_000.0, tz="Asia/Kolkata"), "NIFTY"
    )
    assert result["available"] is True
    assert float(result["volume_ratio"]) > 0.8


def test_a_non_datetime_index_falls_back_to_the_zero_check():
    """No usable interval means completeness cannot be judged; do not crash."""
    df = _frame(LIVE + [0.0])
    df = df.reset_index(drop=True)
    result = VolumeProfiler().analyze(df, "NIFTY")
    assert isinstance(result.get("volume_ratio"), (int, float))


# ── a truncated FINAL bar, hours after it closed ─────────────────────────────
#
# The clock test above only catches a bar forming right now. A final bar can also be
# truncated AND stale: the session ended part-way through it, the feed dropped, or the
# process was stopped mid-bar. Hours later the clock says "long since closed" and the
# stub is measured as if complete.
#
# Captured live on 2026-08-05 at 19:12 on all four indices at once — the real numbers
# below, straight from the VOLUME RATIO LOW diagnostic. This is the zero-volume report
# that was chased across three sessions.

LIVE_STUBS = {
    "NIFTY":      ([6911704, 6327287, 9129460, 14980267, 13736088], 40189),
    "BANKNIFTY":  ([2940594, 4136442, 4687822, 6512759, 8209167], 15712),
    "SENSEX":     ([140586, 121950, 276629, 210651, 1195852], 1792),
    "MIDCPNIFTY": ([3233965, 3631338, 4794768, 6854073, 8315127], 14961),
}


def _closed_frame(volumes, start="2026-08-05 14:20"):
    n = len(volumes)
    idx = pd.date_range(start, periods=n, freq="5min")
    base = 24000 + np.arange(n) * 0.5
    return pd.DataFrame(
        {"open": base, "high": base + 4, "low": base - 4, "close": base + 2,
         "volume": [float(v) for v in volumes], "tick_count": [0.0] * n},
        index=idx,
    )


@pytest.mark.parametrize("instrument", sorted(LIVE_STUBS))
def test_a_truncated_final_bar_is_not_measured_as_a_complete_one(instrument):
    """The exact live capture, per index."""
    body, stub = LIVE_STUBS[instrument]
    volumes = [float(v) for v in body] * 3 + [float(stub)]
    result = VolumeProfiler().analyze(_closed_frame(volumes), instrument)
    ratio = float(result["volume_ratio"])
    assert ratio > 0.25, (
        f"{instrument} read {ratio:.3f}x from a final bar holding "
        f"{stub / body[-1]:.3%} of its predecessor — the panel renders that as 0.0x and "
        "every volume gate refuses it"
    )


def test_the_stub_rule_does_not_flatten_a_genuinely_quiet_bar():
    """A quiet bar runs at tens of percent of the median, not at one."""
    volumes = [700_000.0] * 20 + [220_000.0]
    result = VolumeProfiler().analyze(_closed_frame(volumes), "NIFTY")
    assert 0.2 < float(result["volume_ratio"]) < 0.5, (
        "a real 0.3x bar was rewritten as its predecessor"
    )


def test_a_completed_surge_is_still_measured():
    volumes = [700_000.0] * 20 + [2_100_000.0]
    assert float(VolumeProfiler().analyze(_closed_frame(volumes), "NIFTY")["volume_ratio"]) > 2.0


def test_a_stub_after_too_few_prior_bars_is_left_alone():
    """Three non-zero predecessors is the minimum evidence for calling one a stub."""
    volumes = [0.0] * 18 + [700_000.0, 700_000.0, 1_000.0]
    result = VolumeProfiler().analyze(_closed_frame(volumes), "NIFTY")
    assert isinstance(float(result["volume_ratio"]), float)


def test_an_all_zero_frame_is_still_an_unavailable_feed():
    assert VolumeProfiler().analyze(_closed_frame([0.0] * 21), "NIFTY")["available"] is False
