from config.settings import get_settings
from engine.signal_processor import SignalProcessor
from engine.signal_quality_policy import (
    dynamic_rr,
    five_min_true_impulse_allowed,
    grade_rank,
    option_grade_allowed,
    option_index_stop_distance,
    option_premium_health_ok,
    passes_quality_gate,
)


def test_grade_rank_and_dynamic_rr_bounds(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "dynamic_rr_min", 1.8)
    monkeypatch.setattr(settings, "dynamic_rr_max", 3.0)

    assert [grade_rank(value) for value in ("C", "B", "B+", "A", "A+", "Recovered")] == [0, 1, 2, 3, 4, 4]
    assert dynamic_rr("C", 0.0, -10.0) == 1.8
    assert dynamic_rr("A+", 1.0, 10.0) == 3.0


def test_dynamic_rr_short_timeframe_cap(monkeypatch):
    """5min/1min entries cap the RR ladder; 15min keeps the full runway."""
    settings = get_settings()
    monkeypatch.setattr(settings, "dynamic_rr_min", 1.6)
    monkeypatch.setattr(settings, "dynamic_rr_max", 12.0)
    monkeypatch.setattr(settings, "dynamic_rr_5min_max", 2.5)

    # Unspecified/15min timeframe keeps the quality-scaled ladder (well above the cap).
    full = dynamic_rr("A+", 0.88, 0.0)
    assert full > 2.5
    assert dynamic_rr("A+", 0.88, 0.0, timeframe="15min") == full

    # 5min and 1min are capped to dynamic_rr_5min_max.
    assert dynamic_rr("A+", 0.88, 0.0, timeframe="5min") == 2.5
    assert dynamic_rr("A+", 0.88, 0.0, timeframe="1min") == 2.5

    # The cap never pushes RR below the floor.
    monkeypatch.setattr(settings, "dynamic_rr_5min_max", 0.5)
    assert dynamic_rr("C", 0.0, 0.0, timeframe="5min") == 1.6

    # A low-RR 5min setup below the cap is unaffected by it.
    monkeypatch.setattr(settings, "dynamic_rr_5min_max", 2.5)
    assert dynamic_rr("B", 0.40, 0.0, timeframe="5min") == dynamic_rr("B", 0.40, 0.0)


def test_option_stop_distance_respects_natural_and_percentage_modes(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "sl_mode", "NATURAL")
    assert option_index_stop_distance(20.0, 100.0, 15.0, 0.5, settings) == 20.0

    monkeypatch.setattr(settings, "sl_mode", "PERCENTAGE")
    assert option_index_stop_distance(40.0, 100.0, 15.0, 0.5, settings) == 30.0


