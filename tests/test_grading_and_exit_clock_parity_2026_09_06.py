from datetime import datetime, timedelta
import inspect
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from engine.signal_processor import SignalProcessor
from scanner import Scanner


def test_15min_fast_exit_hold_starts_at_candidate_acceptance(monkeypatch):
    now = datetime(2026, 9, 4, 13, 16, 5)
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(
            fifteen_min_one_min_support_exit_enabled=True,
            fifteen_min_one_min_support_min_hold_seconds=15.0,
        ),
    )
    scanner = Scanner.__new__(Scanner)
    scanner._profit_guard_reversal_context = lambda *args, **kwargs: {
        "has_context": True,
        "one_min_context": {"has_context": True, "reason": "large opposing move"},
        "one_min_emergency": True,
        "fast_reversal": True,
        "trend_supportive": False,
        "pullback_class": "big_reversal",
        "reason": "15m support failed",
    }
    candidate = SimpleNamespace(
        timeframe="15min",
        signal_timestamp=now - timedelta(minutes=15),
        entry_timestamp=now - timedelta(seconds=5),
    )

    allowed, reason = scanner._fifteen_min_one_min_support_exit_decision(
        candidate, 25_000.0, now
    )

    assert allowed is False
    assert "5.0s<15.0s" in reason


def test_trade_age_prefers_confirmed_fill_over_older_signal_clock():
    now = datetime(2026, 9, 4, 10, 0, 10)
    trade = SimpleNamespace(
        entry_filled_at=now - timedelta(seconds=3),
        entry_time=now - timedelta(minutes=2),
        signal_timestamp=now - timedelta(minutes=5),
    )

    assert Scanner._trade_like_age_seconds(trade, now) == pytest.approx(3.0)


def test_historical_eod_grading_uses_point_in_time_inputs_and_own_timeframe():
    source = inspect.getsource(SignalProcessor.process_best_signal)

    assert "eod_vix = scanner._get_historical_vix(last_s.timestamp)" in source
    assert "eod_intel_score, eod_intel_regime" in source
    assert "scanner.intel.regime.detect(sub_df, instrument, tf)" in source
    assert "intel_score=eod_intel_score" in source
    assert "_grade_signal(last_s, 0.5, 0.0" not in source
