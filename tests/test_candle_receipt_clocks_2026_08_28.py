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


def test_higher_timeframe_receipts_advance_only_after_a_successful_resample(monkeypatch):
    builder = CandleBuilder()
    start = datetime(2026, 8, 28, 9, 15)
    bars = pd.DataFrame(
        [
            {"open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i, "volume": 10}
            for i in range(5)
        ],
        index=pd.date_range(start, periods=5, freq="1min"),
    )
    initial_receipt = start + timedelta(seconds=1)
    builder.update_candles("NIFTY", bars, "1min", received_at=initial_receipt)
    initial_5m = builder.latest_candle_receipt("NIFTY", "5min")
    initial_15m = builder.latest_candle_receipt("NIFTY", "15min")
    assert initial_5m == initial_receipt and initial_15m == initial_receipt

    # A throttled forming-bar update changes only the 1m frame; its aggregate receipt
    # must not impersonate a successful 5m/15m update.
    builder._last_resample_at["NIFTY"] = float("inf")
    deferred_receipt = start + timedelta(minutes=5)
    builder.update_candles(
        "NIFTY", _bar(106.0).set_axis(pd.DatetimeIndex([start + timedelta(minutes=5)])),
        "1min", received_at=deferred_receipt, throttle_resample=True,
    )
    assert builder.latest_candle_receipt("NIFTY", "1min") == deferred_receipt
    assert builder.latest_candle_receipt("NIFTY", "5min") == initial_5m
    assert builder.latest_candle_receipt("NIFTY", "15min") == initial_15m

    monkeypatch.setattr(builder, "_resample_and_merge", lambda *_args, **_kwargs: set())
    builder._last_resample_at["NIFTY"] = 0.0
    failed_receipt = start + timedelta(minutes=6)
    builder.update_candles(
        "NIFTY", _bar(107.0).set_axis(pd.DatetimeIndex([start + timedelta(minutes=6)])),
        "1min", received_at=failed_receipt,
    )
    assert builder.latest_candle_receipt("NIFTY", "1min") == failed_receipt
    assert builder.latest_candle_receipt("NIFTY", "5min") == initial_5m
    assert builder.latest_candle_receipt("NIFTY", "15min") == initial_15m
