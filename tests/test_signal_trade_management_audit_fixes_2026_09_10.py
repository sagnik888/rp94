from datetime import datetime, timedelta
from inspect import getsource
from types import SimpleNamespace

import pytest

from engine.entry_policy import (
    ExecutionGeometry,
    ExpectedMoveForecast,
    MODEL_SOURCE,
    MODEL_VERSION,
    SUPPORTED_ENTRY_INSTRUMENTS,
    evaluate_final_entry_policy,
)
from engine.quality_verdict import QualityInputs, evaluate_quality
from engine.signal_processor import SignalProcessor
from tests.source_views import signal_pipeline_source
from intelligence.intelligence_aggregator import IntelligenceAggregator
from intelligence.regime_detector import MarketRegime, RegimeDetector
from scanner import Scanner
from scripts.evaluate_exit_policy_walk_forward import _performance_certification_gates


def test_option_delta_projection_cannot_inflate_executable_peak():
    trade = SimpleNamespace(inst_type="OPT", peak_pnl_provenance="contract_tick")

    peak, provenance = Scanner._resolve_executable_peak_pnl(
        trade,
        prior_peak_pnl=6.0,
        current_pnl_rs=-318.0,
        candle_extreme_pnl=623.1168,
        tick_extreme_pnl=0.0,
        quote_verified=True,
        has_contract_ticks=False,
    )

    assert peak == pytest.approx(6.0)
    assert provenance == "contract_tick"


def test_option_contract_ticks_can_raise_peak_while_futures_keep_contract_candles():
    option_peak, option_source = Scanner._resolve_executable_peak_pnl(
        SimpleNamespace(inst_type="OPT"),
        prior_peak_pnl=100.0,
        current_pnl_rs=250.0,
        candle_extreme_pnl=900.0,
        tick_extreme_pnl=400.0,
        quote_verified=True,
        has_contract_ticks=True,
    )
    future_peak, future_source = Scanner._resolve_executable_peak_pnl(
        SimpleNamespace(inst_type="FUT"),
        prior_peak_pnl=100.0,
        current_pnl_rs=250.0,
        candle_extreme_pnl=900.0,
        tick_extreme_pnl=400.0,
        quote_verified=True,
        has_contract_ticks=True,
    )

    assert (option_peak, option_source) == (400.0, "contract_tick")
    assert (future_peak, future_source) == (900.0, "contract_candle")


def test_breathing_or_trend_hold_cannot_be_overridden_by_retention_backstop():
    common = dict(peak_pnl=900.0, minimum_peak=250.0, ladder_peak=900.0)

    assert Scanner._smart_profit_retention_eligible(
        **common, breathing_clamped=False, trend_deferred=False
    ) is True
    assert Scanner._smart_profit_retention_eligible(
        **common, breathing_clamped=True, trend_deferred=False
    ) is False
    assert Scanner._smart_profit_retention_eligible(
        **common, breathing_clamped=False, trend_deferred=True
    ) is False


def test_consensus_counts_ema_and_supertrend_once_each():
    signal = SimpleNamespace(
        source_engine="consensus",
        source_engines=["ema", "supertrend", "consensus"],
    )

    assert Scanner._quad_union_sources(signal) == {"ema", "supertrend"}


def test_quality_verdict_labels_uncalibrated_confidence_as_heuristic():
    settings = SimpleNamespace(
        ut_confidence_calibration_enabled=True,
        ut_confidence_calibration_live_apply_enabled=False,
    )
    verdict = evaluate_quality(
        QualityInputs(
            timeframe="5min",
            inst_type="OPT",
            confidence=0.70,
            regime="TRENDING_UP",
            adx_value=30.0,
            trend_15m_agrees=True,
            source_count=2,
        ),
        settings,
    )

    assert verdict.checks["confidence_calibrated"] is False
    assert verdict.checks["confidence_semantics"] == "heuristic_score"


def test_effective_intraday_phase_overrides_stale_opening_context():
    ctx = {
        "day_type": "TREND_DAY_UP",
        "bias": "BULLISH",
        "confidence": 0.90,
        "effective_day_type": "TREND_DAY_DOWN",
        "effective_bias": "BEARISH",
        "phase_confidence": 0.80,
    }
    top_down = IntelligenceAggregator()._top_down_context_adjustment(
        session_context=ctx,
        anchor_regime={},
        breadth_context={},
    )

    assert top_down["score_adjustment"] < 0.0
    assert any("trend-down" in reason for reason in top_down["reasons"])
    assert Scanner._intraday_phase_from_session_context(ctx) == ("TREND_PHASE", 0.8)


