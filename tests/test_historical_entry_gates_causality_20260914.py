"""Entry decisions may use only bars/signals known at replay availability."""
from types import SimpleNamespace

import pandas as pd
import pytest

from scanner import Scanner
from engine.signal_processor import SignalProcessor


def candle_frame(tf="5min"):
    frame = pd.DataFrame({"open": 100., "high": 102., "low": 99.,
                          "close": 100., "volume": 1000.},
                         index=pd.date_range("2026-09-01 09:15", periods=26, freq=tf))
    frame.iloc[-2] = [100., 110., 100., 109., 8000.]
    frame.iloc[-1] = [109., 110., 90., 91., 1000.]
    return frame


def scanner_for(frame, mode="HISTORICAL"):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = mode
    scanner.candles = SimpleNamespace(get_candles=lambda *_a: frame)
    scanner.log_event = lambda *_a, **_kw: None
    return scanner


@pytest.mark.parametrize("tf", ["1min", "5min", "15min"])
@pytest.mark.parametrize("aware", [False, True])
@pytest.mark.parametrize("helper", ["_candle_thrust_metrics", "_directional_reclaim_snapshot",
                                    "_vwap_break_context", "_structure_break_context", "_detect_power_move"])
def test_historical_entry_evidence_ignores_next_bar(tf, aware, helper):
    frame = candle_frame(tf)
    decision = frame.index[-1]
    if aware:
        decision = decision.tz_localize("Asia/Kolkata").tz_convert("UTC")
    scanner = scanner_for(frame)
    expected = getattr(scanner_for(frame.iloc[:-1]), helper)("NIFTY", tf, "BUY", as_of=decision)
    observed = getattr(scanner, helper)("NIFTY", tf, "BUY", as_of=decision)
    assert observed == expected
    assert observed is not None
    if helper == "_candle_thrust_metrics":
        assert observed["volume_ratio"] == pytest.approx(8.)
        assert observed["close_location"] == pytest.approx(.9)


@pytest.mark.parametrize("tf", ["1min", "5min", "15min"])
def test_real_signal_start_keeps_source_bar(tf):
    frame = candle_frame(tf)
    metrics = scanner_for(frame, "REAL")._candle_thrust_metrics("NIFTY", tf, "SELL", as_of=frame.index[-1])
    assert metrics["close_location"] == pytest.approx(.95)


@pytest.mark.parametrize("depth", [{}, None])
@pytest.mark.parametrize("mode", ["HISTORICAL", "REAL"])
def test_depth_absence_does_not_fall_back_to_live_in_history(monkeypatch, depth, mode):
    scanner = scanner_for(candle_frame("1min"), mode)
    scanner._candle_thrust_metrics = lambda *_a, **_kw: {
        "volume_ratio": 8., "range_atr_ratio": 4., "close_location": .95}
    calls = []
    scanner._get_depth_context = lambda *_a: calls.append(True) or {"usable": True, "ofr": .9}
    monkeypatch.setattr(SignalProcessor, "_volume_spike_entry_confirmed", staticmethod(lambda **_kw: True))
    result = scanner._detect_volume_spike_entry("NIFTY", "1min", "BUY", depth_context=depth)
    assert result is not None
    assert len(calls) == int(mode == "REAL" and depth is None)
    assert result["depth_usable"] is bool(calls)


@pytest.mark.parametrize("tf", ["1min", "5min", "15min"])
def test_cached_relief_votes_require_bar_close_at_decision(tf):
    scanner = scanner_for(candle_frame(tf))
    decision = pd.Timestamp("2026-09-01 14:30")
    future = SimpleNamespace(timestamp=decision, signal_type="BUY")
    known = SimpleNamespace(timestamp=decision-pd.Timedelta(tf), signal_type="BUY")
    scanner._cached_results = {f"NIFTY_{tf}": {"signals": [future, known]}}
    selected, skew = scanner._cached_signal_near("NIFTY", tf, "BUY", decision, 1800.)
    assert selected is known
    assert skew == 0.
    assert scanner._entry_signal_clock(selected, tf) == decision
    scanner._cached_results[f"NIFTY_{tf}"]["signals"] = [future]
    assert scanner._cached_signal_near("NIFTY", tf, "BUY", decision, 1800.) == (None, None)


def test_replay_signal_clock_is_not_shifted_twice():
    scanner = scanner_for(candle_frame())
    decision = pd.Timestamp("2026-09-01 14:30")
    signal = SimpleNamespace(timestamp=decision, signal_available_at=decision,
                             source_signal_timestamp=decision-pd.Timedelta("5min"))
    assert scanner._entry_signal_clock(signal, "5min") == decision


def test_short_span_move_does_not_use_next_candle_close():
    frame = candle_frame("1min")
    decision = frame.index[-1]
    observed = scanner_for(frame)._index_short_span_move_pct("NIFTY", as_of=decision)
    expected = scanner_for(frame.iloc[:-1])._index_short_span_move_pct("NIFTY", as_of=decision)
    assert observed == expected == pytest.approx(9.)


def test_bad_historical_cutoff_abstains_instead_of_using_full_frame():
    scanner = scanner_for(candle_frame())
    for helper in ("_candle_thrust_metrics", "_vwap_break_context", "_structure_break_context", "_detect_power_move"):
        assert not getattr(scanner, helper)("NIFTY", "5min", "BUY", as_of="invalid clock")
    assert scanner._directional_reclaim_snapshot("NIFTY", "5min", "BUY", as_of="invalid clock") == {}


def test_cross_index_relief_never_accepts_future_votes(monkeypatch):
    import scanner as scanner_module
    scanner = scanner_for(candle_frame())
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    decision = pd.Timestamp("2026-09-01 14:30")
    monkeypatch.setattr(scanner_module, "get_settings", lambda: SimpleNamespace(
        market_wide_reversal_enabled=True, market_wide_reversal_core_votes=2,
        market_wide_reversal_total_votes=2))
    # session_prior_bars joined the metrics contract on 2026-09-18: a vote needs three
    # same-session bars behind the bar it scores, and a dict without the field fails closed.
    scanner._candle_thrust_metrics = lambda *_a, **_kw: {
        "range_atr_ratio": 3., "volume_ratio": 4., "close_location": .9,
        "session_prior_bars": 3}
    scanner._cached_results = {f"{name}_5min": {"signals": [SimpleNamespace(
        timestamp=decision, signal_type="BUY")]} for name in scanner.active_indices}
    assert scanner._market_wide_reversal_context("NIFTY", "5min", "BUY", decision) is None
    for result in scanner._cached_results.values():
        result["signals"][0].timestamp -= pd.Timedelta("5min")
    context = scanner._market_wide_reversal_context("NIFTY", "5min", "BUY", decision)
    assert context["total_votes"] == 4
    assert all(vote["timestamp"] <= decision for vote in context["votes"].values())
