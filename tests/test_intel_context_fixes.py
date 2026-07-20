"""Session-context plumbing fixes: previous-close staleness, breadth as-of,
VIX-open retry, and hourly-anchor smoothing warmup."""

import inspect
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from scanner import Scanner

IST = ZoneInfo("Asia/Kolkata")


def _ohlcv(index, closes, spread=0.5, volume=1000):
    closes = list(closes)
    return pd.DataFrame(
        {
            "open": [c - 0.1 for c in closes],
            "high": [c + spread for c in closes],
            "low": [c - spread for c in closes],
            "close": closes,
            "volume": [volume for _ in closes],
        },
        index=index,
    )


class _CandleStub:
    def __init__(self, frames):
        self._frames = frames

    def get_candles(self, instrument, timeframe):
        return self._frames.get(f"{instrument}_{timeframe}")


def test_previous_close_at_uses_frame_prior_day_for_replay():
    scanner = Scanner.__new__(Scanner)
    scanner._prev_close_cache = {"NIFTY": 25000.0}  # today's broker value

    prior_idx = pd.date_range("2026-06-25 09:15", periods=10, freq="5min")
    day_idx = pd.date_range("2026-06-26 09:15", periods=10, freq="5min")
    frame = pd.concat(
        [
            _ohlcv(prior_idx, [100.0 + i for i in range(10)]),
            _ohlcv(day_idx, [120.0 + i for i in range(10)]),
        ]
    )

    # Replay timestamp on 06-26 must use 06-25's last close from the frame,
    # never today's broker previous close.
    assert scanner._previous_close_at("NIFTY", frame, day_idx[-1]) == 109.0


def test_previous_close_at_broker_fallback_only_for_current_session():
    scanner = Scanner.__new__(Scanner)
    scanner._prev_close_cache = {"NIFTY": 25000.0}

    old_idx = pd.date_range("2026-06-26 09:15", periods=10, freq="5min")
    old_frame = _ohlcv(old_idx, [120.0 + i for i in range(10)])
    # Old-day timestamp with no prior day in frame: unknown, not today's value.
    assert scanner._previous_close_at("NIFTY", old_frame, old_idx[-1]) == 0.0

    today = datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0, tzinfo=None)
    today_idx = pd.date_range(today.replace(hour=9, minute=15), periods=10, freq="5min")
    today_frame = _ohlcv(today_idx, [120.0 + i for i in range(10)])
    # Current session without prior day in frame: broker value is the truth.
    assert scanner._previous_close_at("NIFTY", today_frame, today_idx[-1]) == 25000.0


def test_session_rollover_clears_session_anchored_caches():
    source = inspect.getsource(Scanner._rollover_session_if_needed)
    assert "_prev_close_cache" in source
    assert "_vix_session_open_cache" in source
    assert "_breadth_asof_cache" in source


def test_vix_open_miss_is_not_cached_for_the_session():
    scanner = Scanner.__new__(Scanner)
    scanner._vix_data = {}
    assert scanner._vix_open_for_session() is None

    today = datetime.now(IST).date().isoformat()
    scanner._vix_data = {f"{today} 09:16": 13.5, f"{today} 10:00": 14.0}
    assert scanner._vix_open_for_session() == 13.5
    # And the hit IS cached.
    scanner._vix_data = {}
    assert scanner._vix_open_for_session() == 13.5


def test_hourly_anchor_warms_detector_smoothing_and_is_deterministic():
    bars = []
    price = 100.0
    for day in range(25, 31):
        idx = pd.date_range(f"2026-06-{day:02d} 09:15", periods=75, freq="5min")
        closes = [price + i * 0.2 for i in range(75)]
        price = closes[-1] + 2.0
        bars.append(_ohlcv(idx, closes))
    frame = pd.concat(bars)

    scanner = Scanner.__new__(Scanner)
    first = scanner._hourly_anchor_regime("NIFTY", frame)
    second = scanner._hourly_anchor_regime("NIFTY", frame)

    assert first.get("timeframe") == "60min"
    assert int(first.get("smoothing_warmup_bars", 0)) >= 2
    assert first.get("regime") == second.get("regime")
    assert first.get("regime") != "UNKNOWN"


def test_breadth_context_as_of_does_not_read_live_cache():
    idx = pd.date_range("2026-06-26 09:15", periods=75, freq="5min")
    frames = {
        "NIFTY_5min": _ohlcv(idx, [100.0 + i * 0.3 for i in range(75)]),
        "BANKNIFTY_5min": _ohlcv(idx, [200.0 + i * 0.6 for i in range(75)]),
    }

    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ["NIFTY", "BANKNIFTY"]
    scanner.latest_regimes = {"NIFTY": "TRENDING_DOWN", "BANKNIFTY": "TRENDING_DOWN"}
    scanner.candles = _CandleStub(frames)

    def _live_cache_must_not_be_read(*args, **kwargs):
        raise AssertionError("historical breadth consulted the live intel cache")

    scanner._cached_timeframe_intel = _live_cache_must_not_be_read

    ctx = scanner._market_breadth_context(as_of=idx[-1])
    total = ctx["bullish_count"] + ctx["bearish_count"] + ctx["neutral_count"]
    assert total == 2
    # Steady uptrend frames must not be counted bearish from live state.
    assert ctx["bearish_count"] == 0


def test_breadth_context_live_path_still_uses_cache():
    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    scanner.latest_regimes = {}
    scanner._cached_timeframe_intel = lambda *a, **k: {
        "regime": {"regime": "TRENDING_UP"},
        "aggregate": {"score": 40.0},
    }

    ctx = scanner._market_breadth_context()
    assert ctx["bias"] == "BULLISH"
    assert ctx["aligned_count"] == 4
    assert ctx["avg_intel_score"] == 40.0
