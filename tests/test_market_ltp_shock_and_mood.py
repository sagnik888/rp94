"""Bidirectional late-session LTP shock + dynamic session mood."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from config.settings import get_settings
from engine.signal_processor import SignalProcessor, TradeCandidate
from intelligence.session_context import SessionContextAnalyzer
from scanner import Scanner


def _ohlcv(index, opens, highs, lows, closes, volume=1000):
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [volume for _ in closes],
        },
        index=index,
    )


def test_rolling_mood_detects_late_dump_and_flips_effective_day_type():
    """Full-day TREND_UP open + late dump → effective phase flips bearish."""
    prior_idx = pd.date_range("2026-07-08 14:00", periods=12, freq="5min")
    # Open trends up, then dump hard into the close pocket.
    morning = pd.date_range("2026-07-09 09:15", periods=60, freq="5min")
    prior = _ohlcv(
        prior_idx,
        [100 + i * 0.05 for i in range(12)],
        [101 + i * 0.05 for i in range(12)],
        [99 + i * 0.05 for i in range(12)],
        [100.5 + i * 0.05 for i in range(12)],
    )
    # Strong open break-up for full-day TREND_DAY_UP
    up_closes = [100.5 + i * 0.15 for i in range(48)]
    # Late dump ~0.5% over last 12 bars
    dump_base = up_closes[-1]
    dump_closes = [dump_base - i * 0.09 for i in range(1, 13)]
    closes = up_closes + dump_closes
    assert len(closes) == 60
    current = _ohlcv(
        morning,
        [100.4] + closes[:-1],
        [c + 0.20 for c in closes],
        [c - 0.25 for c in closes],
        closes,
        volume=2000,
    )
    # Amplify volume on dump window
    current.loc[current.index[-12]:, "volume"] = 5000

    result = SessionContextAnalyzer().analyze(
        pd.concat([prior, current]),
        instrument="NIFTY",
        spot_price=closes[-1],
        previous_close=100.0,
    )

    assert result["day_type"] == "TREND_DAY_UP"  # sticky open label preserved
    mood = result["intraday_mood"]
    assert mood["bias"] == "BEARISH"
    assert mood["day_type"] in {"TREND_DAY_DOWN", "MOOD_DUMP"}
    assert result["effective_day_type"] == "TREND_DAY_DOWN"
    assert result["effective_bias"] == "BEARISH"
    assert result["phase_confidence"] >= 0.55


def test_rolling_mood_detects_late_rally():
    prior_idx = pd.date_range("2026-07-08 14:00", periods=12, freq="5min")
    morning = pd.date_range("2026-07-09 09:15", periods=40, freq="5min")
    prior = _ohlcv(
        prior_idx,
        [100 for _ in range(12)],
        [101 for _ in range(12)],
        [99 for _ in range(12)],
        [100 for _ in range(12)],
    )
    # Chop then late rally
    base = [100.0 + ((i % 3) - 1) * 0.05 for i in range(28)]
    rally_start = base[-1]
    rally = [rally_start + i * 0.12 for i in range(1, 13)]
    closes = base + rally
    current = _ohlcv(
        morning,
        closes,
        [c + 0.15 for c in closes],
        [c - 0.10 for c in closes],
        closes,
        volume=1500,
    )
    current.loc[current.index[-8]:, "volume"] = 4000

    result = SessionContextAnalyzer().analyze(
        pd.concat([prior, current]),
        instrument="BANKNIFTY",
        spot_price=closes[-1],
        previous_close=100.0,
    )
    mood = result["intraday_mood"]
    assert mood["bias"] == "BULLISH"
    assert mood["day_type"] in {"TREND_DAY_UP", "MOOD_RALLY"}
    assert result["effective_bias"] in {"BULLISH", result["bias"]}


def _shock_1min_dump(start_px: float = 25000.0, bars: int = 20, dump_pct: float = 0.30):
    # Series ends 15:09 so dump (last 5m) is fully inside 14:45–15:11 tenure.
    idx = pd.date_range("2026-07-09 14:50", periods=bars, freq="1min")
    flat = [start_px] * (bars - 5)
    # last 5 bars dump dump_pct total
    step = start_px * (dump_pct / 100.0) / 5.0
    dump = [start_px - step * (i + 1) for i in range(5)]
    closes = flat + dump
    return _ohlcv(
        idx,
        closes,
        [c + 5 for c in closes],
        [c - 8 for c in closes],
        closes,
        volume=3000,
    )


def test_market_ltp_shock_detects_multi_index_dump(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "market_ltp_shock_enabled", True)
    monkeypatch.setattr(settings, "market_ltp_shock_min_move_pct", 0.20)
    monkeypatch.setattr(settings, "market_ltp_shock_min_indices", 2)
    monkeypatch.setattr(settings, "market_ltp_shock_min_core_indices", 2)
    monkeypatch.setattr(settings, "market_ltp_shock_min_volume_ratio", 1.0)
    monkeypatch.setattr(settings, "market_ltp_shock_session_start", "14:45")
    monkeypatch.setattr(settings, "market_ltp_shock_session_end", "15:11")

    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]

    frames = {
        "NIFTY": _shock_1min_dump(25000.0, dump_pct=0.32),
        "BANKNIFTY": _shock_1min_dump(55000.0, dump_pct=0.35),
        "SENSEX": _shock_1min_dump(82000.0, dump_pct=0.28),
        "MIDCPNIFTY": _shock_1min_dump(13000.0, dump_pct=0.22),
    }

    class _Candles:
        def get_candles(self, inst, tf):
            assert tf == "1min"
            return frames.get(inst)

        def get_latest_price(self, inst):
            df = frames.get(inst)
            return float(df["close"].iloc[-1]) if df is not None else 0.0

    scanner.candles = _Candles()
    # Dump ends ~15:14 on the series; evaluate at 15:09 (inside 14:45–15:11 tenure).
    ts = datetime(2026, 7, 9, 15, 9)
    ctx = scanner._evaluate_market_ltp_shock("NIFTY", "SELL", ts, timeframe="5min")
    assert ctx is not None
    assert ctx["side"] == "dump"
    assert ctx["total_votes"] >= 2
    assert "NIFTY" in ctx["indices"]
    assert ctx["avg_abs_move_pct"] >= 0.20


def test_market_ltp_shock_detects_multi_index_rally(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "market_ltp_shock_enabled", True)
    monkeypatch.setattr(settings, "market_ltp_shock_min_move_pct", 0.20)
    monkeypatch.setattr(settings, "market_ltp_shock_min_core_indices", 2)
    monkeypatch.setattr(settings, "market_ltp_shock_min_volume_ratio", 1.0)
    monkeypatch.setattr(settings, "market_ltp_shock_session_start", "14:45")
    monkeypatch.setattr(settings, "market_ltp_shock_session_end", "15:11")

    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX"]

    def _rally(start, pct=0.30):
        idx = pd.date_range("2026-07-09 14:55", periods=15, freq="1min")
        flat = [start] * 10
        step = start * (pct / 100.0) / 5.0
        up = [start + step * (i + 1) for i in range(5)]
        closes = flat + up
        return _ohlcv(idx, closes, [c + 5 for c in closes], [c - 3 for c in closes], closes, 2500)

    frames = {
        "NIFTY": _rally(25000.0, 0.33),
        "BANKNIFTY": _rally(55000.0, 0.30),
        "SENSEX": _rally(82000.0, 0.28),
    }

    class _Candles:
        def get_candles(self, inst, tf):
            return frames.get(inst)

        def get_latest_price(self, inst):
            df = frames.get(inst)
            return float(df["close"].iloc[-1]) if df is not None else 0.0

    scanner.candles = _Candles()
    # Rally is last 5 bars of 14:55+15 → ends ~15:09
    ctx = scanner._evaluate_market_ltp_shock(
        "BANKNIFTY", "BUY", datetime(2026, 7, 9, 15, 10), timeframe="1min"
    )
    assert ctx is not None
    assert ctx["side"] == "rally"
    assert ctx["signal_type"] == "BUY"


def test_market_ltp_shock_ignores_15min(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "market_ltp_shock_enabled", True)
    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ["NIFTY", "BANKNIFTY"]
    scanner.candles = MagicMock()
    assert (
        scanner._evaluate_market_ltp_shock(
            "NIFTY", "SELL", datetime(2026, 7, 9, 15, 5), timeframe="15min"
        )
        is None
    )


def test_shock_stabilization_is_short():
    scanner = Scanner.__new__(Scanner)
    scanner.latest_regimes = {}
    c = TradeCandidate(
        instrument="NIFTY",
        direction="SHORT",
        price=100.0,
        stop=105.0,
        target=90.0,
        lots=1,
        lot_size=25,
        grade="B+",
        confidence=0.7,
        timeframe="5min",
        inst_type="OPT",
        option_type="PE",
        atm_strike=25000,
        multiplier=-0.5,
        trading_symbol="NIFTY25JUL25000PE",
        symbol_token="1",
        market_ltp_shock_entry=True,
    )
    seconds, scans, adaptive = scanner._live_signal_stabilization_requirements(c)
    assert 5.0 <= seconds <= 10.0
    assert scans == 2
    assert adaptive is True


def test_settings_declare_shock_and_mood_knobs():
    """Assert CODE defaults via model_fields — never the operator's .env values."""
    from config.settings import Settings

    fields = Settings.model_fields
    assert fields["market_ltp_shock_enabled"].default is True
    assert fields["market_ltp_shock_min_move_pct"].default == 0.20
    assert fields["market_ltp_shock_strong_move_pct"].default == 0.35
    # Full-sensitivity window per operator spec 2026-07-10: 14:00–15:10 IST,
    # strong tier (0.35%) required outside the window.
    assert fields["market_ltp_shock_session_start"].default == "14:00"
    assert fields["market_ltp_shock_session_end"].default == "15:10"
    assert fields["market_ltp_shock_session_override_start"].default == "14:00"
    assert fields["market_ltp_shock_outside_window_mode"].default == "strong"
    assert fields["market_ltp_shock_outside_strong_move_pct"].default == 0.35
    assert fields["market_ltp_shock_stabilization_seconds"].default == 8.0
    assert fields["market_ltp_shock_force_exit_opposite"].default is True
    assert fields["market_ltp_shock_opposite_trade_relax"].default is True
    assert fields["session_mood_dynamic_enabled"].default is True
    assert fields["ut_5min_no_entry_after"].default  # still later than 15:01 for 5min path


