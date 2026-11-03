"""Audit must preserve OHLC and disclose gaps rather than inventing candles."""
import pandas as pd
import pytest

from scripts.audit_ut_ema_session import reconstruct


def minutes():
    return pd.DataFrame({"open": [100., 101., 102., 103., 104.],
                         "high": [102., 103., 104., 105., 106.],
                         "low": [99., 100., 101., 102., 103.],
                         "close": [101., 102., 103., 104., 105.]},
                        index=pd.date_range("2026-10-29 09:15", periods=5, freq="min"))


def test_aggregation_retains_first_open_extremes_and_last_close():
    bars, complete = reconstruct(minutes(), "5min")
    assert bars.iloc[0].to_dict() == {"open": 100., "high": 106., "low": 99., "close": 105.}
    assert complete.tolist() == [True]


def test_missing_minute_is_disclosed_without_forward_filling():
    bars, complete = reconstruct(minutes().drop(pd.Timestamp("2026-10-29 09:17")), "5min")
    assert len(bars) == 1
    assert complete.tolist() == [False]
    # Adjacent sessions must not acquire synthetic overnight candles.
    next_day = minutes().copy()
    next_day.index += pd.Timedelta(days=1)
    bars, complete = reconstruct(pd.concat([minutes(), next_day]), "15min")
    assert len(bars) == 2
    assert complete.tolist() == [False, False]


def test_duplicate_minutes_cannot_claim_full_coverage():
    data = minutes()
    data.index = pd.DatetimeIndex([data.index[0], *data.index[:-1]])
    with pytest.raises(ValueError, match="ordered and unique"):
        reconstruct(data, "5min")
