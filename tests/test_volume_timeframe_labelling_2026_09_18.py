"""The volume panel number, and which yardstick produced it.

The operator read "0.7x" on the panel while the 1min frame was showing ~1.0x and asked
whether the aggregation across timeframes was broken. It is not: a 5min bar IS the sum of
its five 1min bars, and this file pins that. What differs is the RATIO, because each frame
divides the current bar by its own 20-bar mean of same-length bars. Five minutes of flow
against a 5min mean and one minute of flow against a 1min mean are two different
measurements, and both can be right at the same instant.

The real gap was that nothing on screen said which frame the number came from, so the two
readings looked like a contradiction. The bundle now carries the requested timeframe and
the profiler already carried the measured bar; both must reach the panel.
"""
from pathlib import Path

import pandas as pd
import pytest

from data.candle_builder import CandleBuilder
from intelligence.intelligence_aggregator import IntelligenceAggregator
from intelligence.volume_profiler import VolumeProfiler

ROOT = Path(__file__).resolve().parents[1]


def _one_min_frame(bars: int = 120, *, per_bar_volume: float = 100_000.0) -> pd.DataFrame:
    # A past session, so the profiler's forming-bar clock test is not wall-clock
    # dependent: a bar whose close is still in the future is skipped as forming, and a
    # fixture stamped "today" would then measure a different bar before 11:15 than after.
    idx = pd.date_range("2026-09-17 09:15:00", periods=bars, freq="1min")
    price = pd.Series(range(bars), dtype="float64") * 0.5 + 25000.0
    return pd.DataFrame(
        {
            "open": price.values,
            "high": (price + 5).values,
            "low": (price - 5).values,
            "close": (price + 1).values,
            # A rising last leg, so the 1min and 5min ratios cannot coincide by accident.
            "volume": [per_bar_volume * (3.0 if i >= bars - 5 else 1.0) for i in range(bars)],
        },
        index=idx,
    )


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """The production roll-up, so the bucket labels match what the live panel is fed."""
    return CandleBuilder._resample_ohlcv(df, rule)


# ── the aggregation itself ────────────────────────────────────────────────────


def test_a_five_minute_bar_is_the_sum_of_its_one_minute_children():
    one_min = _one_min_frame()
    five_min = _resample(one_min, "5min")

    # Buckets are labelled left/closed left in production: 09:15 covers 09:15..09:19.
    last_label = five_min.index[-1]
    children = one_min.loc[last_label: last_label + pd.Timedelta(minutes=4), "volume"]

    assert len(children) == 5
    assert float(five_min["volume"].iloc[-1]) == pytest.approx(float(children.sum()))


def test_the_profiler_measures_the_frame_it_was_handed():
    """current_volume is the bar of the supplied frame, not a 1min bar inside it."""
    one_min = _one_min_frame()
    five_min = _resample(one_min, "5min")

    one = VolumeProfiler().analyze(one_min, "NIFTY")
    five = VolumeProfiler().analyze(five_min, "NIFTY")

    assert one["available"] and five["available"]
    assert float(five["current_volume"]) == pytest.approx(float(five_min["volume"].iloc[-1]))
    assert float(one["current_volume"]) == pytest.approx(float(one_min["volume"].iloc[-1]))
    # Same tape, same instant, five times the flow in the 5min numerator.
    assert float(five["current_volume"]) == pytest.approx(5.0 * float(one["current_volume"]))


def test_the_ratio_differs_per_frame_and_that_is_not_a_bug():
    """Each frame divides by its OWN 20-bar mean, so the ratios must not be forced equal."""
    one_min = _one_min_frame()
    five_min = _resample(one_min, "5min")

    one = VolumeProfiler().analyze(one_min, "NIFTY")
    five = VolumeProfiler().analyze(five_min, "NIFTY")

    assert float(one["volume_ratio"]) != pytest.approx(float(five["volume_ratio"]), rel=0.02), (
        "identical ratios across frames would mean one of them is not measuring its own frame"
    )


# ── the label ─────────────────────────────────────────────────────────────────


def test_the_profiler_names_the_frame_and_the_bar_it_measured():
    five_min = _resample(_one_min_frame(), "5min")

    result = VolumeProfiler().analyze(five_min, "NIFTY")

    assert result["measurement_timeframe"] == "5min"
    assert str(result["measurement_timestamp"]).startswith(str(five_min.index[-1])[:16])
    assert int(result["measurement_lag_bars"]) == 0


def test_the_bundle_stamps_the_requested_timeframe_on_its_volume():
    """The panel is fed the 5min bundle; without this it could not say so."""
    one_min = _one_min_frame()
    five_min = _resample(one_min, "5min")

    bundle = IntelligenceAggregator().analyze(
        instrument="NIFTY",
        timeframe="5min",
        candle_df=five_min,
        candle_1min_df=one_min,
        spot_price=float(five_min["close"].iloc[-1]),
    )

    assert bundle["volume"]["timeframe"] == "5min"
    assert bundle["volume"]["measurement_timeframe"] == "5min"


def test_the_bundle_volume_is_the_requested_frame_not_the_one_minute_frame():
    one_min = _one_min_frame()
    five_min = _resample(one_min, "5min")

    bundle = IntelligenceAggregator().analyze(
        instrument="NIFTY",
        timeframe="5min",
        candle_df=five_min,
        candle_1min_df=one_min,
        spot_price=float(five_min["close"].iloc[-1]),
    )

    assert float(bundle["volume"]["current_volume"]) == pytest.approx(
        float(five_min["volume"].iloc[-1])
    )


# ── the panel ─────────────────────────────────────────────────────────────────
# Source pins: the browser is not executed here, so these prove only that the element
# exists and that the code writing it reads the payload keys above.


def test_the_panel_has_a_place_to_show_the_frame():
    html = ROOT.joinpath("dashboard", "static", "index.html").read_text(encoding="utf-8")

    assert 'id="intelVolFrame"' in html
    frame_at = html.index('id="intelVolFrame"')
    ratio_at = html.index('id="intelVolRatio"')
    assert frame_at < ratio_at, "the frame label belongs on the card title, above the number"


def test_the_panel_writes_the_frame_and_the_measured_bar():
    app = ROOT.joinpath("dashboard", "static", "js", "app.js").read_text(encoding="utf-8")

    assert "getElementById('intelVolFrame')" in app
    block = app[app.index("getElementById('intelVolFrame')") - 800: app.index("getElementById('intelVolFrame')") + 800]
    assert "vol.timeframe" in block
    assert "vol.measurement_timeframe" in block
    assert "vol.measurement_timestamp" in block


def test_the_page_carries_a_cache_buster_for_app_js():
    """Without one, an open dashboard keeps serving the app.js that had no frame label.

    The exact version is not pinned here on purpose: it bumps with every app.js change.
    """
    html = ROOT.joinpath("dashboard", "static", "index.html").read_text(encoding="utf-8")

    assert "app.js?v=" in html
