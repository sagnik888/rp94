"""Retired grade-bypass controls must stay absent from the canonical policy.

The original experiment exposed rank/C/late-ADX switches from a small, selected and
mixed-accounting outcome sample. Those switches duplicated admission logic and are now
removed. Legacy .env keys are ignored; they cannot silently change live decisions.
"""

import inspect

import pytest

from config.settings import Settings
from engine.quality_verdict import QualityInputs, evaluate_quality
from engine.signal_processor import SignalProcessor


RETIRED_CONTROLS = (
    "grade_gate_min_rank_override",
    "grade_gate_allow_c_enabled",
    "late_trend_entry_guard_enabled",
    "late_trend_entry_adx_ceiling",
    "live_b_grade_min_confidence",
)


@pytest.mark.parametrize("name", RETIRED_CONTROLS)
def test_retired_control_is_not_a_setting(name):
    assert name not in Settings.model_fields


@pytest.mark.parametrize("name", RETIRED_CONTROLS)
def test_retired_control_is_not_consumed_by_signal_processor(name):
    assert name not in inspect.getsource(SignalProcessor)


@pytest.mark.parametrize("name", RETIRED_CONTROLS[:-1])
def test_retired_control_is_not_exposed_or_forwarded_by_dashboard(name):
    import dashboard.server as server

    source = inspect.getsource(server)
    assert name not in server._ADVANCED_CONFIG_FIELDS
    assert name not in server._ADVANCED_BOOLEAN_CONFIG_FIELDS
    assert f'{name}=msg.get("{name}")' not in source


def test_legacy_extra_keys_are_ignored_and_cannot_lower_admission():
    settings = Settings(
        _env_file=None,
        grade_gate_min_rank_override=0,
        grade_gate_allow_c_enabled=True,
        late_trend_entry_guard_enabled=False,
        live_b_grade_min_confidence=0.40,
    )
    verdict = evaluate_quality(
        QualityInputs(
            timeframe="15min",
            inst_type="FUT",
            confidence=0.54,
            grade="B",
            regime="TRENDING_UP",
            adx_value=40.0,
            trend_15m_agrees=True,
            impulse_confirmed=True,
            volume_ratio=2.0,
            source_count=3,
            power_move=True,
        ),
        settings,
    )
    assert verdict.allowed is False
    assert verdict.reason_code == "BELOW_BPLUS_ELIGIBILITY"


ACTIVE_CONTROLS = (
    "entry_preopen_signal_guard_enabled",
    "entry_mark_touch_validation_enabled",
    "entry_mark_max_ask_multiple",
    "entry_mark_min_bid_multiple",
    "profit_lock_min_peak_rs",
    "profit_lock_nonscalp_min_peak_rs",
    "profit_lock_min_r_multiple",
    "pnl_path_logging_enabled",
    "pnl_path_min_interval_seconds",
    "pnl_path_min_move_rs",
    "pnl_path_max_samples",
    "slippage_calibration_max_age_days",
)
BOOLEAN_CONTROLS = (
    "entry_preopen_signal_guard_enabled",
    "entry_mark_touch_validation_enabled",
    "pnl_path_logging_enabled",
)


@pytest.fixture(scope="module")
def server_src():
    import dashboard.server as server

    return inspect.getsource(server)


@pytest.mark.parametrize("name", ACTIVE_CONTROLS)
def test_active_control_is_whitelisted(name):
    import dashboard.server as server

    assert name in server._ADVANCED_CONFIG_FIELDS


@pytest.mark.parametrize("name", ACTIVE_CONTROLS)
def test_active_control_is_forwarded(name, server_src):
    assert f'{name}=msg.get("{name}")' in server_src


@pytest.mark.parametrize("name", BOOLEAN_CONTROLS)
def test_active_boolean_control_is_coerced(name):
    import dashboard.server as server

    assert name in server._ADVANCED_BOOLEAN_CONFIG_FIELDS


@pytest.mark.parametrize(
    "name", [name for name in ACTIVE_CONTROLS if name not in BOOLEAN_CONTROLS]
)
def test_active_numeric_control_is_not_coerced_to_bool(name):
    import dashboard.server as server

    assert name not in server._ADVANCED_BOOLEAN_CONFIG_FIELDS


@pytest.mark.parametrize("name", ACTIVE_CONTROLS)
def test_active_setting_exists(name):
    assert name in Settings.model_fields