def test_regime_promotion_uses_effective_phase_instead_of_opening_label():
    detector = RegimeDetector.__new__(RegimeDetector)
    settings = SimpleNamespace()
    common = dict(
        settings=settings,
        current_regime=MarketRegime.RANGING,
        direction="UP",
        adx_val=10.0,
        er_value=0.70,
        er_period=10,
        candle_count=40,
    )

    promoted = detector._early_trend_er_promotion(
        session_context={
            "day_type": "RANGE_DAY",
            "confidence": 0.90,
            "effective_day_type": "TREND_DAY_UP",
            "phase_confidence": 0.80,
        },
        **common,
    )
    blocked = detector._early_trend_er_promotion(
        session_context={
            "day_type": "TREND_DAY_UP",
            "confidence": 0.90,
            "effective_day_type": "RANGE_DAY",
            "phase_confidence": 0.80,
        },
        **common,
    )

    assert promoted.get("promote") is True
    assert blocked == {"blocked": "confirmed_range_day"}


def test_expected_move_statistic_is_advisory_when_geometry_is_verified():
    now = datetime(2026, 9, 10, 10, 0)
    geometry = ExecutionGeometry(
        instrument="NIFTY",
        direction="LONG",
        inst_type="OPT",
        underlying_entry=25_000.0,
        underlying_stop=24_975.0,
        underlying_target=25_050.0,
        premium_entry=100.0,
        premium_stop=96.0,
        premium_target=106.0,
        contract_entry=100.0,
        contract_stop=96.0,
        contract_target=106.0,
        observed_at=now.isoformat(),
        source="signal_processor.dual_geometry|auto_pre_submit",
        verified=True,
        conversion_ratio=0.5,
    )
    forecast = ExpectedMoveForecast(
        instrument="NIFTY",
        direction="LONG",
        timeframe="5min",
        horizon_minutes=30,
        horizon_bars=6,
        expected_favorable_move_pct=0.10,
        q10_pct=0.05,
        q25_pct=0.10,
        q50_pct=0.15,
        uncertainty_pct=0.10,
        probability_meeting_floor=0.30,
        floor_pct=0.20,
        sample_count=120,
        generated_at=now.isoformat(),
        data_as_of=(now - timedelta(seconds=30)).isoformat(),
        model_version=MODEL_VERSION,
        source=MODEL_SOURCE,
        available=True,
        reason="",
    )

    verdict = evaluate_final_entry_policy(
        geometry,
        forecast,
        min_rr=1.5,
        expected_move_floor_pct=0.20,
        max_underlying_stop_pct=0.15,
        max_forecast_age_seconds=90.0,
        max_geometry_age_seconds=10.0,
        checked_at=now,
        phase="test",
        supported_instruments=SUPPORTED_ENTRY_INSTRUMENTS,
    )

    assert verdict.allowed is True
    assert dict(verdict.checks)["expected_move_floor_advisory"] is False


def test_negative_candidate_cannot_pass_exit_certification_by_being_less_bad():
    gates = _performance_certification_gates(
        {
            "expectancy": -300.0,
            "profit_factor": 0.40,
            "max_drawdown": 40_000.0,
            "p10_trade": -2_000.0,
        },
        {
            "expectancy": -100.0,
            "profit_factor": 0.80,
            "max_drawdown": 30_000.0,
            "p10_trade": -1_500.0,
        },
    )

    assert gates["candidate_holdout_expectancy_better"] is True
    assert gates["candidate_holdout_pf_not_worse"] is True
    assert gates["candidate_holdout_expectancy_positive"] is False
    assert gates["candidate_holdout_profit_factor_above_one"] is False
    assert all(gates.values()) is False


def test_raw_collection_does_not_consume_every_competing_signal():
    source = signal_pipeline_source()
    collection_tail = source.index("self._append_adaptive_dual_apex_candidates")
    last_append = source.rfind("candidates_raw.append", 0, collection_tail)
    selection = source.index("rank_entry_candidates", collection_tail)

    assert "_remember_live_signal_identity" not in source[last_append:collection_tail]
    assert "selected_raw" in source[selection:]
    assert "_remember_live_signal_identity" in source[selection:]
