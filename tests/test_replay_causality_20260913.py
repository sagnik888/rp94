"""Causal replay acceptance: clock, executable prices and exact contracts."""
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from engine.option_history_store import OptionHistoryStore
from engine.signal_processor import SignalProcessor, _PointInTimeCandles, _estimate_opt_premium


@pytest.fixture
def processor(monkeypatch):
    monkeypatch.setattr("engine.signal_processor.get_settings", lambda: SimpleNamespace(
        ut_force_exit_time="15:25", backtest_live_exits_enabled=True,
        fifteen_min_one_min_support_exit_enabled=False,
    ))
    return SignalProcessor(SimpleNamespace(mode="HISTORICAL"))


def bars(rows, start="2026-09-11 09:30"):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                        index=pd.date_range(start, periods=len(rows), freq="1min"))


@pytest.mark.parametrize("timeframe,minutes", [("1min", 1), ("5min", 5), ("15min", 15)])
def test_signal_available_at_close_without_mutating_source_and_id(processor, timeframe, minutes):
    raw = SimpleNamespace(timestamp=pd.Timestamp("2026-09-11 03:45Z"), source_engine="utbot")
    copy = processor._historical_signal_for_replay(raw, timeframe)
    assert raw.timestamp == pd.Timestamp("2026-09-11 03:45Z")
    assert copy.timestamp == pd.Timestamp("2026-09-11 09:15") + pd.Timedelta(minutes=minutes)
    assert copy.source_signal_timestamp == raw.timestamp
    assert processor._historical_trade_id("H", "NIFTY", timeframe, copy) == processor._historical_trade_id("H", "NIFTY", timeframe, raw)


def run(processor, frame, **kwargs):
    return processor._simulate_actual_option_exit(
        frame, kwargs.pop("start", frame.index[0]), kwargs.pop("end", frame.index[-1] + pd.Timedelta(minutes=1)),
        sl_price=90, rr=3, natural_sl_distance=10, max_sl_pct=.6, **kwargs)


def test_entry_uses_open_and_manages_first_bar_after_signal_available(processor):
    frame = bars([(100, 111, 95, 110), (110, 115, 104, 112)])
    out = run(processor, frame)
    assert out["entry"] == 100
    assert out["entry_time"] == frame.index[0]
    assert out["exit"] == 112
    assert out["exit_time"] == frame.index[-1] + pd.Timedelta(minutes=1)
    assert len(out["price_path"]) == 2


def test_pre_signal_premium_stop_cannot_exit_a_15m_arrow(processor):
    frame = bars([(100, 101, 80, 95)] * 15 + [(177, 180, 175, 179), (179, 180, 178, 179)], start="2026-09-11 09:15")
    out = run(processor, frame, start=pd.Timestamp("2026-09-11 09:30"))
    assert out["entry"] == 177
    assert out["reason"] == "HISTORICAL"
    assert all(pd.Timestamp(row["time"]) >= pd.Timestamp("2026-09-11 09:30") for row in out["price_path"])


def test_gap_through_stop_fills_at_available_open(processor):
    out = run(processor, bars([(100, 102, 98, 100), (80, 85, 78, 83)]))
    assert out["reason"] == "SL HIT"
    assert out["exit"] == 80
    assert out["exit_time"] == pd.Timestamp("2026-09-11 09:31")


def test_ambiguous_both_levels_touch_is_not_optimistic_target(processor):
    out = run(processor, bars([(100, 101, 99, 100), (129, 135, 85, 110)]))
    assert out["reason"] == "SL HIT"
    assert out["exit"] == 90


def test_close_guard_cannot_fill_at_new_stop_outside_observed_prices(processor):
    def make_probe(*args, **kwargs):
        return lambda *a: ("SMART_PROFIT_LOCK", "new stop", 150, "UNKNOWN", [], [])
    processor._historical_live_management_probe = make_probe
    frame = bars([(100, 104, 98, 103)])
    out = run(processor, frame, qty=65, tf15_exit_scanner=SimpleNamespace(), tf15_timeframe="5min")
    assert out["exit"] == 103
    assert out["exit_time"] == frame.index[0] + pd.Timedelta(minutes=1)
    assert out["exit_trace"][0]["guard"] == "replay_input"
    assert len(out["input_sha256"]) == 64


