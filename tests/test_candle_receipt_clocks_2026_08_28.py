"""The quote lane must not impersonate a candle transport receipt."""

from datetime import datetime, timedelta

import pandas as pd

from data.candle_builder import CandleBuilder


def _bar(value: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        [{"open": value, "high": value + 1, "low": value - 1, "close": value, "volume": 10}],
        index=pd.DatetimeIndex([datetime(2026, 8, 28, 9, 15)]),
    )


def test_quote_receipt_is_separate_and_does_not_mutate_ohlc():
    builder = CandleBuilder()
    builder.update_candles("NIFTY", _bar(), "1min")
    before = builder.get_candles("NIFTY", "1min").copy(deep=True)
    candle_receipt = builder.latest_candle_receipt("NIFTY", "1min")

    builder.update_latest_price("NIFTY", 103.0)

    after = builder.get_candles("NIFTY", "1min")
    assert after.equals(before)
    assert builder.latest_candle_receipt("NIFTY", "1min") == candle_receipt
    assert builder.latest_quote_receipt("NIFTY") is not None


def test_restored_candles_do_not_advance_receipt_clock_until_live_update():
    builder = CandleBuilder()
    builder.update_candles("NIFTY", _bar(), "1min", mark_receipt=False)
    assert builder.latest_candle_receipt("NIFTY", "1min") is None

    received = datetime.now() - timedelta(seconds=1)
    builder.update_candles("NIFTY", _bar(101.0), "1min", received_at=received)
    assert builder.latest_candle_receipt("NIFTY", "1min") == received


def test_malformed_explicit_receipt_does_not_fabricate_freshness():
    builder = CandleBuilder()
    builder.update_candles("NIFTY", _bar(), "1min", received_at="not-a-clock")
    assert builder.latest_candle_receipt("NIFTY", "1min") is None
