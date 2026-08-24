"""A Fyers history fetch returns the minute currently being traded, and we stored it.

`get_historical_candles` asks Fyers for intraday history on a ~60s cadence. The newest
candle in that response is the bar the market is in the middle of, holding only the
volume traded so far. It was merged straight into the 1m store, and the moment its
minute closed nothing downstream could tell it from a complete observation:

  * the profiler's wall-clock `forming` test only excuses a bar whose interval has yet
    to elapse, and this bar's has;
  * its stub walk-back only rejects bars under STUB_BAR_FRACTION (5%) of the local
    median, and a bar caught eight seconds into a minute holds ~13%.

Captured live on 2026-08-14. One fetch per index landed mid-bar and all four persisted
a partial 14:55 candle (`data_store/candles/<INDEX>_1min.csv`, written 14:55):

    fetch 14:55:05  NIFTY      14:55 =      0   median   557,659
    fetch 14:55:08  BANKNIFTY  14:55 = 36,075   average  349,417  -> 0.103x
    fetch 14:55:12  SENSEX     14:55 =  5,774   median    17,068  -> 0.34x
    fetch 14:55:15  MIDCPNIFTY 14:55 = 83,942   average  539,076  -> 0.156x

The captured fraction tracks the fetch's offset into the minute, which is what
identifies these as partial bars rather than a quiet tape.

The reading is not inert. It reports available=True, so every volume gate refuses on it
(2.5x power move, 1.6x thrust, 1.40x cascade, 1.05x scalp) and the volume pillar scores
a phantom "no participation" into the confidence of every candidate instead of
abstaining. It persists until the next fetch repairs the bar, which the same session's
log shows can be 4m47s later (BANKNIFTY 14:55:08 -> 14:59:55), and that session emitted
665 VOLUME RATIO LOW diagnostics across the four indices.
"""
import numpy as np
import pandas as pd
import pytest

from data.market_data import drop_forming_tail_candle
from intelligence.volume_profiler import STUB_BAR_FRACTION, VolumeProfiler

# The real BANKNIFTY tail, 2026-08-14 14:51..14:55 IST, from BANKNIFTY_1min.csv.
LIVE_BANKNIFTY_TAIL = [465886, 430065, 890948, 446737, 36075]
LIVE_PARTIAL_BAR = 36075
LIVE_ROLLING_AVG = 349417


def _frame(tail, healthy=350_000, bars=60):
    """A healthy session ending in `tail`, stamped so every bar's minute has closed.

    The last bar sits a full interval in the past, so the profiler's wall-clock test
    considers it finished — which is exactly the state the stored partial bar reaches
    one minute after it was fetched.
    """
    vols = [healthy] * (bars - len(tail)) + list(tail)
    end = pd.Timestamp.now().floor("min") - pd.Timedelta(minutes=2)
    idx = pd.date_range(end=end, periods=bars, freq="1min")
    price = np.linspace(57_500.0, 57_530.0, bars)
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


def test_the_partial_bar_is_above_the_stub_floor_that_would_have_caught_it():
    """This is why the existing walk-back cannot save the measurement."""
    local_median = float(np.median(LIVE_BANKNIFTY_TAIL[:-1]))
    assert LIVE_PARTIAL_BAR > STUB_BAR_FRACTION * local_median
    # ...and the ratio it produces still fails every volume gate in the system.
    assert 0.05 < LIVE_PARTIAL_BAR / LIVE_ROLLING_AVG < 1.05


def test_a_stored_partial_bar_is_measured_as_a_real_quiet_market():
    """The defect, stated in the profiler's own output."""
    result = VolumeProfiler().analyze(_frame(LIVE_BANKNIFTY_TAIL), "BANKNIFTY")
    assert result["available"] is True, "a phantom measurement, not an abstention"
    assert result["current_volume"] == LIVE_PARTIAL_BAR
    assert result["volume_ratio"] < 0.2
    assert result["is_surge"] is False


