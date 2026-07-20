"""Weak-market scalp mode: confirmed range-day non-burst entries arm scalp-lock
and cap RR; bursts run free; trend days untouched."""

import inspect

from config.settings import get_settings
from engine.signal_processor import SignalProcessor


def test_weak_market_scalp_mode_wired_with_burst_exemptions():
    source = inspect.getsource(SignalProcessor.process_best_signal)
    assert "weak_market_scalp_mode_enabled" in source
    assert "Weak-market scalp" in source
    # Bursts must stay runners.
    block = source.split("Weak-market scalp mode")[1].split("is_explosive_bypass:")[0]
    assert "not fast_thrust_override" in block
    assert "market_wide_reversal_entry" in block
    assert "not candidate.is_explosive_bypass" in block
    # Only tighten, never widen the target.
    assert "_wm_span > _wm_risk * _wm_max_rr" in block
    # Only on CONFIRMED range days (UNDECIDED mornings stay runners).
    assert '_wm_day == "RANGE_DAY"' in block


def test_weak_market_scalp_settings_defaults():
    settings = get_settings()
    assert settings.weak_market_scalp_mode_enabled is True
    assert settings.weak_market_scalp_min_day_confidence == 0.60
    assert settings.weak_market_scalp_max_rr == 1.5
