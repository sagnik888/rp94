from pathlib import Path


def test_dashboard_index_does_not_inject_mock_trading_events():
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")

    forbidden = [
        "MOCK TAPE INJECTOR",
        "MOCK TOAST INJECTOR",
        "Matched LONG Signal",
        "Matched SHORT Signal",
        "LONG Signal Matched",
        "SHORT Signal Matched",
        "Extreme Volatility Detected",
    ]
    for marker in forbidden:
        assert marker not in html


def test_dashboard_recovers_immediately_and_periodically_from_omitted_ws_state():
    js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")

    assert "state.wsPayloadOmitted = true" in js
    assert "refreshDashboardSnapshot('websocket-payload-omitted', { force: true })" in js
    assert "state.wsPayloadOmitted || wsIsStale || !hasState" in js
    assert "state.wsPayloadOmitted = false" in js
    assert "app.js?v=aug2026-v43-independent-quotes" in html


def test_index_cards_render_preopen_settlement_quote_label():
    js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")

    assert "if (d?.preopen_settlement === true) return 'SETTLE';" in js
    assert "sessionLabel === 'PRE-OPEN' || sessionLabel === 'SETTLE'" in js
    assert "Pre-open settlement quote from" in js
    # Cache-bust must exist; exact version is not pinned (it bumps with every
    # app.js change and stale duplicated pins caused recurring false failures).
    assert "app.js?v=" in html


def test_settings_ui_exposes_advanced_gate_controls():
    js = Path("dashboard/static/js/dashboard-settings.js").read_text(encoding="utf-8")
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")

    for marker in [
        "settPriceActionModule",
        "settLeanRouting",
        "settRegimeVeto",
        "settRegimeErPromotion",
        "settRegimeErThreshold",
        "settFutLossCut",
        "settSpikeBypass",
        "settTrendContinuation",
        "settExecutionTouchQuote",
        "settExecutionTouchQuoteShadow",
        "settExecutionTouchQuoteStrict",
        "settStabilizationMomentumOverride",
        "settLiveStab5",
        "settLiveStab15",
        "settLive15MomentumOverride",
        "settReversalEntryRelief",
        "settDirectionalCensusRelief",
        "settLateSessionChoppyGuard",
        "settMarketWideReversal",
        "settLateReclaimEnabled",
        "settLateReclaimCoreVotes",
        "sett15mChoppyScalpEnabled",
        "sett15mChoppyScalpVolume",
        "settMidcap15mBufferSec",
        "settDualLaneEnabled",
        "settDualLaneGapBars",
        "settDualLaneMinA_R",
        "settDualLaneSizeFraction",
        "settDualLaneExposureCap",
        "settDualLaneMinBConf",
        "settDualLaneJaccard",
        "settVolumeSpikeEntry",
        "settOneMinScalping",
        "settNoEntryAfter",
        "settForceExitTime",
        "price_action_module_enabled",
        "price_action_reversal_relief_enabled",
        "regime_early_trend_er_promotion_enabled",
        "regime_early_trend_er_threshold",
        "one_min_scalping_enabled",
        "power_move_spike_bypass_15m",
        "execution_use_touch_quote",
        "execution_touch_quote_shadow_enabled",
        "execution_touch_quote_strict",
        "stabilization_momentum_override_enabled",
        "live_15min_momentum_override_enabled",
        "reversal_entry_relief_enabled",
        "directional_census_reversal_relief_enabled",
        "late_session_choppy_entry_guard_enabled",
        "market_wide_reversal_enabled",
        "late_session_reclaim_override_enabled",
        "fifteen_min_choppy_scalp_guard_enabled",
        "midcap_15min_risky_stabilization_seconds",
        "dual_lane_enabled",
        "dual_lane_same_dir_min_bars_gap",
        "dual_lane_same_dir_min_lane_a_r",
        "dual_lane_size_fraction",
        "dual_lane_max_index_exposure_rs",
        "dual_lane_min_lane_b_confidence",
        "dual_lane_jaccard_alert_threshold",
        "cascade_lot_scale_enabled",
        "heavy_trend_lot_scale_enabled",
        "cascade_multi_tf_stack_enabled",
        "cascade_midcap_solo_max_tf",
        "cascade_core_bucket_max_positions",
        "multi_signal_mixed_book_enabled",
        "dual_clone_reduction_enabled",
    ]:
        assert marker in js
    assert "dashboard-settings.js?v=" in html
    assert 'data-type="engine_lab"' in html


