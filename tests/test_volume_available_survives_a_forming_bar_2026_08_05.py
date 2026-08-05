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


def test_a_feed_that_stopped_several_bars_ago_is_unavailable():
    """Forgiving ONE forming bar must not forgive a feed that quit."""
    assert _available(LIVE + [0.0, 0.0, 0.0])[0] is False


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
