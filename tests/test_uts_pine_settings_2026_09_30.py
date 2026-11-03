"""The uploaded Pine profile must reach both engine and dashboard defaults."""
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from config.settings import Settings
from engine import ut_smart_engine as uts


PINE_DEFAULTS = {
    "uts_key_value": 1.0, "uts_atr_period": 10,
    "uts_key_value_5min": 1.0, "uts_atr_period_5min": 10,
    "uts_key_value_1min": 1.0, "uts_atr_period_1min": 10,
    "uts_use_heikin_ashi": False, "uts_smart_exit": True,
    "uts_arm_atr": 0.8, "uts_retrace_atr": 0.45,
    "uts_hard_retrace_atr": 1.1, "uts_min_bars": 2,
    "uts_confirmations": 2, "uts_rsi_length": 14,
    "uts_rsi_ob": 70.0, "uts_rsi_os": 30.0,
    "uts_rsi_lookback": 5, "uts_exit_ema": 5, "uts_wick_ratio": 0.35,
}


@pytest.mark.parametrize("field,expected", PINE_DEFAULTS.items())
def test_settings_and_dashboard_keep_uploaded_pine_defaults(field, expected):
    assert Settings.model_fields[field].default == expected
    text = (Path(__file__).resolve().parents[1] /
            "dashboard/static/js/dashboard-settings.js").read_text(encoding="utf-8")
    match = re.search(
        rf"\['{field}',\s*'[^']*',\s*'[^']*',\s*'(?:int|float|bool)',\s*([^,\]]+)",
        text,
    )
    assert match is not None
    raw = match.group(1).strip()
    value = {"true": True, "false": False}.get(raw) if isinstance(expected, bool) else float(raw)
    assert value == expected


@pytest.mark.parametrize("timeframe", ["1min", "5min", "15min"])
def test_every_timeframe_starts_with_the_uploaded_pine_profile(timeframe):
    defaults = SimpleNamespace(**{
        name: field.default for name, field in Settings.model_fields.items()
        if name.startswith("uts_")
    })
    assert uts.params_from_settings(defaults, timeframe) == uts.UTSParams()


def test_pine_profile_preserves_user_option_risk_rules():
    defaults = SimpleNamespace(**{
        name: field.default for name, field in Settings.model_fields.items()
        if name.startswith("uts_")
    })
    from engine import uts_lane

    # User requested buffered intrabar entries in addition to the uploaded
    # confirmed profile. Indicator formulas and option risk rules stay identical.
    assert defaults.uts_lane_allow_intrabar_entries is True
    assert defaults.uts_lane_guard_max_pct == 15.0
    special = SimpleNamespace(uts_multi_index_alignment=True, timeframe="15min")
    assert uts_lane.premium_cap_pct(special, defaults, operator_cap=15.0) == 5.0
    regular = SimpleNamespace(timeframe="5min")
    assert uts_lane.premium_cap_pct(regular, defaults, operator_cap=15.0) == 10.0
    choppy = SimpleNamespace(timeframe="15min")
    assert uts_lane.premium_cap_pct(choppy, defaults, operator_cap=15.0, regime="RANGING") == 15.0