def test_signal_processor_quality_wrapper_matches_policy(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")
    monkeypatch.setattr(settings, "ut_regime_adaptation", "False")
    monkeypatch.setattr(settings, "ut_backtest_more_results", True)

    class DummyScanner:
        mode = "HISTORICAL"

    processor = SignalProcessor.__new__(SignalProcessor)
    processor.scanner = DummyScanner()
    args = ("5min", "B", 0.55, "CHOPPY", "OPT", settings)

    assert processor._passes_quality_gate(*args, adx_value=30.0) == passes_quality_gate(
        *args,
        mode="HISTORICAL",
        adx_value=30.0,
    )


def test_adx_alone_does_not_bypass_risky_five_minute_gate(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")
    monkeypatch.setattr(settings, "ut_regime_adaptation", True)
    monkeypatch.setattr(settings, "ut_backtest_more_results", False)

    assert not passes_quality_gate(
        "5min",
        "B+",
        0.55,
        "VOLATILE",
        "OPT",
        settings,
        mode="REAL",
        adx_value=40.0,
        trend_15m_agrees=False,
        intel_score=0.0,
    )


def test_adx_alignment_and_intelligence_do_not_invent_impulse_evidence(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")
    monkeypatch.setattr(settings, "ut_regime_adaptation", True)
    monkeypatch.setattr(settings, "ut_backtest_more_results", False)
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)

    common = dict(
        tf="5min",
        grade="A",
        confidence=0.70,
        regime="VOLATILE",
        inst_type="OPT",
        settings=settings,
        mode="REAL",
        adx_value=40.0,
        trend_15m_agrees=True,
        intel_score=0.8,
    )
    assert not passes_quality_gate(**common)
    assert passes_quality_gate(**common, impulse_confirmed=True)


def test_five_min_true_impulse_math_is_exact(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)
    monkeypatch.setattr(settings, "live_5min_bplus_min_confidence", 0.70)

    assert five_min_true_impulse_allowed(
        "A",
        0.70,
        settings,
        trend_15m_agrees=True,
        impulse_confirmed=True,
    )
    assert not five_min_true_impulse_allowed(
        "B+",
        0.69,
        settings,
        trend_15m_agrees=True,
        impulse_confirmed=True,
    )
    assert not five_min_true_impulse_allowed(
        "B",
        0.90,
        settings,
        trend_15m_agrees=True,
        impulse_confirmed=True,
    )
    assert not five_min_true_impulse_allowed(
        "A+",
        0.90,
        settings,
        trend_15m_agrees=False,
        impulse_confirmed=True,
    )


def test_five_min_special_bplus_sixty_exception_requires_all_guards(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)
    monkeypatch.setattr(settings, "live_5min_special_min_confidence", 0.60)
    monkeypatch.setattr(settings, "live_5min_special_adx_threshold", 30.0)
    monkeypatch.setattr(settings, "live_5min_special_intel_threshold", 0.75)
    monkeypatch.setattr(settings, "live_5min_special_max_iv_percentile", 70.0)
    monkeypatch.setattr(settings, "live_5min_special_min_chain_score", 50.0)

    assert five_min_true_impulse_allowed(
        "B+",
        0.60,
        settings,
        trend_15m_agrees=True,
        impulse_confirmed=False,
        explosive_bypass=False,
        regime="VOLATILE",
        adx_value=30.0,
        intel_score=0.30,
        iv_percentile=65.0,
        chain_quality={"score": 60, "stale": False},
    )
    assert not five_min_true_impulse_allowed(
        "B+",
        0.60,
        settings,
        trend_15m_agrees=True,
        regime="UNKNOWN",
        adx_value=30.0,
        iv_percentile=65.0,
        chain_quality={"score": 60, "stale": False},
    )
    assert not five_min_true_impulse_allowed(
        "B+",
        0.60,
        settings,
        trend_15m_agrees=True,
        regime="VOLATILE",
        adx_value=30.0,
        iv_percentile=75.0,
        chain_quality={"score": 60, "stale": False},
    )


def test_quality_gate_uses_canonical_bplus_floor_and_setup_evidence(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_regime_adaptation", True)
    monkeypatch.setattr(settings, "ut_backtest_more_results", False)
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)
    monkeypatch.setattr(settings, "live_15min_min_confidence", 0.62)

    assert passes_quality_gate(
        "15min",
        "B+",
        0.55,
        "RANGING",
        "OPT",
        settings,
        mode="REAL",
        adx_value=25.0,
        trend_15m_agrees=True,
        volume_ratio=1.30,
    )
    assert not passes_quality_gate(
        "15min",
        "B",
        0.54,
        "RANGING",
        "OPT",
        settings,
        mode="REAL",
        adx_value=25.0,
        trend_15m_agrees=True,
        volume_ratio=1.30,
    )
    assert not passes_quality_gate(
        "15min",
        "C",
        0.90,
        "TRENDING_UP",
        "FUT",
        settings,
        mode="REAL",
    )


def test_option_grade_allows_15min_b_choppy_floor(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)
    monkeypatch.setattr(settings, "live_15min_min_confidence", 0.62)

    assert option_grade_allowed(
        "B+",
        0.62,
        "VOLATILE",
        mode="REAL",
        timeframe="15min",
    )
    assert option_grade_allowed(
        "B+",
        0.62,
        "TRENDING_UP",
        mode="REAL",
        timeframe="15min",
    )
    assert not option_grade_allowed(
        "B+",
        0.61,
        "VOLATILE",
        mode="REAL",
        timeframe="15min",
    )
    assert not five_min_true_impulse_allowed(
        "B+",
        0.60,
        settings,
        trend_15m_agrees=False,
        regime="VOLATILE",
        adx_value=30.0,
        iv_percentile=65.0,
        chain_quality={"score": 60, "stale": False},
    )


def test_15min_uses_the_same_bplus_evidence_policy_as_live(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_regime_adaptation", True)
    monkeypatch.setattr(settings, "ut_backtest_more_results", False)
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)
    monkeypatch.setattr(settings, "live_15min_min_confidence", 0.62)
    monkeypatch.setattr(settings, "live_15min_trending_momentum_min_confidence", 0.58)
    monkeypatch.setattr(settings, "live_15min_trending_momentum_adx_threshold", 24.0)
    monkeypatch.setattr(settings, "live_15min_trending_momentum_intel_threshold", 0.60)

    assert passes_quality_gate(
        "15min",
        "B+",
        0.58,
        "TRENDING_DOWN",
        "OPT",
        settings,
        mode="REAL",
        adx_value=25.0,
        trend_15m_agrees=True,
        intel_score=0.70,
        impulse_confirmed=True,
    )
    assert passes_quality_gate(
        "15min",
        "B+",
        0.58,
        "RANGING",
        "OPT",
        settings,
        mode="REAL",
        adx_value=25.0,
        trend_15m_agrees=True,
        volume_ratio=1.3,
    )
    assert not passes_quality_gate(
        "15min",
        "B+",
        0.58,
        "RANGING",
        "OPT",
        settings,
        mode="REAL",
        adx_value=25.0,
        intel_score=-0.60,
    )
    assert not passes_quality_gate(
        "15min",
        "B+",
        0.58,
        "RANGING",
        "OPT",
        settings,
        mode="REAL",
        adx_value=18.0,
        intel_score=0.20,
    )


def test_option_grade_allows_15min_bplus_trending_momentum_floor(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)
    monkeypatch.setattr(settings, "live_15min_min_confidence", 0.62)
    monkeypatch.setattr(settings, "live_15min_trending_momentum_min_confidence", 0.58)

    assert option_grade_allowed(
        "B+",
        0.58,
        "BREAKOUT_UP",
        mode="REAL",
        timeframe="15min",
    )
    assert not option_grade_allowed(
        "B+",
        0.57,
        "BREAKOUT_UP",
        mode="REAL",
        timeframe="15min",
    )


def test_common_quality_floor_cannot_be_bypassed_by_low_grade_momentum(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)
    monkeypatch.setattr(settings, "live_15min_momentum_override_enabled", True)
    monkeypatch.setattr(settings, "live_15min_momentum_min_confidence", 0.20)
    monkeypatch.setattr(settings, "live_15min_momentum_volume_ratio", 2.0)
    monkeypatch.setattr(settings, "live_15min_momentum_range_atr_ratio", 1.25)
    monkeypatch.setattr(settings, "live_15min_momentum_close_location", 0.65)
    monkeypatch.setattr(settings, "live_15min_momentum_adx_threshold", 24.0)

    base = dict(
        mode="REAL",
        adx_value=28.0,
        trend_15m_agrees=True,
        volume_ratio=2.4,
        range_atr_ratio=1.6,
        close_location=0.82,
    )

    assert not passes_quality_gate(
        "15min",
        "C",
        0.22,
        "RANGING",
        "OPT",
        settings,
        **base,
    )
    assert not option_grade_allowed(
        "B",
        0.44,
        "RANGING",
        timeframe="15min",
        **base,
    )


def test_15min_low_grade_momentum_override_still_requires_thrust(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)
    monkeypatch.setattr(settings, "live_15min_momentum_override_enabled", True)

    assert not passes_quality_gate(
        "15min",
        "C",
        0.22,
        "RANGING",
        "OPT",
        settings,
        mode="REAL",
        adx_value=28.0,
        trend_15m_agrees=True,
        volume_ratio=1.2,
        range_atr_ratio=1.6,
        close_location=0.82,
    )
    assert not passes_quality_gate(
        "15min",
        "B",
        0.44,
        "RANGING",
        "OPT",
        settings,
        mode="REAL",
        adx_value=18.0,
        trend_15m_agrees=True,
        volume_ratio=2.4,
        range_atr_ratio=1.6,
        close_location=0.82,
    )
    assert not passes_quality_gate(
        "15min",
        "C",
        0.19,
        "RANGING",
        "OPT",
        settings,
        mode="REAL",
        adx_value=28.0,
        trend_15m_agrees=True,
        volume_ratio=2.4,
        range_atr_ratio=1.6,
        close_location=0.82,
    )


def test_option_premium_health_rejects_expensive_or_stale_context(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_5min_special_max_iv_percentile", 70.0)
    monkeypatch.setattr(settings, "live_5min_special_min_chain_score", 50.0)

    assert option_premium_health_ok(
        settings,
        iv_percentile=65.0,
        chain_quality={"score": 50, "stale": False},
    )
    assert not option_premium_health_ok(
        settings,
        iv_percentile=71.0,
        chain_quality={"score": 80, "stale": False},
    )
    assert not option_premium_health_ok(
        settings,
        iv_percentile=60.0,
        chain_quality={"score": 80, "stale": True},
    )
    assert not option_premium_health_ok(
        settings,
        iv_percentile=None,
        chain_quality={"score": 80, "stale": False},
    )


def test_choppy_option_grade_gate_honors_true_five_min_impulse(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_backtest_more_results", False)
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)

    # Use a self-consistent B+ confidence (0.62). 0.70 is A-grade confidence, so
    # "B+ @ 0.70" is contradictory under the single-source model (grade == bucket(conf)).
    assert not option_grade_allowed(
        "A",
        0.62,
        "VOLATILE",
        mode="REAL",
        five_min_true_impulse=False,
    )
    assert option_grade_allowed(
        "B+",
        0.62,
        "VOLATILE",
        mode="REAL",
        five_min_true_impulse=True,
    )


def test_quality_gate_blocks_bplus_five_min_without_full_impulse(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")
    monkeypatch.setattr(settings, "ut_regime_adaptation", True)
    monkeypatch.setattr(settings, "ut_backtest_more_results", False)
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)

    assert not passes_quality_gate(
        "5min",
        "A",
        0.70,
        "VOLATILE",
        "OPT",
        settings,
        mode="REAL",
        adx_value=40.0,
        trend_15m_agrees=False,
        intel_score=0.8,
    )
    assert passes_quality_gate(
        "5min",
        "B+",
        0.69,
        "VOLATILE",
        "OPT",
        settings,
        mode="REAL",
        adx_value=40.0,
        trend_15m_agrees=True,
        intel_score=0.8,
        impulse_confirmed=True,
    )


def test_quality_gate_allows_bplus_sixty_with_trend_and_volume_confirmation(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")
    monkeypatch.setattr(settings, "ut_regime_adaptation", True)
    monkeypatch.setattr(settings, "ut_backtest_more_results", False)
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)

    assert passes_quality_gate(
        "5min",
        "B+",
        0.60,
        "VOLATILE",
        "OPT",
        settings,
        mode="REAL",
        adx_value=30.0,
        trend_15m_agrees=True,
        intel_score=0.2,
        iv_percentile=65.0,
        chain_quality={"score": 60, "stale": False},
        volume_ratio=1.3,
    )
