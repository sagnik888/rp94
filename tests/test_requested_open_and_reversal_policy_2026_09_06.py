from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from config.settings import Settings, get_settings
from engine.ut_bot_core import UTBotEngine
from scanner import Scanner


def _frame(index, price, volume=1000.0):
    closes = [float(price)] * len(index)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "volume": [float(volume)] * len(index),
        },
        index=index,
    )


def test_requested_session_and_reversal_defaults():
    fields = Settings.model_fields

    assert fields["ut_session_start"].default == "09:15"
    assert fields["live_signal_5min_warmup_bars"].default == 1
    assert fields["post_exit_reversion_fast_move_pct"].default == 0.30
    assert fields["post_exit_reversion_slow_move_pct"].default == 0.40
    assert fields["stabilization_override_drop_regime_flip"].default is True


def test_all_strategy_timeframes_share_the_0915_session_gate():
    engine = UTBotEngine(session_filter=True, session_start="09:15")

    for timeframe in ("1min", "5min", "15min"):
        assert engine._in_session(datetime(2026, 9, 4, 9, 15), timeframe) is True
        assert engine._in_session(datetime(2026, 9, 4, 9, 14, 59), timeframe) is False


def test_first_live_bar_is_eligible_but_clears_previous_session_state(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_signal_5min_warmup_bars", 1)
    prior = _frame(pd.date_range("2026-09-03 15:10", periods=2, freq="5min"), 100.0)
    today = _frame(pd.date_range("2026-09-04 09:15", periods=1, freq="5min"), 105.0)
    data = pd.concat([prior, today])

    engine = SimpleNamespace(_states={"NIFTY_5min": object()})
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._get_current_session_day = lambda: "2026-09-04"
    scanner._cached_results = {"NIFTY_5min": {"signals": ["old"]}}
    scanner._cached_result_metadata = {"NIFTY_5min": {"old": True}}
    scanner._candidate_process_cache = {"NIFTY": {"old": True}}
    scanner._last_signal_time = {"NIFTY_5min": datetime(2026, 9, 3, 15, 10)}
    scanner._last_signal_identity = {"NIFTY_5min": "old"}
    scanner.mtf = SimpleNamespace(
        _result_cache={"NIFTY_5min": {"old": True}},
        _data_hash={"NIFTY_5min": 1},
        _data_cache={"NIFTY_5min": prior},
        engines={"NIFTY_5min": engine},
    )
    scanner.log_signal_decision_once = lambda *args, **kwargs: None

    prepared = scanner._prepare_live_signal_data("NIFTY", "5min", data)

    assert prepared is not None
    assert prepared.index[-1] == pd.Timestamp("2026-09-04 09:15")
    assert "NIFTY_5min" not in scanner._cached_results
    assert "NIFTY_5min" not in engine._states


def test_historical_intelligence_uses_requested_timeframe_candles():
    frames = {
        "1min": _frame(pd.date_range("2026-09-04 09:15", periods=21, freq="1min"), 100.0),
        "5min": _frame(pd.date_range("2026-09-04 09:15", periods=5, freq="5min"), 500.0),
        "15min": _frame(pd.date_range("2026-09-04 09:15", periods=2, freq="15min"), 1500.0),
    }
    scanner = Scanner.__new__(Scanner)
    scanner.simulation_id = 1
    scanner._historical_intel_at_cache = {}
    scanner._historical_day_type_cache = {}
    scanner.candles = SimpleNamespace(get_candles=lambda _instrument, tf: frames[tf])
    scanner._previous_close_at = lambda *args: 500.0
    scanner._get_historical_vix = lambda _ts: 15.0
    scanner._vix_percentile = lambda *_args: 50.0
    scanner._days_to_expiry_for = lambda *_args: 3
    scanner._build_intelligence_contexts = lambda *args, **kwargs: {
        "session_context": {}, "anchor_regime": {}, "breadth_context": {}
    }
    seen = []

    def analyze(**kwargs):
        seen.append(
            (kwargs["timeframe"], len(kwargs["candle_df"]), float(kwargs["candle_df"]["open"].iloc[0]))
        )
        return {"aggregate": {"score": 0.0}, "regime": {"regime": "UNKNOWN"}}

    scanner.intel = SimpleNamespace(analyze=analyze)
    at = datetime(2026, 9, 4, 9, 35)
    for timeframe in ("1min", "5min", "15min"):
        scanner._compute_historical_intel_at("NIFTY", at, timeframe)

    assert seen == [("1min", 21, 100.0), ("5min", 5, 500.0), ("15min", 2, 1500.0)]


def _outside_window_dump(start, move_pct):
    index = pd.date_range("2026-09-04 09:45", periods=15, freq="1min")
    flat = [float(start)] * 10
    step = float(start) * float(move_pct) / 100.0 / 5.0
    closes = flat + [float(start) - step * (i + 1) for i in range(5)]
    frame = _frame(index, start)
    frame["open"] = closes
    frame["high"] = [value + 2.0 for value in closes]
    frame["low"] = [value - 3.0 for value in closes]
    frame["close"] = closes
    frame.loc[index[-6]:, "volume"] = 3000.0
    return frame


def test_sub_035_outside_window_shock_requires_strong_setup(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "market_ltp_shock_session_start", "13:00")
    monkeypatch.setattr(settings, "market_ltp_shock_session_end", "15:15")
    monkeypatch.setattr(settings, "market_ltp_shock_min_move_pct", 0.12)
    monkeypatch.setattr(settings, "market_ltp_shock_outside_strong_move_pct", 0.35)
    frames = {
        "NIFTY": _outside_window_dump(25000.0, 0.25),
        "BANKNIFTY": _outside_window_dump(55000.0, 0.24),
    }
    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = list(frames)
    scanner.candles = SimpleNamespace(
        get_candles=lambda instrument, _tf: frames[instrument],
        get_latest_price=lambda instrument: float(frames[instrument]["close"].iloc[-1]),
    )
    at = datetime(2026, 9, 4, 9, 59)

    weak = scanner._evaluate_market_ltp_shock("NIFTY", "SELL", at, timeframe="1min")
    strong = scanner._evaluate_market_ltp_shock(
        "NIFTY",
        "SELL",
        at,
        timeframe="1min",
        setup_confidence=0.82,
        setup_adx=30.0,
        setup_intel_score=-0.70,
        trend_15m_agrees=True,
    )

    assert weak is None
    assert strong is not None
    assert strong["outside_quality_relax"] is True
    assert strong["min_move_pct"] == 0.12
