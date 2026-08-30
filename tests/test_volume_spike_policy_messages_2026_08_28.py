from types import SimpleNamespace

from engine.signal_processor import SignalProcessor


def _settings():
    return SimpleNamespace(
        one_min_scalping_high_burst_only=True,
        volume_spike_entry_min_volume_ratio=2.0,
        volume_spike_entry_strong_1min_min_volume_ratio=2.5,
        volume_spike_min_ratio_by_index={"SENSEX": 3.71},
        surge_tick_percentile_floor=97.0,
    )


def test_one_min_requirement_names_both_measurement_bases():
    text = SignalProcessor._volume_spike_requirement_text(_settings(), "SENSEX")

    assert "traded volume >= 3.71x" in text
    assert "tick activity >= p97" in text
    assert "2.5x volume" not in text


def test_tick_evidence_is_not_labelled_as_traded_volume():
    text = SignalProcessor._volume_spike_metric_text(
        {
            "volume_basis": "TICK_ACTIVITY",
            "volume_ratio": 1.18,
            "surge_percentile": 98.4,
        },
        _settings(),
        "NIFTY",
    )

    assert text == "tick activity p98.4 (floor p97)"
    assert "volume" not in text


def test_traded_evidence_reports_the_active_index_floor():
    text = SignalProcessor._volume_spike_metric_text(
        {"volume_basis": "TRADED", "volume_ratio": 4.12},
        _settings(),
        "SENSEX",
    )

    assert text == "traded volume 4.12x (floor 3.71x)"
