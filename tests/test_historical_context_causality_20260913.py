"""Decision-time regressions: changing unavailable observations cannot change a grade input."""
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from scanner import Scanner
from engine.signal_manager import SignalManager
from engine.ut_bot_core import UTBotSignal
from intelligence.session_context import SessionContextAnalyzer
from intelligence.intelligence_aggregator import IntelligenceAggregator
from data.derivative_participation import DerivativeParticipation


def frame(timeframe="5min", days=1):
    frames = []
    for day in pd.bdate_range("2026-09-01", periods=days):
        count = 375 // {"1min": 1, "5min": 5, "15min": 15}[timeframe]
        frames.append(pd.DataFrame(
            {"open": 100., "high": 101., "low": 99., "close": 100.,
             "volume": 100., "volume_basis": "fyers_index"},
            index=pd.date_range(day + pd.Timedelta(hours=9, minutes=15), periods=count, freq=timeframe)))
    return pd.concat(frames)


def scanner_with(frames):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner.simulation_id = 1
    scanner._vix_data = {}
    scanner.candles = SimpleNamespace(get_candles=lambda _index, tf: frames[tf])
    scanner.session_context = SessionContextAnalyzer()
    scanner._previous_close_at = lambda *args: 100.
    scanner._days_to_expiry_for = lambda *args: 3
    scanner._market_breadth_context = lambda **kwargs: {}
    return scanner


