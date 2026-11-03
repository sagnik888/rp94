"""Admission must use current directional evidence, never birth flags or prose."""
from types import SimpleNamespace

import pandas as pd
import pytest

from config.settings import get_settings
from scanner import Scanner


def scanner_with_position(position, mode="REAL"):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = mode
    scanner.mtf = SimpleNamespace(engines={
        "NIFTY_15min": SimpleNamespace(
            get_state=lambda _key: SimpleNamespace(position=position)
        )
    })
    scanner.log_signal_decision_once = lambda *_args: None
    return scanner


@pytest.mark.parametrize("direction,old,current,expected", [
    ("LONG", True, -1, False), ("SHORT", True, 1, False),
    ("LONG", False, 1, True), ("SHORT", False, -1, True),
])
def test_live_15m_confirmation_is_refreshed(direction, old, current, expected):
    scanner = scanner_with_position(current)
    candidate = SimpleNamespace(instrument="NIFTY", timeframe="5min",
                                direction=direction, trend_15m_agrees=old)
    assert scanner._candidate_15m_trend_agrees(candidate) is expected
    assert candidate.trend_15m_agrees is expected


def test_live_missing_15m_cannot_reuse_birth_confirmation():
    scanner = scanner_with_position(1)
    scanner.mtf.engines.clear()
    candidate = SimpleNamespace(instrument="NIFTY", timeframe="5min",
                                direction="LONG", trend_15m_agrees=True)
    assert scanner._candidate_15m_trend_agrees(candidate) is False
    assert candidate.trend_15m_agrees is False
    assert candidate.trend_15m_state == "unavailable"


def test_historical_15m_uses_causal_record_not_current_engine():
    scanner = scanner_with_position(-1, mode="HISTORICAL")
    candidate = SimpleNamespace(instrument="NIFTY", timeframe="5min",
                                direction="LONG", trend_15m_agrees=True)
    assert scanner._candidate_15m_trend_agrees(candidate) is True


def test_live_warmup_still_checks_current_admission_evidence():
    scanner = scanner_with_position(-1)
    scanner.is_warmup = True
    candidate = SimpleNamespace(instrument="NIFTY", timeframe="5min",
                                direction="LONG", trend_15m_agrees=True)
    assert scanner._candidate_15m_trend_agrees(candidate) is False


def test_final_quality_rechecks_15m_in_both_directions():
    scanner = scanner_with_position(-1)
    scanner._candidate_market_intelligence_context = lambda _candidate: {}
    candidate = SimpleNamespace(
        instrument="NIFTY", timeframe="5min", direction="LONG", inst_type="OPT",
        grade="A", confidence=0.70, adx_value=30.0, impulse_confirmed=True,
        trend_15m_agrees=True,
    )
    assert not scanner._quality_verdict_for_candidate(candidate, regime="RANGING").allowed
    scanner.mtf.engines["NIFTY_15min"].get_state = lambda _key: SimpleNamespace(position=1)
    assert scanner._quality_verdict_for_candidate(candidate, regime="RANGING").allowed


@pytest.mark.parametrize("direction,intel,reasons,allowed", [
    ("LONG", -0.8, [], False),
    ("LONG", -0.6, ["Intelligence: Institutional Volume/OI Confirmed"], False),
    ("SHORT", 0.8, [], False),
    ("SHORT", 0.6, ["Intelligence: Institutional Volume/OI Confirmed"], False),
    ("LONG", 0.6, [], True),
    ("SHORT", -0.6, [], True),
    ("LONG", 60.0, [], True),
])
def test_location_exception_uses_directional_score_not_old_prose(
    monkeypatch, direction, intel, reasons, allowed
):
    monkeypatch.setattr(get_settings(), "ut_market_location_gate", True)
    scanner = scanner_with_position(1 if direction == "LONG" else -1)
    closes = [119.0 - i for i in range(20)]
    if direction == "SHORT":
        closes = list(reversed(closes))
    frame = pd.DataFrame({
        "open": [v + 0.5 for v in closes], "close": closes,
        "high": [v + 1 for v in closes], "low": [v - 1 for v in closes],
    }, index=pd.date_range("2026-09-22 09:45", periods=20, freq="min"))
    scanner.candles = SimpleNamespace(get_candles=lambda *_args: frame)
    scanner._get_trade_regime = lambda *_args, **_kwargs: "RANGING"
    scanner._trade_regime_is_own_timeframe = lambda _candidate: True
    candidate = SimpleNamespace(
        instrument="NIFTY", timeframe="5min", direction=direction,
        inst_type="OPT", intelligence_score=intel, reasons=reasons,
        impulse_confirmed=True, adx_value=30.0, trend_15m_agrees=True,
    )
    result, reason = scanner._market_location_entry_gate(candidate)
    assert reason.startswith("Market location risk")
    assert result is allowed
