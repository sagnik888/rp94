"""Completed cash history must repair live-tick completeness through Intel."""
import numpy as np
import pandas as pd
import pytest

from data.candle_builder import CandleBuilder
from data.market_data import MarketDataProvider
from intelligence.volume_profiler import VolumeProfiler


def history(periods=150):
    index = pd.date_range("2026-10-22 09:15", periods=periods, freq="min")
    return pd.DataFrame(dict(open=100., high=102., low=99., close=101., volume=10_000.), index=index)


@pytest.mark.parametrize("instrument", ["NIFTY", "BANKNIFTY", "MIDCPNIFTY", "SENSEX"])
@pytest.mark.parametrize("timeframe", ["1min", "5min", "15min"])
def test_history_repairs_incomplete_ticks_and_reaches_intel(instrument, timeframe):
    bars = history()
    builder = CandleBuilder()
    builder.update_candles(instrument, bars.assign(volume=0., source="live_tick",
        volume_basis="", volume_complete=False), "1min")
    repaired = MarketDataProvider._attach_fyers_history_provenance(bars)
    builder.update_candles(instrument, repaired, "1min", mark_receipt=False)
    # A later price-only tick must not erase the independent cash measurement.
    builder.update_candles(instrument, bars.iloc[-1:].assign(volume=0., source="live_tick",
        volume_basis="", volume_complete=False), "1min")
    frame = builder.get_candles(instrument, timeframe)
    assert frame["volume_complete"].all()
    result = VolumeProfiler().analyze(frame, instrument)
    assert result["available"] is True
    assert result["current_volume"] == 10_000 * {"1min": 1, "5min": 5, "15min": 15}[timeframe]
    assert result["volume_ratio"] == pytest.approx(1.)
    assert result["is_proxy"] is False


@pytest.mark.parametrize("value,complete", [(0., True), (100., True),
    (np.nan, False), (np.inf, False), (-1., False), ("bad", False)])
def test_only_finite_nonnegative_history_quantities_are_complete(value, complete):
    frame = history(1).assign(volume=value)
    original = frame.copy(deep=True)
    annotated = MarketDataProvider._attach_fyers_history_provenance(frame)
    assert bool(annotated["volume_complete"].iloc[0]) is complete
    pd.testing.assert_frame_equal(frame, original)


def test_explicit_incomplete_history_is_not_certified():
    frame = history(2).assign(volume_complete=[False, True])
    result = MarketDataProvider._attach_fyers_history_provenance(frame)
    assert result["volume_complete"].tolist() == [False, True]


def test_missing_history_quantity_is_not_certified():
    result = MarketDataProvider._attach_fyers_history_provenance(history(1).drop(columns="volume"))
    assert not result["volume_complete"].any()