def test_mood_horizons_expose_day_minute_quarter_hour():
    prior_idx = pd.date_range("2026-07-08 14:00", periods=12, freq="5min")
    current_idx = pd.date_range("2026-07-09 09:15", periods=40, freq="5min")
    prior = _ohlcv(
        prior_idx,
        [100 for _ in range(12)],
        [101 for _ in range(12)],
        [99 for _ in range(12)],
        [100 for _ in range(12)],
    )
    closes = [100 + i * 0.05 for i in range(40)]
    current = _ohlcv(
        current_idx,
        closes,
        [c + 0.2 for c in closes],
        [c - 0.2 for c in closes],
        closes,
        volume=1500,
    )
    result = SessionContextAnalyzer().analyze(
        pd.concat([prior, current]),
        instrument="NIFTY",
        spot_price=closes[-1],
        previous_close=100.0,
    )
    mh = result.get("mood_horizons") or {}
    assert "day" in mh and "minute" in mh and "quarter" in mh and "hour" in mh
    assert mh["day"]["day_type"]
    assert result.get("effective_day_type")
    assert float(result.get("bar_minutes") or 0) == 5.0


def test_shock_detector_respects_session_window(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "market_ltp_shock_enabled", True)
    monkeypatch.setattr(settings, "market_ltp_shock_session_start", "14:45")
    monkeypatch.setattr(settings, "market_ltp_shock_session_end", "15:11")
    monkeypatch.setattr(settings, "market_ltp_shock_min_move_pct", 0.20)
    monkeypatch.setattr(settings, "market_ltp_shock_min_core_indices", 2)
    monkeypatch.setattr(settings, "market_ltp_shock_min_volume_ratio", 1.0)

    def _dump(start=25000.0, pct=0.32):
        idx = pd.date_range("2026-07-09 14:50", periods=20, freq="1min")
        flat = [start] * 15
        step = start * (pct / 100.0) / 5.0
        dump = [start - step * (i + 1) for i in range(5)]
        closes = flat + dump
        return _ohlcv(idx, closes, [c + 5 for c in closes], [c - 8 for c in closes], closes, 3000)

    frames = {
        "NIFTY": _dump(25000.0),
        "BANKNIFTY": _dump(55000.0),
        "SENSEX": _dump(82000.0),
    }
    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX"]

    class _C:
        def get_candles(self, inst, tf):
            return frames.get(inst)

        def get_latest_price(self, inst):
            return float(frames[inst]["close"].iloc[-1])

    scanner.candles = _C()
    # Inside window + dump complete through 15:09 → can fire
    inside = scanner._evaluate_market_ltp_shock(
        "NIFTY", "SELL", datetime(2026, 7, 9, 15, 9), timeframe="5min"
    )
    assert inside is not None
    # Outside window (before 14:45) → idle
    early = scanner._evaluate_market_ltp_shock(
        "NIFTY", "SELL", datetime(2026, 7, 9, 14, 30), timeframe="5min"
    )
    assert early is None
    # Outside window (after 15:11) → idle
    late = scanner._evaluate_market_ltp_shock(
        "NIFTY", "SELL", datetime(2026, 7, 9, 15, 20), timeframe="5min"
    )
    assert late is None