def test_trimming_the_forming_bar_restores_the_real_measurement():
    """With the partial bar never stored, the newest complete bar is measured instead."""
    result = VolumeProfiler().analyze(_frame(LIVE_BANKNIFTY_TAIL[:-1]), "BANKNIFTY")
    assert result["available"] is True
    assert result["current_volume"] == LIVE_BANKNIFTY_TAIL[-2] == 446737
    # A normal bar against a normal average: the gates get a usable number back.
    assert result["volume_ratio"] > 0.9


@pytest.mark.parametrize(
    "fetched_at, expect_kept",
    [
        ("2026-08-14 14:55:08", 4),  # mid-bar: the 14:55 candle is still forming
        ("2026-08-14 14:56:00", 5),  # exactly on the boundary: 14:55 has closed
        ("2026-08-14 14:59:55", 5),  # long after: nothing to trim
    ],
)
def test_only_the_unfinished_bar_is_dropped(fetched_at, expect_kept):
    idx = pd.to_datetime(
        ["2026-08-14 14:51", "2026-08-14 14:52", "2026-08-14 14:53",
         "2026-08-14 14:54", "2026-08-14 14:55"]
    )
    df = pd.DataFrame({"volume": [float(v) for v in LIVE_BANKNIFTY_TAIL]}, index=idx)
    kept = drop_forming_tail_candle(df, 1, "BANKNIFTY 1m", now=pd.Timestamp(fetched_at))
    assert len(kept) == expect_kept


def test_a_five_minute_bar_needs_five_minutes():
    """The interval is the bar's, not a fixed minute."""
    idx = pd.to_datetime(["2026-08-14 14:45", "2026-08-14 14:50", "2026-08-14 14:55"])
    df = pd.DataFrame({"volume": [1.0, 2.0, 3.0]}, index=idx)
    at_1457 = drop_forming_tail_candle(df, 5, "x", now=pd.Timestamp("2026-08-14 14:57"))
    at_1500 = drop_forming_tail_candle(df, 5, "x", now=pd.Timestamp("2026-08-14 15:00"))
    assert len(at_1457) == 2, "14:55 bar does not close until 15:00"
    assert len(at_1500) == 3


def test_earlier_bars_are_never_punched_out():
    """Only a contiguous tail is trimmed; a clock skew must not hole the frame."""
    idx = pd.to_datetime(["2026-08-14 14:51", "2026-08-14 14:52", "2026-08-14 14:53"])
    df = pd.DataFrame({"volume": [1.0, 2.0, 3.0]}, index=idx)
    kept = drop_forming_tail_candle(df, 1, "x", now=pd.Timestamp("2026-08-14 14:54"))
    assert list(kept.index) == list(idx)


def test_a_frame_that_is_entirely_unfinished_is_kept_rather_than_emptied():
    """An empty frame sends the caller to Yahoo, which reports 0 volume for every index.

    Losing the whole series is a strictly worse outcome than one partial bar, so the
    trim declines rather than cascading into a volume-less source.
    """
    idx = pd.to_datetime(["2026-08-14 14:55"])
    df = pd.DataFrame({"volume": [36075.0]}, index=idx)
    kept = drop_forming_tail_candle(df, 1, "x", now=pd.Timestamp("2026-08-14 14:55:08"))
    assert len(kept) == 1


def test_a_frame_with_no_datetime_index_is_left_alone():
    df = pd.DataFrame({"volume": [1.0, 2.0]})
    assert drop_forming_tail_candle(df, 1, "x") is df


def test_history_days_back_is_untouched_outside_the_current_bar():
    """A multi-day backfill loses nothing but the bar in progress."""
    idx = pd.date_range("2026-08-13 09:15", "2026-08-14 14:55", freq="1min")
    df = pd.DataFrame({"volume": np.full(len(idx), 100.0)}, index=idx)
    kept = drop_forming_tail_candle(df, 1, "x", now=pd.Timestamp("2026-08-14 14:55:08"))
    assert len(kept) == len(df) - 1
    assert kept.index[-1] == pd.Timestamp("2026-08-14 14:54")
