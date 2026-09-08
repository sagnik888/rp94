from dashboard.server import _trim_chart_payload, _valid_command_payload


def test_manual_close_rejects_non_finite_and_negative_prices():
    for price in ("nan", "inf", "-1"):
        assert (
            _valid_command_payload(
                {"cmd": "close_trade", "trade_id": "T0001", "price": price}
            )
            == "invalid_price"
        )


def test_manual_close_accepts_zero_for_broker_reconciled_market_exit():
    assert (
        _valid_command_payload(
            {"cmd": "close_trade", "trade_id": "T0001", "price": "0"}
        )
        is None
    )


def test_manual_close_accepts_positive_finite_price():
    assert (
        _valid_command_payload(
            {"cmd": "close_trade", "trade_id": "T0001", "price": "123.45"}
        )
        is None
    )


def test_execute_signal_requires_existing_signal_identity():
    assert (
        _valid_command_payload(
            {"cmd": "execute_signal", "signal_id": "SESSION|NIFTY|5min|ENTRY|LONG|2026-07-03T10:25:00"}
        )
        is None
    )
    assert (
        _valid_command_payload(
            {"cmd": "execute_signal", "lifecycle_id": "NIFTY|5min|LONG|2026-07-03T10:25:00"}
        )
        is None
    )
    assert _valid_command_payload({"cmd": "execute_signal"}) == "invalid_signal_id"
    assert (
        _valid_command_payload({"cmd": "execute_signal", "signal_id": "x" * 241})
        == "invalid_signal_id"
    )


def test_configure_accepts_advanced_gate_settings():
    assert (
        _valid_command_payload(
            {
                "cmd": "configure",
                "lean_routing_to_options": True,
                "price_action_module_enabled": False,
                "price_action_block_adverse": False,
                "price_action_adverse_threshold": "-0.4",
                "price_action_reversal_relief_enabled": True,
                "regime_veto_enabled": True,
                "regime_veto_adx_floor": "18",
                "regime_early_trend_er_promotion_enabled": True,
                "regime_early_trend_er_period": "10",
                "regime_early_trend_er_threshold": "0.60",
                "regime_early_trend_er_adx_ceiling": "20.0",
                "regime_early_trend_er_range_day_min_confidence": "0.60",
                "fut_early_loss_cut_enabled": True,
                "fut_loss_cut_r": "0.6",
                "power_move_override_enabled": True,
                "power_move_late_hard_cutoff": "15:15",
                "power_move_spike_bypass_15m": True,
                "power_move_spike_range_atr_mult": "2.5",
                "live_5min_trend_continuation_enabled": True,
                "live_5min_trend_continuation_adx": "28",
                "volume_spike_entry_enabled": True,
                "one_min_scalping_enabled": False,
                "volume_spike_entry_min_volume_ratio": "2.8",
                "volume_spike_entry_min_range_atr_ratio": "1.5",
                "volume_spike_entry_min_close_location": "0.65",
                "volume_spike_entry_futures_sl_pct": "0.05",
                "volume_spike_entry_options_sl_pct": "5.0",
                "volume_spike_entry_stabilization_seconds": "5.0",
                "stabilization_momentum_override_enabled": True,
                "live_signal_stabilization_seconds_5min": "30.0",
                "live_signal_stabilization_seconds_15min": "30.0",
                "live_15min_momentum_override_enabled": True,
                "live_15min_momentum_min_confidence": "0.20",
                "live_15min_momentum_volume_ratio": "2.0",
                "live_15min_momentum_range_atr_ratio": "1.25",
                "live_15min_momentum_close_location": "0.65",
                "live_15min_momentum_adx_threshold": "24.0",
                "live_15min_momentum_intel_threshold": "0.65",
                "reversal_entry_relief_enabled": True,
                "reversal_entry_min_confidence": "0.20",
                "reversal_entry_thrust_min_range_atr_ratio": "1.20",
                "reversal_entry_thrust_min_close_location": "0.62",
                "reversal_entry_thrust_min_volume_ratio": "1.20",
                "reversal_entry_vwap_min_dev_pct": "0.03",
                "directional_census_reversal_relief_enabled": True,
                "directional_census_reversal_min_confidence": "0.20",
                "late_session_choppy_entry_guard_enabled": True,
                "late_session_choppy_min_runway_minutes": "30",
                "market_wide_reversal_enabled": True,
                "market_wide_reversal_core_votes": "2",
                "market_wide_reversal_total_votes": "3",
                "market_wide_reversal_min_range_atr_ratio": "1.10",
                "market_wide_reversal_min_range_atr_ratio_5min": "1.10",
                "market_wide_reversal_min_range_atr_ratio_15min": "0.50",
                "market_wide_reversal_midcap_min_range_atr_ratio_5min": "0.65",
                "market_wide_reversal_midcap_min_range_atr_ratio_15min": "0.45",
                "market_wide_reversal_min_close_location": "0.65",
                "market_wide_reversal_max_skew_seconds_5min": "180",
                "market_wide_reversal_max_skew_seconds_15min": "900",
                "late_session_reclaim_override_enabled": True,
                "late_session_reclaim_start": "14:25",
                "late_session_reclaim_end": "15:10",
                "late_session_reclaim_core_votes": "3",
                "late_session_reclaim_total_votes": "3",
                "late_session_reclaim_reclaim_votes": "2",
                "late_session_reclaim_max_skew_seconds": "240",
                "late_session_reclaim_min_close_location": "0.55",
                "late_session_reclaim_min_volume_ratio": "1.10",
                "late_session_reclaim_volume_votes": "3",
                "late_session_reclaim_options_sl_pct": "5.0",
                "late_session_reclaim_futures_sl_pct": "0.05",
                "late_session_reclaim_rr": "1.5",
                "fifteen_min_choppy_scalp_guard_enabled": True,
                "fifteen_min_choppy_scalp_min_intel_score": "0.20",
                "fifteen_min_choppy_scalp_min_volume_ratio": "1.05",
                "fifteen_min_choppy_scalp_micro_max_skew_seconds": "240",
                "fifteen_min_choppy_scalp_min_close_location": "0.55",
                "fifteen_min_choppy_scalp_profit_lock_r_multiple": "0.20",
                "fifteen_min_choppy_scalp_min_peak_rs": "250",
                "midcap_15min_risky_stabilization_seconds": "60",
                "midcap_15min_risky_stabilization_min_scans": "3",
                "midcap_15min_risky_stabilization_min_volume_ratio": "1.05",
                "midcap_15min_risky_stabilization_min_intel_score": "0.25",
                "ut_confidence_calibration_enabled": False,
                "engine_lab_enabled": True,
                "engine_lab_real_mode_only": False,
                "execution_use_touch_quote": False,
                "execution_touch_quote_shadow_enabled": True,
                "execution_touch_quote_strict": False,
                "quality_hybrid_min_vote_score": "3.0",
                "quality_hybrid_broad_agreement": "4",
                "quality_hybrid_chop_min_agreement": "4",
                "quality_hybrid_trend_adx": "22",
                "quality_hybrid_trend_er": "0.50",
                "quality_hybrid_thrust_volume_ratio": "1.6",
                "quality_hybrid_thrust_range_atr": "1.05",
                "quality_hybrid_thrust_close_loc": "0.60",
                "ut_confidence_calibration_min_samples": "200",
                "ut_no_entry_after": "15:01",
                "ut_5min_no_entry_after": "15:14",
                "ut_force_exit_time": "15:22",
            }
        )
        is None
    )