def test_settings_ui_saves_all_backend_advanced_gate_fields():
    js = Path("dashboard/static/js/dashboard-settings.js").read_text(encoding="utf-8")

    expected_fields = [
        "price_action_reversal_relief_enabled",
        "regime_early_trend_er_promotion_enabled",
        "regime_early_trend_er_period",
        "regime_early_trend_er_threshold",
        "regime_early_trend_er_adx_ceiling",
        "regime_early_trend_er_range_day_min_confidence",
        "stabilization_momentum_override_enabled",
        "live_signal_stabilization_seconds_5min",
        "live_signal_stabilization_seconds_15min",
        "live_15min_momentum_override_enabled",
        "live_15min_momentum_min_confidence",
        "live_15min_momentum_volume_ratio",
        "live_15min_momentum_range_atr_ratio",
        "live_15min_momentum_close_location",
        "live_15min_momentum_adx_threshold",
        "live_15min_momentum_intel_threshold",
        "reversal_entry_relief_enabled",
        "reversal_entry_min_confidence",
        "reversal_entry_thrust_min_range_atr_ratio",
        "reversal_entry_thrust_min_close_location",
        "reversal_entry_thrust_min_volume_ratio",
        "reversal_entry_vwap_min_dev_pct",
        "directional_census_reversal_relief_enabled",
        "directional_census_reversal_min_confidence",
        "late_session_choppy_entry_guard_enabled",
        "late_session_choppy_min_runway_minutes",
        "market_wide_reversal_enabled",
        "market_wide_reversal_core_votes",
        "market_wide_reversal_total_votes",
        "market_wide_reversal_min_range_atr_ratio",
        "market_wide_reversal_min_range_atr_ratio_5min",
        "market_wide_reversal_min_range_atr_ratio_15min",
        "market_wide_reversal_midcap_min_range_atr_ratio_5min",
        "market_wide_reversal_midcap_min_range_atr_ratio_15min",
        "market_wide_reversal_min_close_location",
        "market_wide_reversal_max_skew_seconds_5min",
        "market_wide_reversal_max_skew_seconds_15min",
        "late_session_reclaim_override_enabled",
        "late_session_reclaim_start",
        "late_session_reclaim_end",
        "late_session_reclaim_core_votes",
        "late_session_reclaim_total_votes",
        "late_session_reclaim_reclaim_votes",
        "late_session_reclaim_max_skew_seconds",
        "late_session_reclaim_min_close_location",
        "late_session_reclaim_min_volume_ratio",
        "late_session_reclaim_volume_votes",
        "late_session_reclaim_options_sl_pct",
        "late_session_reclaim_futures_sl_pct",
        "late_session_reclaim_rr",
        "fifteen_min_choppy_scalp_guard_enabled",
        "fifteen_min_choppy_scalp_min_intel_score",
        "fifteen_min_choppy_scalp_min_volume_ratio",
        "fifteen_min_choppy_scalp_micro_max_skew_seconds",
        "fifteen_min_choppy_scalp_min_close_location",
        "fifteen_min_choppy_scalp_profit_lock_r_multiple",
        "fifteen_min_choppy_scalp_min_peak_rs",
        "midcap_15min_risky_stabilization_seconds",
        "midcap_15min_risky_stabilization_min_scans",
        "midcap_15min_risky_stabilization_min_volume_ratio",
        "midcap_15min_risky_stabilization_min_intel_score",
        "dual_lane_enabled",
        "dual_lane_same_dir_min_bars_gap",
        "dual_lane_same_dir_min_lane_a_r",
        "dual_lane_size_fraction",
        "dual_lane_max_index_exposure_rs",
        "dual_lane_min_lane_b_confidence",
        "dual_lane_jaccard_alert_threshold",
        "dual_clone_reduction_enabled",
        "dual_clone_reduction_window_seconds",
        "cascade_lot_scale_enabled",
        "cascade_lot_multiplier_opt",
        "cascade_lot_multiplier_opt_min",
        "cascade_lot_multiplier_fut",
        "heavy_trend_lot_scale_enabled",
        "heavy_trend_lot_multiplier_opt",
        "heavy_trend_min_aligned_indices",
        "heavy_trend_vote_max_age_seconds",
        "cascade_multi_tf_stack_enabled",
        "cascade_primary_max_tf",
        "cascade_dual_addon_max",
        "cascade_midcap_solo_max_tf",
        "cascade_midcap_dual_main_max",
        "cascade_midcap_dual_addon_max",
        "cascade_core_bucket_max_positions",
        "cascade_midcap_bucket_max_positions",
        "cascade_max_concurrent_positions",
        "multi_signal_mixed_book_enabled",
        "multi_signal_max_fut",
        "multi_signal_fut_lots",
        "multi_signal_max_opt_instruments",
        "execution_touch_quote_shadow_enabled",
        "execution_touch_quote_strict",
    ]
    for field in expected_fields:
        assert field in js


def test_topbar_exposes_one_min_scalping_toggle():
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")
    app_js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")

    assert "1MSCALP" in html
    assert "P-WIN SNAP" in html
    assert "1M SPIKE ENTRIES" not in html
    assert "POST-WIN 3M SNAPBACK" not in html
    assert "oneMinScalpSwitch" in html
    assert "reversionScalpSwitch" in html
    assert "one_min_scalping_enabled" in app_js
    assert "post_exit_reversion_enabled" in app_js
    assert "1MSCALP:" in app_js
    assert "P-WIN SNAP:" in app_js
    assert "app.js?v=" in html


def test_topbar_exposes_quad_prime_engine_option():
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")
    app_js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")

    assert 'value="quad_prime"' in html
    assert "QUAD PRIME" in html
    assert "quad_prime: 'Quad Prime" in app_js
    # Default engine is Quad Prime (not UTBot+Consensus).
    assert 'value="quad_prime" selected' in html
    assert "scanner_engine || 'quad_prime'" in app_js or "scanner_engine || \"quad_prime\"" in app_js


def test_topbar_exposes_adaptive_dual_engine_option():
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")
    app_js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")

    assert 'value="adaptive_dual"' in html
    assert "ADAPTIVE DUAL" in html
    assert "adaptive_dual: 'Adaptive Dual" in app_js
    assert "12-Engine Lab" in app_js


def test_market_intel_volume_tile_uses_explicit_display_ratio():
    app_js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")

    assert "vol.display_volume_ratio" in app_js
    assert "hasTrustedRelativeVolume" in app_js
    assert "vol.relative_volume_ratio || vol.volume_ratio" not in app_js