def test_force_exit_opposite_is_wired_not_dead_code():
    """Regression: setting was declared but never read (dead knob)."""
    import inspect

    src = inspect.getsource(Scanner._market_ltp_shock_force_exit_opposite)
    handle = inspect.getsource(Scanner._handle_opposite_signal_exit)
    assert "market_ltp_shock_force_exit_opposite" in src
    assert "_market_ltp_shock_force_exit_opposite" in handle
    assert "LTP_SHOCK_OPPOSITE" in handle or "force_shock_exit" in handle


def test_force_exit_opposite_skips_repaint_wait_on_shock(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "market_ltp_shock_force_exit_opposite", True)
    monkeypatch.setattr(settings, "market_ltp_shock_enabled", True)
    monkeypatch.setattr(settings, "repaint_recheck_seconds", 20.0)

    trade = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        inst_type="OPT",
        current_price=100.0,
    )
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.trades = SimpleNamespace(open_trades={"t1": trade})
    scanner._pending_opposite_exit = {}
    scanner._last_opposite_exit_signal_time = {}
    scanner._active_session_entry_candidates = lambda *a, **k: []
    scanner._latest_trade_candidates = {}
    scanner.log_event = lambda *a, **k: None
    scanner.candles = SimpleNamespace(get_latest_price=lambda _i: 100.0)
    scanner._get_live_premium_cached = lambda _t: 100.0
    scanner._quad_union_opposite_exit_allowed = lambda *a, **k: True
    closed = []

    def _close(tid, price, reason, exit_time=None):
        closed.append((tid, reason))

    scanner._close_and_record = _close
    # Tagged shock on the flip signal
    sig = SimpleNamespace(
        signal_type="SELL",
        timestamp=datetime(2026, 7, 9, 15, 5, 0),
        price=100.0,
        market_ltp_shock_entry=True,
        market_ltp_shock_context={"side": "dump", "reason": "test"},
    )

    # Without force-exit wiring, first call would only start a 20s timer and
    # return False. With force-exit, it must close immediately.
    assert scanner._market_ltp_shock_force_exit_opposite("NIFTY", sig, "5min") is True
    handled = scanner._handle_opposite_signal_exit("NIFTY", sig, "5min")
    assert handled is True
    assert closed == [("t1", "LTP_SHOCK_OPPOSITE")]
    assert scanner._pending_opposite_exit == {}


