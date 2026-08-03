"""Order flow must describe TODAY, not today plus half of yesterday.

Callers hand the analyzer a warm-up frame: scanner._live_analysis_window prepends up to
180 prior-session 1min bars, which the rolling-window consumers (regime detector, volume
profiler) genuinely need. Order flow is an intraday statistic, so summing the whole frame
diluted buy/sell pressure toward 1.0 and made the aggregator's divergence gate disagree
with a clean read on roughly a third of samples.

The sharper case is the first four minutes of the session. The len(df) < 5 guard was
applied to the WHOLE frame, so with hundreds of warm-up rows it passed while
`pos.iloc[-5:]` was 1-4 of today's bars padded with YESTERDAY'S CLOSING bars — emitting
BULLISH/BEARISH where a clean read is NEUTRAL. That signal is a hard entry veto
(scanner._bias_opposes_signal), a loss-guard severity vote and a peak-threat exit vote.
"""
import numpy as np
import pandas as pd
import pytest

from intelligence.order_flow import OrderFlowAnalyzer


def _bars(day, n, close_pos, start_hour=9, start_min=15, volume=1000.0):
    """n 1min bars on `day` whose close sits at `close_pos` within each bar's range."""
    idx = pd.date_range(f"{day} {start_hour:02d}:{start_min:02d}", periods=n, freq="1min")
    low = np.full(n, 100.0)
    high = np.full(n, 110.0)
    close = low + (high - low) * close_pos
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close,
         "volume": np.full(n, volume)},
        index=idx,
    )


def test_yesterdays_pressure_is_excluded_from_todays_ratio():
    """Yesterday screaming bullish must not colour today's flat tape."""
    yesterday = _bars("2026-08-02", 180, close_pos=0.95, volume=5000.0)
    today = _bars("2026-08-03", 30, close_pos=0.50)

    contaminated = OrderFlowAnalyzer().analyze(pd.concat([yesterday, today]))
    clean = OrderFlowAnalyzer().analyze(today)

    assert contaminated["ratio"] == pytest.approx(clean["ratio"]), (
        "the prior session still moves today's buy/sell ratio"
    )
    assert contaminated["signal"] == clean["signal"] == "NEUTRAL"


def test_the_first_four_minutes_report_no_reading_instead_of_yesterdays():
    """1-4 live bars: pos.iloc[-5:] used to reach back into the prior session."""
    yesterday = _bars("2026-08-02", 180, close_pos=0.95, volume=5000.0)
    for live_bars in (1, 2, 3, 4):
        today = _bars("2026-08-03", live_bars, close_pos=0.50)
        out = OrderFlowAnalyzer().analyze(pd.concat([yesterday, today]))
        assert out["signal"] == "NEUTRAL", (
            f"{live_bars} live bar(s) produced {out['signal']!r} built from yesterday"
        )


def test_five_live_bars_are_enough_to_read_the_session():
    yesterday = _bars("2026-08-02", 180, close_pos=0.05, volume=5000.0)
    today = _bars("2026-08-03", 5, close_pos=0.95)
    out = OrderFlowAnalyzer().analyze(pd.concat([yesterday, today]))
    assert out["signal"] == "BULLISH", "today's own bars were not read"


def test_a_genuine_intraday_signal_still_comes_through():
    today = _bars("2026-08-03", 40, close_pos=0.90)
    assert OrderFlowAnalyzer().analyze(today)["signal"] == "BULLISH"

    bearish = _bars("2026-08-03", 40, close_pos=0.10)
    assert OrderFlowAnalyzer().analyze(bearish)["signal"] == "BEARISH"


def test_a_single_session_frame_is_unchanged_by_the_trim():
    """The overwhelmingly common shape must behave exactly as before."""
    today = _bars("2026-08-03", 120, close_pos=0.70)
    out = OrderFlowAnalyzer().analyze(today)
    assert out["ratio"] > 1.0 and out["signal"] == "BULLISH"
    assert out["buy_volume"] > out["sell_volume"]


def test_a_non_datetime_index_falls_through_untouched():
    """Tests and any RangeIndex caller must keep working."""
    df = _bars("2026-08-03", 30, close_pos=0.80).reset_index(drop=True)
    assert OrderFlowAnalyzer().analyze(df)["signal"] == "BULLISH"
