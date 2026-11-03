"""Option entry and replay use one bounded premium-loss policy, including UTS."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from config.settings import Settings
from engine import uts_lane
from engine.signal_processor import SignalProcessor
from trading.trade_manager import TradeManager


@pytest.mark.parametrize("regime,grade,confidence,pct", [
    ("TRENDING_DOWN", "B", 0.60, 10.0),
    ("RANGING", "B", 0.60, 15.0),
    ("VOLATILE", "C", 0.28, 15.0),
])
def test_actual_option_replay_stop_matches_live_uts_guard(regime, grade, confidence, pct):
    settings = Settings()
    # Explicit legacy values must not revive the old 50% bypass.
    object.__setattr__(settings, "uts_lane_guard_max_pct", 50.0)
    start = datetime(2026, 9, 29, 10, 0)
    frame = pd.DataFrame(
        {"open": [100.0, 100.0], "high": [101.0, 101.0],
         "low": [99.0, 99.0], "close": [100.0, 100.0]},
        index=pd.date_range(start, periods=2, freq="min"),
    )
    live_risk = uts_lane.premium_stop_distance(
        100.0, 100.0, 0.5, settings,
        regime=regime, grade=grade, confidence=confidence, timeframe="15min",
    )
    historical_risk = SignalProcessor._historical_uts_premium_stop_distance(
        100.0, 100.0, 0.5, settings,
        regime=regime, grade=grade, confidence=confidence, timeframe="15min",
    )
    result = SignalProcessor.__new__(SignalProcessor)._simulate_actual_option_exit(
        frame, start, start + timedelta(minutes=2), 50.0, 3.0,
        natural_sl_distance=50.0, max_sl_pct=0.50,
        replay_regime=regime, replay_grade=grade, replay_confidence=confidence,
        live_exits=False, replay_source_lane="UTS",
    )
    assert live_risk == historical_risk == pytest.approx(pct)
    assert result is not None
    assert result["stop"] == pytest.approx(100.0 - pct)


@pytest.mark.parametrize("regime,grade,confidence,fast,pct", [
    ("TRENDING_UP", "B", 0.60, False, 10.0),
    ("CHOPPY", "B", 0.60, False, 15.0),
    ("VOLATILE", "C", 0.28, False, 5.0),
    ("VOLATILE", "A", 0.80, True, 5.0),
])
def test_broker_fill_boundary_caps_legacy_fifty_percent_stop(regime, grade, confidence, fast, pct):
    manager = TradeManager.__new__(TradeManager)
    manager.options_sl_pct = 50.0
    manager.fresh_entry_policy_guard = None
    trade = SimpleNamespace(
        inst_type="OPT", entry_price=120.0, current_stop=50.0,
        market_regime=regime, grade=grade, confidence=confidence,
        volume_spike_entry=fast, is_reversion_scalp=False, is_recovery=False,
    )
    assert manager._actual_fill_entry_policy_verdict(trade) == (True, "")
    expected = 120.0 * (1.0 - pct / 100.0)
    assert trade.current_stop == trade.initial_stop == trade.premium_stop == pytest.approx(expected)
    trade.current_stop = 119.0
    manager._actual_fill_entry_policy_verdict(trade)
    assert trade.current_stop == 119.0  # an existing tighter stop is never loosened


def test_recovered_position_stop_is_not_retroactively_changed():
    manager = TradeManager.__new__(TradeManager)
    manager.options_sl_pct = 15.0
    trade = SimpleNamespace(inst_type="OPT", is_recovery=True, current_stop=50.0)
    manager._cap_option_entry_stop(trade)
    assert trade.current_stop == 50.0


def test_fractional_premium_stop_rounding_never_exceeds_percentage_cap():
    manager = TradeManager.__new__(TradeManager)
    manager.options_sl_pct = 15.0
    trade = SimpleNamespace(
        inst_type="OPT", is_recovery=False, entry_price=162.35, current_stop=81.15,
        market_regime="TRENDING_DOWN", grade="B", confidence=0.48,
    )
    manager._cap_option_entry_stop(trade)
    assert trade.current_stop == 146.15
    assert (trade.entry_price - trade.current_stop) / trade.entry_price <= .10


def test_selected_five_percent_stop_survives_adverse_fill_without_candidate_flags():
    manager = TradeManager.__new__(TradeManager)
    manager.options_sl_pct = 15.0
    trade = SimpleNamespace(
        inst_type="OPT", is_recovery=False, entry_price=100.0, current_stop=95.0,
        market_regime="RANGING", grade="A", confidence=0.8,
    )
    manager._cap_option_entry_stop(trade)
    trade.entry_price = 120.0
    manager._cap_option_entry_stop(trade)
    assert trade.current_stop == 114.0


@pytest.mark.parametrize("regime,grade,confidence,expected", [
    ("TRENDING_DOWN", "B", 0.60, 90.0),
    ("RANGING", "B", 0.60, 85.0),
    ("VOLATILE", "C", 0.28, 85.0),
])
def test_new_trade_ledger_never_accepts_legacy_fifty_percent_stop(
    regime, grade, confidence, expected,
):
    manager = TradeManager(broker=None, max_positions=5)
    manager.mode = "HISTORICAL"
    manager.open_trades.clear()
    trade = manager.open_trade(
        instrument="NIFTY", timeframe="15min", direction="SHORT",
        price=100.0, trailing_stop=50.0, initial_stop=50.0, target=250.0,
        lots=1, lot_size=65, grade=grade, confidence=confidence,
        inst_type="OPT", option_type="PE", market_regime=regime,
        source_lane="UTS", source_engine="ut_smart", is_ghost=True,
    )
    assert trade is not None
    assert trade.current_stop == trade.trailing_stop == trade.initial_stop == expected