def test_force_exit_opposite_disabled_still_waits_repaint(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "market_ltp_shock_force_exit_opposite", False)
    monkeypatch.setattr(settings, "market_ltp_shock_enabled", True)
    monkeypatch.setattr(settings, "repaint_recheck_seconds", 20.0)

    trade = SimpleNamespace(instrument="NIFTY", timeframe="5min", direction="LONG", inst_type="OPT")
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.trades = SimpleNamespace(open_trades={"t1": trade})
    scanner._pending_opposite_exit = {}
    scanner._last_opposite_exit_signal_time = {}
    scanner._active_session_entry_candidates = lambda *a, **k: []
    scanner._latest_trade_candidates = {}
    scanner.log_event = lambda *a, **k: None
    closed = []
    scanner._close_and_record = lambda *a, **k: closed.append(a)

    sig = SimpleNamespace(
        signal_type="SELL",
        timestamp=datetime(2026, 7, 9, 15, 5, 0),
        price=100.0,
        market_ltp_shock_entry=True,
    )
    assert scanner._market_ltp_shock_force_exit_opposite("NIFTY", sig, "5min") is False
    # First sighting only starts the timer — no close yet.
    assert scanner._handle_opposite_signal_exit("NIFTY", sig, "5min") is False
    assert closed == []
    assert scanner._pending_opposite_exit  # timer started


