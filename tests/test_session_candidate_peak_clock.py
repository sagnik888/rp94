from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from scanner import Scanner


def _candidate():
    return SimpleNamespace(
        instrument="MIDCPNIFTY",
        direction="LONG",
        inst_type="FUT",
        price=14647.30,
        entry_spot=14630.70,
        signal_timestamp=datetime(2026, 6, 22, 13, 0, 0),
        entry_timestamp=datetime(2026, 6, 22, 13, 2, 41),
    )


def test_pre_entry_source_candle_extreme_cannot_create_session_peak():
    candles = pd.DataFrame(
        [
            {"timestamp": "2026-06-22 13:00:00", "open": 14630.0, "high": 14636.05, "low": 14629.0, "close": 14634.0},
            {"timestamp": "2026-06-22 13:01:00", "open": 14634.0, "high": 14636.0, "low": 14631.0, "close": 14633.0},
            {"timestamp": "2026-06-22 13:02:00", "open": 14633.0, "high": 14636.05, "low": 14632.0, "close": 14634.8},
        ]
    )
    candles["timestamp"] = pd.to_datetime(candles["timestamp"])
    candles = candles.set_index("timestamp")
    scanner = Scanner.__new__(Scanner)
    scanner.candles = SimpleNamespace(get_candles=lambda instrument, timeframe: candles)
    scanner._is_premium_long_candidate = lambda candidate: False

    peak_price = scanner._candidate_candle_extreme_price(
        _candidate(),
        datetime(2026, 6, 22, 13, 2, 42),
    )

    assert peak_price == 0.0


def test_first_full_post_entry_minute_can_contribute_to_peak():
    candles = pd.DataFrame(
        [
            {"timestamp": "2026-06-22 13:02:00", "open": 14633.0, "high": 14636.05, "low": 14632.0, "close": 14634.8},
            {"timestamp": "2026-06-22 13:03:00", "open": 14634.8, "high": 14638.0, "low": 14634.0, "close": 14637.0},
        ]
    )
    candles["timestamp"] = pd.to_datetime(candles["timestamp"])
    candles = candles.set_index("timestamp")
    scanner = Scanner.__new__(Scanner)
    scanner.candles = SimpleNamespace(get_candles=lambda instrument, timeframe: candles)
    scanner._is_premium_long_candidate = lambda candidate: False

    peak_price = scanner._candidate_candle_extreme_price(
        _candidate(),
        datetime(2026, 6, 22, 13, 3, 59),
    )

    assert peak_price == pytest.approx(14654.60)