def test_configure_rejects_invalid_advanced_setting_types():
    assert (
        _valid_command_payload(
            {"cmd": "configure", "price_action_module_enabled": "true"}
        )
        == "invalid_boolean:price_action_module_enabled"
    )
    assert (
        _valid_command_payload({"cmd": "configure", "ut_force_exit_time": "3:99"})
        == "invalid_time:ut_force_exit_time"
    )
    assert (
        _valid_command_payload(
            {"cmd": "configure", "regime_early_trend_er_promotion_enabled": "true"}
        )
        == "invalid_boolean:regime_early_trend_er_promotion_enabled"
    )
    assert (
        _valid_command_payload(
            {"cmd": "configure", "regime_early_trend_er_threshold": "nan"}
        )
        == "invalid_number:regime_early_trend_er_threshold"
    )


def test_set_scanner_engine_accepts_new_engine_command_shapes():
    for engine in ("quad_prime", "adaptive_dual"):
        assert (
            _valid_command_payload({"cmd": "set_scanner_engine", "state": engine})
            is None
        )


def test_chart_payload_trim_keeps_overlays_and_intel_in_visible_window():
    payload = {
        "candles": [{"time": i, "close": 100 + i} for i in range(10)],
        "trailing_stop": [{"time": i, "value": 90 + i} for i in range(10)],
        "markers": [{"time": i, "text": str(i)} for i in range(10)],
        "indicator_overlays": [
            {"name": "EMA 21", "data": [{"time": i, "value": 100 + i} for i in range(10)]}
        ],
        "intel_history": {
            "pcr": [{"time": i, "value": 1.0} for i in range(10)],
            "latest": {"primary_pcr": 1.0},
        },
    }

    trimmed = _trim_chart_payload(payload, 3)

    assert [row["time"] for row in trimmed["candles"]] == [7, 8, 9]
    assert [row["time"] for row in trimmed["trailing_stop"]] == [7, 8, 9]
    assert [row["time"] for row in trimmed["markers"]] == [7, 8, 9]
    assert [row["time"] for row in trimmed["indicator_overlays"][0]["data"]] == [7, 8, 9]
    assert [row["time"] for row in trimmed["intel_history"]["pcr"]] == [7, 8, 9]
    assert trimmed["intel_history"]["latest"] == {"primary_pcr": 1.0}
