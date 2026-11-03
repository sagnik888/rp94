"""Regression proof for the 11 September broker/intelligence audit repairs."""

from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd

from data.market_data import MarketDataProvider
from engine.alt_scanners import AltSignalEngine
from engine.multi_timeframe import MultiTimeframeEngine
from intelligence.order_flow import OrderFlowAnalyzer
from intelligence.pcr_engine import PCREngine
from intelligence.session_context import SessionContextAnalyzer
from intelligence.volume_profiler import VolumeProfiler
from scanner import Scanner


def _frame(volumes, closes=None, start="2026-09-11 09:15", freq="5min"):
    closes = closes or [100.0] * len(volumes)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": volumes,
            "volume_basis": ["fyers_index"] * len(volumes),
        },
        index=pd.date_range(start, periods=len(volumes), freq=freq),
    )


def test_futures_minute_context_is_unavailable_after_bucket_rollover_until_new_tick():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    import threading
    from collections import deque

    provider._index_futures_minute_lock = threading.RLock()
    provider._index_futures_minute_state = {
        "NIFTY": {
            "tradingsymbol": "NIFTYFUT", "minute": 100,
            "current_volume": 100.0, "completed": deque([100.0] * 6, maxlen=20),
            "last_at": 6059.0,
        }
    }
    context = provider.get_index_futures_minute_volume_context("NIFTY", now_ts=6061.0)
    assert context["available"] is False
    assert context["reliable"] is False


def test_volume_profiler_keeps_the_measured_spike_direction():
    profiler = VolumeProfiler()
    frame = _frame([100] * 26 + [500, 0, 0], [100] * 26 + [110, 109, 108])
    result = profiler.analyze(frame, "NIFTY")
    assert result["available"] is True
    assert result["signal"] == "BULLISH_VOLUME"
    assert result["measurement_timeframe"] == "5min"


def test_order_flow_abstains_when_recent_edge_has_no_measured_volume():
    frame = _frame([100] * 5 + [0] * 5, [100] * 5 + [101] * 5)
    result = OrderFlowAnalyzer().analyze(frame)
    assert result["available"] is False
    assert result["signal"] == "NEUTRAL"


def test_pcr_repeated_read_of_same_snapshot_does_not_change_history():
    engine = PCREngine()
    chain = pd.DataFrame({
        "strike": [100, 150, 200], "call_oi": [100, 100, 100],
        "put_oi": [150, 150, 150], "call_volume": [10, 10, 10],
        "put_volume": [10, 10, 10],
    })
    chain.attrs["expiry"] = "18SEP2026"
    chain.attrs["observation_id"] = "same-chain"
    first = engine.analyze(chain, "NIFTY")
    second = engine.analyze(chain, "NIFTY")
    assert first["pcr_history"] == second["pcr_history"] == [1.5]


def test_session_context_uses_immediate_prior_day_and_no_zero_volume_delta_vote():
    old = _frame([100] * 3, [100, 100, 100], "2026-09-09 09:15")
    old.loc[old.index[0], "high"] = 180
    prior = _frame([100] * 3, [100, 100, 100], "2026-09-10 09:15")
    prior.loc[prior.index[0], "high"] = 140
    today = _frame([0] * 6, [101] * 6, "2026-09-11 09:15")
    result = SessionContextAnalyzer().analyze(pd.concat([old, prior, today]), instrument="NIFTY")
    assert result["pdh"] == 140.0
    assert result["session_delta"]["source"] == "unavailable"
    assert result["session_delta"]["bias"] == "NEUTRAL"


def test_quad_state_uses_only_primitive_votes():
    engine = AltSignalEngine.__new__(AltSignalEngine)
    positions, _ctx = engine._quad_union_consensus_positions(
        np.array([1, -1]), np.array([1, 1]), np.array([1, -1])
    )
    assert positions.tolist() == [1, -1]


def test_mtf_input_hash_changes_when_volume_changes():
    frame = _frame([100, 100])
    first = MultiTimeframeEngine._hash_df(frame)
    frame.loc[frame.index[-1], "volume"] = 999
    assert MultiTimeframeEngine._hash_df(frame) != first


def test_mtf_input_hash_changes_when_a_recent_prior_bar_is_corrected():
    frame = _frame([100] * 25)
    first = MultiTimeframeEngine._hash_df(frame)
    frame.loc[frame.index[-10], "volume"] = 999
    assert MultiTimeframeEngine._hash_df(frame) != first


def test_trade_context_prefers_current_exact_timeframe_cache():
    scanner = Scanner.__new__(Scanner)
    scanner._fresh_exact_trade_intel = lambda *_a, **_k: {
        "aggregate": {"score_norm": -0.8}, "volume": {"available": True}
    }
    trade = SimpleNamespace(
        instrument="NIFTY", timeframe="5min",
        market_intelligence_context={"aggregate": {"score_norm": 0.8}},
    )
    context = scanner._trade_like_market_intelligence_context(trade)
    assert context["aggregate"]["score_norm"] == -0.8