def test_force_exit_evaluates_shock_live_when_untagged(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "market_ltp_shock_force_exit_opposite", True)
    monkeypatch.setattr(settings, "market_ltp_shock_enabled", True)

    trade = SimpleNamespace(instrument="NIFTY", timeframe="5min", direction="LONG")
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.trades = SimpleNamespace(open_trades={"t1": trade})
    scanner._pending_opposite_exit = {}
    scanner.log_event = lambda *a, **k: None
    scanner._evaluate_market_ltp_shock = lambda *a, **k: {
        "side": "dump",
        "reason": "live eval",
        "indices": ["NIFTY", "BANKNIFTY"],
    }
    sig = SimpleNamespace(
        signal_type="SELL",
        timestamp=datetime(2026, 7, 9, 15, 5, 0),
        price=100.0,
    )
    assert scanner._market_ltp_shock_force_exit_opposite("NIFTY", sig, "5min") is True
    assert getattr(sig, "market_ltp_shock_entry", False) is True


def test_session_trend_guard_source_uses_effective_phase():
    import inspect

    source = inspect.getsource(SignalProcessor.process_best_signal)
    assert "effective_day_type" in source
    assert "market_ltp_shock" in source
    assert "session_mood_dynamic_enabled" in source
    assert "market_ltp_shock_session_override_start" in source
    assert "Opposite-trade relax" in source or "market_ltp_shock_opposite_trade_relax" in source


def test_current_day_type_prefers_effective_phase():
    scanner = Scanner.__new__(Scanner)
    # Dynamic phase only wins at/above session_mood_min_confidence (default 0.55).
    def _intel(base, tf, allow_fallback=True):
        return {
            "session_context": {
                "day_type": "TREND_DAY_UP",
                "opening_day_type": "TREND_DAY_UP",
                "effective_day_type": "TREND_DAY_DOWN",
                "market_phase": "TREND_DAY_DOWN",
                "phase_confidence": 0.70,
            }
        }

    scanner._cached_timeframe_intel = _intel
    assert scanner._current_day_type("NIFTY") == "TREND_DAY_DOWN"


def test_current_day_type_keeps_opening_when_phase_conf_low():
    scanner = Scanner.__new__(Scanner)

    def _intel(base, tf, allow_fallback=True):
        return {
            "session_context": {
                "day_type": "TREND_DAY_UP",
                "opening_day_type": "TREND_DAY_UP",
                "effective_day_type": "TREND_DAY_DOWN",
                "phase_confidence": 0.20,
            }
        }

    scanner._cached_timeframe_intel = _intel
    assert scanner._current_day_type("NIFTY") == "TREND_DAY_UP"
