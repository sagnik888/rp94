"""Every setting referenced in code must be declared on the Settings model.

Undeclared settings still "work" through getattr defaults, but the operator
cannot toggle or persist them and the dashboard cannot expose them — the
2026-07-03 audit found seven such gaps (trend-day guard toggles, market
location gate, 5min special floor, audit-log verbosity, Angel backoff pair).
This test makes the sweep permanent so an undeclared setting fails CI.
"""

import glob
import inspect
import re

from config.settings import Settings, get_settings
from scanner import Scanner

# getattr targets that look like settings reads but are attribute reads on
# other objects (signals) behind a variable coincidentally named `s`.
NOT_SETTINGS = {
    "bar_index",   # engine/alt_scanners.py: sort key over UTBotSignal objects
    "timestamp",   # scanner.py: signal-object timestamp read
}

SETTINGS_RECEIVER = re.compile(
    r"getattr\(\s*(?:settings|_s|_settings|get_settings\(\)|s)\s*,\s*[\"'](\w+)[\"']"
)

SCANNED_GLOBS = ["scanner.py", "engine/*.py", "intelligence/*.py", "trading/*.py", "data/*.py"]


def _referenced_setting_names():
    referenced = {}
    for pattern in SCANNED_GLOBS:
        for path in glob.glob(pattern):
            source = open(path, encoding="utf-8", errors="ignore").read()
            for name in SETTINGS_RECEIVER.findall(source):
                referenced.setdefault(name, set()).add(path)
    return referenced


def test_every_referenced_setting_is_declared():
    fields = set(Settings.model_fields.keys())
    unresolved = {
        name: sorted(paths)
        for name, paths in _referenced_setting_names().items()
        if name not in fields
        and not hasattr(Settings, name)  # methods/properties are fine
        and name not in NOT_SETTINGS
        and not name.startswith("_")
    }
    assert not unresolved, (
        "Settings referenced in code but not declared on the Settings model "
        f"(declare them in config/settings.py): {unresolved}"
    )


def test_audit_gap_settings_are_declared_with_safe_defaults():
    s = get_settings()
    # Trend-day guard (was permanently ON with no operator toggle).
    assert s.session_context_trend_day_guard_enabled is True
    assert s.session_context_trend_day_guard_min_confidence == 0.70
    # Market-location entry gate (was stuck ON, undeclarable).
    assert s.ut_market_location_gate is True
    # 5min special floor and audit-log verbosity (defaults preserved exactly).
    assert s.live_5min_special_min_confidence == 0.60
    assert s.ut_historical_verbose_audit_logs is False
    # Angel REST backoff pair (code defaults preserved exactly).
    assert s.angel_rest_backoff_seconds == 12.0
    assert s.angel_rest_backoff_max_seconds == 60.0
    # ER early-trend promotion: opt-out but enabled by default with range-day guard.
    assert s.regime_early_trend_er_promotion_enabled is True
    assert s.regime_early_trend_er_period == 10
    assert s.regime_early_trend_er_threshold == 0.60
    assert s.regime_early_trend_er_adx_ceiling == 20.0
    assert s.regime_early_trend_er_range_day_min_confidence == 0.60


def test_declared_gap_settings_are_actually_toggleable():
    s = get_settings()
    previous = s.session_context_trend_day_guard_enabled
    try:
        s.session_context_trend_day_guard_enabled = False
        assert get_settings().session_context_trend_day_guard_enabled is False
    finally:
        s.session_context_trend_day_guard_enabled = previous


def test_cache_fingerprint_reads_the_declared_strike_setting():
    # The results-cache fingerprint must react when the operator flips
    # ATM/ITM/BOTH; it previously read a nonexistent "strike_selection" key
    # and therefore never invalidated on that change.
    source = inspect.getsource(Scanner)
    assert 'getattr(settings, "strike_selection"' not in source
    assert 'getattr(settings, "option_strike_selection", "BOTH")' in source
