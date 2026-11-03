"""Range-day regime-pillar damp: phantom trend alignment on a confirmed range
day must not manufacture A+ labels (2026-07-03: six A+ range-day trades, 0/6)."""

import inspect
from types import SimpleNamespace

from config.settings import get_settings
from engine.signal_manager import SignalManager
from scanner import Scanner
from tests.source_views import scanner_runtime_source, signal_pipeline_source


def _grade(day_type="", day_conf=0.0, regime="TRENDING_UP"):
    manager = SignalManager()
    signal = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        signal_type="BUY",
        price=24000.0,
        adx_value=28.0,
        atr_value=40.0,
        stop_distance=40.0,
        timestamp=None,
    )
    return manager._grade_signal(
        signal,
        confluence_score=0.9,
        intelligence_score=10.0,  # near-zero intel, like the July-3 losers
        regime=regime,
        day_type=day_type,
        day_type_confidence=day_conf,
    )


def test_range_day_caps_phantom_trend_alignment():
    _, conf_plain, _ = _grade()
    _, conf_damped, reasons = _grade(day_type="RANGE_DAY", day_conf=0.62)
    assert conf_damped < conf_plain
    assert any("Range-day damp" in r for r in reasons)


def test_trend_day_and_low_confidence_are_untouched():
    _, conf_plain, _ = _grade()
    _, conf_trend_day, reasons_trend = _grade(day_type="TREND_DAY_UP", day_conf=0.80)
    assert conf_trend_day == conf_plain
    assert not any("Range-day damp" in r for r in reasons_trend)
    # Below the confidence floor the classification is not trusted.
    _, conf_low, reasons_low = _grade(day_type="RANGE_DAY", day_conf=0.40)
    assert conf_low == conf_plain
    assert not any("Range-day damp" in r for r in reasons_low)


def test_damp_respects_toggle():
    settings = get_settings()
    previous = getattr(settings, "range_day_regime_pillar_damp_enabled", True)
    settings.range_day_regime_pillar_damp_enabled = False
    try:
        _, conf_plain, _ = _grade()
        _, conf_off, reasons = _grade(day_type="RANGE_DAY", day_conf=0.80)
        assert conf_off == conf_plain
        assert not any("Range-day damp" in r for r in reasons)
    finally:
        settings.range_day_regime_pillar_damp_enabled = previous


def test_day_type_threaded_into_live_and_historical_grading():
    from engine.signal_processor import SignalProcessor

    live = signal_pipeline_source()
    assert '_live_session_ctx.get("effective_day_type")' in live
    assert '_live_session_ctx.get("phase_confidence")' in live
    hist_sp = inspect.getsource(SignalProcessor)
    assert "_historical_day_type_at" in hist_sp
    sc = scanner_runtime_source()
    assert "_historical_day_type_at" in sc
    assert "_historical_day_type_cache" in sc