@pytest.mark.parametrize("index", ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"])
@pytest.mark.parametrize("timeframe", ["1min", "5min", "15min"])
def test_historical_intel_only_sees_finalized_inputs(index, timeframe):
    frames = {tf: frame(tf) for tf in ("1min", "5min", "15min")}
    cutoff = pd.Timestamp("2026-09-01 10:02")

    def evaluate(frames):
        scanner = scanner_with(frames)
        seen = []
        scanner.intel = SimpleNamespace(analyze=lambda **kwargs: seen.append(kwargs) or {})
        scanner._compute_historical_intel_at(index, cutoff.tz_localize("Asia/Kolkata"), timeframe)
        return seen[0]

    before = evaluate(frames)
    changed = {}
    for tf, original in frames.items():
        changed[tf] = original.copy()
        ends = original.index + pd.Timedelta(tf)
        changed[tf].loc[ends > cutoff, ["open", "high", "low", "close", "volume"]] = 9999.
    after = evaluate(changed)
    pd.testing.assert_frame_equal(before["candle_df"], after["candle_df"])
    pd.testing.assert_frame_equal(before["candle_1min_df"], after["candle_1min_df"])
    assert before["session_context"] == after["session_context"]
    assert before["spot_price"] == after["spot_price"] == 100.
    assert before["strike_interval"] == {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100, "MIDCPNIFTY": 25}[index]
    assert (before["candle_df"].index + pd.Timedelta(timeframe) <= cutoff).all()
    assert before["volatility_value"] is None
    assert before["volatility_source"] == "india_vix_unavailable"


def test_session_does_not_see_future_part_of_five_minute_bar():
    full = frame()
    cutoff = pd.Timestamp("2026-09-01 10:02")
    before = scanner_with({"5min": full})._build_intelligence_contexts("NIFTY", full, as_of=cutoff)
    changed = full.copy()
    changed.loc[pd.Timestamp("2026-09-01 10:00"), "high"] = 150.
    after = scanner_with({"5min": changed})._build_intelligence_contexts("NIFTY", changed, as_of=cutoff)
    assert before["session_context"] == after["session_context"]
    assert before["session_context"]["session_high"] == 101.


def test_prior_session_is_not_presented_as_current_before_first_closed_bar():
    full = frame(days=2)
    scanner = scanner_with({"5min": full})
    result = scanner._build_intelligence_contexts("NIFTY", full, as_of=pd.Timestamp("2026-09-02 09:16"))
    assert result["session_context"]["bars"] == 0
    assert result["session_context"]["day_type"] == "UNDECIDED"
    assert result["session_context"]["reason"] == "NO_CANDLES"


def test_hourly_anchor_has_identical_aware_and_naive_cutoffs():
    full = frame(days=9)
    scanner = scanner_with({"5min": full})
    seen = []
    scanner._detect_regime_warmed = lambda df, *args: seen.append(df) or {"regime": "TEST"}
    cutoff = pd.Timestamp("2026-09-10 10:02")
    naive = scanner._hourly_anchor_regime("NIFTY", as_of=cutoff)
    aware = scanner._hourly_anchor_regime("NIFTY", as_of=cutoff.tz_localize("Asia/Kolkata").tz_convert("UTC"))
    assert naive == aware
    pd.testing.assert_frame_equal(seen[0], seen[1])
    assert (seen[0].index + pd.Timedelta(hours=1) <= cutoff).all()
    changed = full.copy()
    changed.loc[changed.index + pd.Timedelta(minutes=5) > cutoff, ["high", "close"]] = 9999.
    scanner.candles = SimpleNamespace(get_candles=lambda *args: changed)
    scanner._hourly_anchor_regime("NIFTY", as_of=cutoff)
    pd.testing.assert_frame_equal(seen[0], seen[-1])
    assert scanner._hourly_anchor_regime("NIFTY", as_of="invalid") == {}


def test_breadth_does_not_use_unfinished_five_minute_candle():
    full = frame(days=2)
    scanner = scanner_with({"5min": full})
    seen = []
    scanner._detect_regime_warmed = lambda df, *args: seen.append(df.index[-1]) or {"regime": "RANGING"}
    scanner._breadth_regime_at("NIFTY", pd.Timestamp("2026-09-02 10:02", tz="Asia/Kolkata"))
    assert seen == [pd.Timestamp("2026-09-02 09:55")]


@pytest.mark.parametrize("key", ["2026-09-01 15:30:00", "2026-09-01 00:00:00"])
def test_future_vix_observation_is_unavailable(key):
    scanner = scanner_with({})
    scanner._vix_data = {key: 28.}
    cutoff = pd.Timestamp("2026-09-01 09:20")
    assert scanner._get_historical_vix(cutoff) == 0.
    assert scanner._historical_vix_context(cutoff)["available"] is False


def test_vix_daily_close_becomes_known_at_session_close():
    scanner = scanner_with({})
    scanner._vix_data = {"2026-09-01 00:00:00": 28.}
    assert scanner._get_historical_vix(pd.Timestamp("2026-09-01 15:29")) == 0.
    context = scanner._historical_vix_context(pd.Timestamp("2026-09-01 15:30"))
    assert context["value"] == 28.
    assert context["source"] == "india_vix_daily_close"
    assert context["available_at"] == "2026-09-01T15:30:00"


def test_vix_quote_uses_completed_minute_and_abstains_when_stale():
    scanner = scanner_with({})
    scanner._vix_data = {"2026-09-01 09:20:00": 14.}
    assert scanner._get_historical_vix(pd.Timestamp("2026-09-01 09:20")) == 0.
    assert scanner._get_historical_vix(pd.Timestamp("2026-09-01 09:21", tz="Asia/Kolkata")) == 14.
    assert scanner._get_historical_vix(pd.Timestamp("2026-09-01 09:30")) == 0.
    assert scanner._historical_vix_context(pd.Timestamp("2026-09-01 09:30"))["reason"] == "STALE_INTRADAY_VIX"


def test_vix_session_open_cannot_leak_from_a_later_cached_query():
    scanner = scanner_with({})
    scanner._vix_data = {"2026-09-01 09:25:00": 14.}
    assert scanner._vix_open_for_session(pd.Timestamp("2026-09-01 09:30")) == 14.
    assert scanner._vix_open_for_session(pd.Timestamp("2026-09-01 09:20")) is None


@pytest.mark.parametrize("missing", [0., None, float("nan"), -1.])
def test_unavailable_vix_cannot_grant_low_volatility_grade_bonus(missing):
    manager = SignalManager()
    signal = UTBotSignal(datetime(2026, 9, 1, 9, 30), "BUY", 100., 98., 2., 1., 25., 1, 10,
                         instrument="NIFTY", timeframe="5min")
    neutral = manager._grade_signal(signal, 0., 0., vix_value=15.)
    unavailable = manager._grade_signal(signal, 0., 0., vix_value=missing)
    assert neutral[:2] == unavailable[:2]
    assert signal.grade_pillars["vix_penalty"] == 0.
    assert signal.grade_pillars["vix_available"] is False


def test_higher_timeframe_flip_only_votes_after_bar_close():
    scanner = scanner_with({})
    scanner.mtf = SimpleNamespace(tf_weights={"15min": 1.})
    signal = SimpleNamespace(timestamp=pd.Timestamp("2026-09-01 09:15"), signal_type="BUY")
    result = SimpleNamespace(results_15min={"signals": [signal]})
    before = pd.Timestamp("2026-09-01 09:29", tz="Asia/Kolkata")
    after = pd.Timestamp("2026-09-01 09:30", tz="Asia/Kolkata")
    assert scanner._compute_historical_confluence_at(result, before) == 0.
    assert scanner._compute_historical_confluence_at(result, after) == 1.
    assert scanner._historical_15m_trend_agrees(result, before, "LONG") is False
    assert scanner._historical_15m_trend_agrees(result, after, "LONG") is True


def test_replay_intelligence_cannot_borrow_future_detector_history():
    frames = {tf: frame(tf, days=2) for tf in ("1min", "5min", "15min")}
    scanner = scanner_with(frames)
    scanner.intel = IntelligenceAggregator()
    cutoff = pd.Timestamp("2026-09-02 12:00")
    baseline = scanner._compute_historical_intel_at("NIFTY", cutoff, "5min")
    scanner._historical_intel_at_cache.clear()
    scanner.intel.regime._regime_history = {"NIFTY_5min": ["TRENDING_DOWN"] * 50}
    scanner.intel.regime._regime_history_bar = {"NIFTY_5min": "2026-09-11 15:25:00"}
    future = list(scanner.intel.regime._regime_history["NIFTY_5min"])
    assert scanner._compute_historical_intel_at("NIFTY", cutoff, "5min") == baseline
    assert scanner.intel.regime._regime_history["NIFTY_5min"] == future
    assert scanner.intel.regime._regime_history_bar["NIFTY_5min"] == "2026-09-11 15:25:00"


def test_recorded_chain_reaches_full_historical_context_without_future_response():
    frames = {tf: frame(tf) for tf in ("1min", "5min", "15min")}
    cutoff = pd.Timestamp("2026-09-01 10:02", tz="Asia/Kolkata")
    store = DerivativeParticipation()
    chain = pd.DataFrame({"strike": [95., 100., 105.], "call_oi": [100., 200., 300.],
                          "put_oi": [300., 200., 100.]})
    chain.attrs["expiry"] = "2026-09-08"
    quality = {"intelligence_usable": True, "expiry_aligned": True, "source": "kite",
               "strike_count": 3, "fallback": False, "stale": False}
    store.record_option_chain("NIFTY", chain, cutoff.timestamp() - 30, quality)
    future_chain = chain.copy()
    future_chain["call_oi"] = 9999.
    store.record_option_chain("NIFTY", future_chain, cutoff.timestamp() + 30, quality)
    scanner = scanner_with(frames)
    scanner.data = SimpleNamespace(get_recorded_option_chain=store.recorded_option_chain)
    calls = []
    scanner.intel = SimpleNamespace(analyze=lambda **kwargs: calls.append(kwargs) or {
        "aggregate": {"score_norm": .2}, "regime": {"regime": "TRENDING_UP"},
        "pcr": {"available": True, "pcr": 1.},
    })
    result = scanner._get_historical_intel_context_at("NIFTY", cutoff, "1min")
    pd.testing.assert_frame_equal(calls[0]["options_chain"], chain)
    assert calls[0]["chain_quality"]["recorded_at"] == cutoff.timestamp() - 30
    assert calls[0]["days_to_expiry"] == 7
    assert result["historical_data_availability"]["expiry_context"]["contract_expiry"] == "2026-09-08"
    assert result["pcr"]["available"] is True
    assert result["timeframe"] == "1min"
    assert scanner._intel_age_seconds(result, "NIFTY", cutoff) == 0.
    assert result["historical_data_availability"]["option_chain_available"] is True
    assert scanner._historical_intel_coverage["by_index_timeframe"]["NIFTY_1min"]["option_chain_available"] == 1
    assert scanner._get_historical_intel_context_at("NIFTY", cutoff, "1min") == result
    assert len(calls) == 1
    scanner._historical_intel_result_at_cache.clear()
    assert scanner._get_historical_intel_context_at("NIFTY", cutoff, "1min") == result
    assert len(calls) == 2


@pytest.mark.parametrize("offset", [-121, 1])
def test_historical_chain_boundary_rejects_stale_or_future_reader_result(offset):
    frames = {tf: frame(tf) for tf in ("1min", "5min", "15min")}
    cutoff = pd.Timestamp("2026-09-01 10:02", tz="Asia/Kolkata")
    scanner = scanner_with(frames)
    scanner.data = SimpleNamespace(get_recorded_option_chain=lambda *args: (
        pd.DataFrame({"strike": [95., 100., 105.]}),
        {"intelligence_usable": True, "recorded_at": cutoff.timestamp() + offset}))
    calls = []
    scanner.intel = SimpleNamespace(analyze=lambda **kwargs: calls.append(kwargs) or {})
    result = scanner._get_historical_intel_context_at("NIFTY", cutoff, "1min")
    assert calls[0]["options_chain"] is None
    assert calls[0]["chain_quality"]["intelligence_usable"] is False
    assert result["historical_data_availability"]["option_chain_available"] is False
