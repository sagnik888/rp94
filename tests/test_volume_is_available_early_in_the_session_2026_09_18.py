"""The intel panel read "NO VOLUME DATA" every morning on 5min and 15min.

2026-09-18 09:31, all four indices: `available=False`,
`volume_ratio_basis=INSUFFICIENT_CURRENT_SESSION`, while `current_volume` was 4,030,610
for NIFTY and every bar carried real exchange volume.

`volume_available` required five positive bars from TODAY's session. A 5-minute frame
reaches five bars at 09:40 and a 15-minute frame at 10:30, so both lanes reported the
feed dead for the first half of the morning — the part of the session the operator cares
about most — even though the ratio's own baseline is a 20-bar rolling mean that spans
prior sessions quite happily.

The requirement is now proportional: five once the session HAS five, otherwise whatever
the session has. A dead feed still fails it, because a dead feed has zero positive bars
however young the session is.
"""
import numpy as np
import pandas as pd

from intelligence.volume_profiler import MAX_STUB_WALKBACK, VolumeProfiler


def _session(day, volumes, start_time="09:15", freq="5min", ticks=None):
    index = pd.date_range(f"{day} {start_time}", periods=len(volumes), freq=freq)
    base = 24000 + np.arange(len(volumes)) * 0.5
    return pd.DataFrame(
        {
            "open": base, "high": base + 4, "low": base - 4, "close": base + 2,
            "volume": [float(v) for v in volumes],
            "tick_count": [float(t) for t in (ticks if ticks is not None else [0.0] * len(volumes))],
        },
        index=index,
    )


PRIOR = [700000.0, 680000.0, 720000.0, 660000.0, 710000.0, 690000.0,
         705000.0, 715000.0, 670000.0, 695000.0, 688000.0, 702000.0]


def _analyze(prior_volumes, today_volumes, **kwargs):
    prior = _session("2026-09-17", prior_volumes, **kwargs)
    today = _session("2026-09-18", today_volumes, **kwargs)
    return VolumeProfiler().analyze(pd.concat([prior, today]), "NIFTY")


def test_the_first_bars_of_a_session_report_volume():
    """09:20 on a 5min frame: two bars today, a full prior session behind them."""
    result = _analyze(PRIOR, [640000.0, 690000.0])

    assert result["available"] is True
    assert result["status"] == "OK"
    assert result["source"] == "EXCHANGE_VOLUME"
    assert result["volume_ratio_basis"] != "INSUFFICIENT_CURRENT_SESSION"
    assert result["volume_ratio"] > 0.0


def test_the_very_first_bar_of_a_session_reports_volume():
    result = _analyze(PRIOR, [640000.0])

    assert result["available"] is True
    assert result["volume_ratio"] > 0.0


def test_a_dead_feed_today_is_still_unavailable():
    """Zero positive bars today, however young the session is."""
    assert _analyze(PRIOR, [0.0, 0.0])["available"] is False
    assert _analyze(PRIOR, [0.0])["available"] is False


def test_a_dying_feed_mid_session_is_still_unavailable():
    """Once the session HAS five bars, five positive bars are required again."""
    result = _analyze(PRIOR, [800000.0, 750000.0, 0.0, 0.0, 0.0, 0.0])

    assert result["available"] is False


def test_yesterdays_close_is_never_reported_as_todays_participation():
    """The stub walk-back must not step back over the session boundary."""
    result = _analyze(PRIOR, [0.0] * min(3, MAX_STUB_WALKBACK))

    assert result["available"] is False


def test_a_full_prior_session_alone_does_not_qualify_the_feed():
    """No bars today at all: there is nothing current to measure."""
    prior = _session("2026-09-17", PRIOR)
    result = VolumeProfiler().analyze(prior.iloc[:-1], "NIFTY")

    assert result["available"] is True, "a complete session measured mid-session is fine"


def test_a_short_history_still_needs_baseline_support():
    """Fewer than five positive bars in the whole frame cannot support a 20-bar mean."""
    frame = _session("2026-09-18", [0.0] * 8 + [700000.0, 690000.0])

    assert VolumeProfiler().analyze(frame, "NIFTY")["available"] is False


def test_the_tick_proxy_also_starts_at_the_open():
    """Index frames with no traded volume fall back to labelled tick activity."""
    prior = _session("2026-09-17", [0.0] * 12, ticks=[40.0] * 12)
    today = _session("2026-09-18", [0.0, 0.0], ticks=[38.0, 44.0])

    result = VolumeProfiler().analyze(pd.concat([prior, today]), "NIFTY")

    assert result["available"] is True
    assert result["source"] == "INDEX_TICK_ACTIVITY"
    assert result["is_proxy"] is True