def test_no_entry_after_cutoff_and_no_bridge_missing_minutes(processor):
    frame = bars([(100, 101, 99, 100)] * 4)
    assert run(processor, frame, end=frame.index[0]) is None
    assert run(processor, frame.drop(frame.index[1])) is None
    assert run(processor, frame, start=frame.index[0] - pd.Timedelta(minutes=5)) is None


def test_utc_and_ist_simulation_inputs_represent_same_instant(processor):
    frame = bars([(100, 102, 99, 101), (101, 102, 100, 101)])
    expected = run(processor, frame)
    frame.index = frame.index.tz_localize("Asia/Kolkata").tz_convert("UTC")
    actual = run(processor, frame)
    assert actual["entry_time"] == expected["entry_time"]
    assert actual["exit_time"] == expected["exit_time"]
    assert actual["input_sha256"] == expected["input_sha256"]


def test_point_in_time_view_excludes_partial_higher_timeframe_candle():
    frame = bars([(100, 101, 99, 100)] * 16, start="2026-09-11 09:15")
    view = _PointInTimeCandles(SimpleNamespace(get_candles=lambda *args: frame))
    view.as_of = pd.Timestamp("2026-09-11 09:29")
    assert view.get_candles("NIFTY", "15min").empty
    view.as_of = pd.Timestamp("2026-09-11 09:30")
    assert len(view.get_candles("NIFTY", "15min")) == 1
    assert view.get_candles("NIFTY", "1min").index[-1] == pd.Timestamp("2026-09-11 09:29")


def test_historical_trend_requires_closed_arrow_and_never_final_state(processor):
    raw = SimpleNamespace(timestamp=pd.Timestamp("2026-09-11 09:15"), signal_type="BUY")
    mtf = SimpleNamespace(results_15min={"signals": [raw]})
    assert not processor._trend_15m_agrees_at(mtf, pd.Timestamp("2026-09-11 09:29"), "BUY", closed_only=True)
    assert processor._trend_15m_agrees_at(mtf, pd.Timestamp("2026-09-11 09:30"), "BUY", closed_only=True)
    mtf.results_15min = {"state": {"position": 1}}
    assert not processor._trend_15m_agrees_at(mtf, pd.Timestamp("2026-09-11 09:30"), "BUY", closed_only=True)


