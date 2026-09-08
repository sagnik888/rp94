"""The canonical quality floor is no longer split by hidden clock windows."""

from config.settings import Settings
from engine.quality_verdict import QualityInputs


def test_retired_clock_specific_quality_controls_stay_removed():
    retired = {
        "ut_early_session_cutoff",
        "live_5min_early_min_confidence",
        "live_5min_pre_close_min_confidence",
    }

    assert retired.isdisjoint(Settings.model_fields)


def test_quality_inputs_have_no_time_of_day_backdoor():
    fields = set(QualityInputs.__dataclass_fields__)

    assert "timestamp" not in fields
    assert "time_of_day" not in fields
    assert "early_session" not in fields
