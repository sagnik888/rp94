"""Configured option-premium risk is never widened by an adverse regime."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.signal_processor import SignalProcessor
from scanner import Scanner, TradeCandidate


@pytest.mark.parametrize("regime", ["RANGING", "VOLATILE", "UNKNOWN"])
def test_signal_option_stop_honours_lower_configured_cap_in_risky_regimes(regime):
    assert SignalProcessor._option_premium_stop_distance(
        100.0, SimpleNamespace(options_sl_pct=10.0), regime
    ) == pytest.approx(10.0)


@pytest.mark.parametrize("regime", ["RANGING", "VOLATILE", "UNKNOWN"])
def test_execution_option_stop_honours_lower_configured_cap_in_risky_regimes(regime):
    scanner = Scanner.__new__(Scanner)
    scanner.expiry = SimpleNamespace(in_gamma_window=lambda *_: False)
    scanner._session_squareoff_clock = lambda: None
    scanner.options_sl_pct = 10.0
    candidate = TradeCandidate(
        instrument="NIFTY", direction="LONG", price=100.0, stop=90.0,
        target=120.0, lots=1, lot_size=65, grade="A", confidence=0.8,
        timeframe="5min", inst_type="OPT", option_type="CE", atm_strike=24500.0,
        multiplier=0.5, trading_symbol="NIFTYTESTCE", symbol_token="123",
    )
    candidate.market_regime = regime
    assert Scanner._option_premium_stop_distance_for_candidate(
        scanner, 100.0, candidate
    ) == pytest.approx(10.0)