def test_archive_rejects_nearest_strike_and_unknown_monthly_expiry(processor, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "data_store" / "candles"
    root.mkdir(parents=True)
    frame = bars([(100, 101, 99, 100)], start="2026-05-21 09:15")
    frame.to_csv(root / "NIFTY26MAY2623950CE_1min.csv")
    frame.to_csv(root / "NIFTY26MAY24000CE_1min.csv")
    processor._option_history_store = OptionHistoryStore(bundle_path=tmp_path / "bundle.parquet")
    missing, symbol = processor._load_cached_historical_option_contract("NIFTY", 24000, "CE", "1min", date(2026, 5, 21))
    assert missing.empty and symbol == ""
    frame.to_csv(root / "NIFTY26MAY2624000CE_1min.csv")
    exact, symbol = processor._load_cached_historical_option_contract("NIFTY", 24000, "CE", "1min", date(2026, 5, 21))
    assert not exact.empty
    assert symbol == "NIFTY26MAY2624000CE"
    assert exact.attrs["contract_expiry"] == "2026-05-26"
    assert exact.attrs["contract_strike"] == 24000


def test_estimated_premium_never_averages_future_same_day_vix():
    received = []
    scanner = SimpleNamespace(_vix_data={"2026-09-11 15:30": 100},
                              _get_historical_vix=lambda ts: received.append(ts) or 0)
    assert _estimate_opt_premium(10000, scanner, trade_date=date(2026, 9, 11)) == 120
    assert received == [pd.Timestamp("2026-09-11 00:00")]


@pytest.mark.parametrize("inst_type,basis,synthetic", [
    ("OPT", "SYNTHETIC_OPTION_ESTIMATE", True), ("FUT", "INDEX_PRICE_PROXY", False),
])
def test_modeled_prices_have_explicit_non_observed_replay_evidence(inst_type, basis, synthetic):
    evidence = SignalProcessor._historical_price_evidence(inst_type, entry_time=pd.Timestamp("2026-09-11 09:30"), shadow=inst_type == "FUT")
    assert evidence[0]["price_basis"] == basis
    assert evidence[0]["observed_option_prices"] is False
    assert evidence[0]["actual_futures_prices"] is False
    assert evidence[0]["synthetic_option_prices"] is synthetic
    assert evidence[0]["shadow"] is (inst_type == "FUT")


def test_observed_option_evidence_retains_hash_guards_and_original_result():
    actual = {"exit_trace": [{"guard": "replay_input", "input_sha256": "abc"}, {"guard": "SL HIT", "price": 90}]}
    evidence = SignalProcessor._historical_price_evidence("OPT", actual)
    assert evidence[0]["price_basis"] == "OBSERVED_OPTION_OHLC"
    assert evidence[0]["input_sha256"] == "abc"
    assert evidence[0]["observed_option_prices"] is True
    assert evidence[1] == actual["exit_trace"][1]
    assert "price_basis" not in actual["exit_trace"][0]


@pytest.mark.parametrize("factory", ["_historical_live_management_probe", "_historical_tf15_exit_probe"])
def test_replay_direct_guard_reader_gets_exact_full_context_and_known_age(factory):
    import inspect
    from scanner import Scanner

    frame = bars([(100, 101, 99, 100)] * 120, start="2026-09-11 09:15")
    calls = []
    def reader(instrument, at, timeframe):
        calls.append((instrument, at, timeframe))
        return {"aggregate": {"score_norm": -.8, "score": -80},
                "oi": {"available": True, "signal": "BEARISH"},
                "pcr": {"available": True, "signal": "BEARISH"},
                "derivative_context": {"available": True, "multiplier": .9},
                "data_quality": {"intelligence_usable": True, "age_seconds": 10},
                "as_of": pd.Timestamp(at).tz_localize("Asia/Kolkata").isoformat(),
                "timeframe": timeframe}
    source = SimpleNamespace(candles=SimpleNamespace(get_candles=lambda *a: frame),
                             active_indices=["NIFTY"], _get_historical_intel_context_at=reader,
                             _compute_historical_intel_at=lambda *a: (-.8, "RANGING"))
    kwargs = dict(instrument="NIFTY", direction="LONG", option_type="CE",
                  entry_time=pd.Timestamp("2026-09-11 10:00"), entry_price=100., stop_price=80., qty=65)
    if factory == "_historical_live_management_probe":
        kwargs.update(timeframe="15min", target_price=150.)
    processor = SignalProcessor(source)
    evaluate = getattr(processor, factory)(source, **kwargs)
    closed = inspect.getclosurevars(evaluate).nonlocals
    probe, trade, view = closed["probe"], closed["trade"], closed["candle_view"]
    view.as_of = pd.Timestamp("2026-09-11 10:01")
    original_live_reader = Scanner._fresh_exact_trade_intel
    exact = probe._fresh_exact_trade_intel(trade, evaluation_time=pd.Timestamp("2099-01-01"), allow_blocking=False)
    assert calls[-1] == ("NIFTY", pd.Timestamp("2026-09-11 10:01"), "15min")
    assert exact["oi"]["signal"] == "BEARISH"
    assert exact["derivative_context"]["multiplier"] == .9
    assert probe._get_trade_intel(trade) == exact
    assert probe._trade_like_market_intelligence_context(trade)["pcr"] == exact["pcr"]
    assert probe._intel_age_seconds(exact, "NIFTY", view.as_of) == 0
    assert probe._fifteen_min_chain_is_post_entry(trade, exact, view.as_of) is True
    assert Scanner._fresh_exact_trade_intel is original_live_reader
