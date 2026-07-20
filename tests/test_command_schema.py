"""Strict WebSocket command-schema validation (external audit P1)."""

import pytest

import dashboard.server as server
from dashboard.server import _valid_command_payload as v


def test_valid_commands_pass():
    assert v({"cmd": "get_state"}) is None
    assert v({"cmd": "get_state", "request_id": "r1"}) is None
    assert v({"cmd": "set_power", "state": "ON"}) is None
    assert v({
        "cmd": "set_mode",
        "mode": "REAL",
        "confirm_real_mode": True,
        "real_mode_verification": "YES",
    }) is None
    assert v({"cmd": "close_trade", "trade_id": "T1", "price": 101.5}) is None
    assert v({"cmd": "subscribe_chart", "instrument": "NIFTY", "tf": "5min"}) is None
    # A full configure payload (every documented field) must still validate.
    assert v({
        "cmd": "configure", "request_id": "x",
        "capital_total": 1100000, "capital_fut": 600000, "risk_fut_pct": 1.5,
        "capital_opt": 500000, "risk_opt_pct": 12, "lots": {"NIFTY": 1},
        "lots_fut": {"NIFTY": 1}, "futures_sl_pct": 0.15, "options_sl_pct": 15,
        "fut_cost": 200, "opt_cost": 80, "backtest_days": 5, "auto_mode": True,
        "inst_pref": "AUTO", "strike_selection": "BOTH", "grade_preference": "A",
        "ut_preset": "AGGRESSIVE", "active_indices": ["NIFTY"], "mode": "HISTORICAL",
        "reset": False, "confirm_real_mode": False, "real_mode_verification": "",
        "timeframe_entry_policy": "INCLUDE_5MIN", "max_trades_per_index": 5,
        "max_consecutive_losses": 3, "index_cooldown_minutes": 4,
        "futures_shadow_enabled": False,
        "signal_fire_mode": "CANDLE_CLOSE",
        "real_mode_premarket_arm_start": "09:00:00",
        "real_mode_premarket_settle_end": "09:08:00",
        "real_mode_premarket_refresh_end": "09:14:30",
        "real_mode_live_start": "09:15:00",
        "auto_real_mode_premarket_switch_time": "09:00:00",
    }) is None


def test_unknown_command_rejected():
    assert v({"cmd": "rm_rf"}) == "unknown_command"
    assert v({"cmd": ""}) == "unknown_command"


def test_unknown_field_rejected():
    assert v({"cmd": "get_state", "evil": 1}) == "unknown_field:evil"
    assert v({"cmd": "set_power", "state": "ON", "extra": 2}) == "unknown_field:extra"


def test_numeric_fields_validated():
    assert v({"cmd": "configure", "capital_fut": "DROP TABLE"}) == "invalid_number:capital_fut"
    assert v({"cmd": "configure", "risk_fut_pct": float("nan")}) == "invalid_number:risk_fut_pct"
    assert v({"cmd": "configure", "capital_opt": -5}) == "invalid_number:capital_opt"


def test_boolean_fields_validated():
    assert v({"cmd": "configure", "futures_shadow_enabled": "false"}) == "invalid_boolean:futures_shadow_enabled"
    assert v({"cmd": "configure", "auto_mode": "false"}) == "invalid_boolean:auto_mode"
    assert v({"cmd": "configure", "confirm_real_mode": "true"}) == "invalid_boolean:confirm_real_mode"
    assert v({"cmd": "configure", "reset": "false"}) == "invalid_boolean:reset"
    assert v({"cmd": "set_mode", "confirm_real_mode": "true", "mode": "REAL"}) == "invalid_boolean:confirm_real_mode"
    assert v({"cmd": "set_chart_stream", "enabled": "false"}) == "invalid_boolean:enabled"
    assert (
        v({"cmd": "configure", "execution_touch_quote_shadow_enabled": "true"})
        == "invalid_boolean:execution_touch_quote_shadow_enabled"
    )


def test_signal_fire_mode_validated():
    assert v({"cmd": "configure", "signal_fire_mode": "INTRACANDLE"}) is None
    assert v({"cmd": "configure", "signal_fire_mode": "CANDLE_CLOSE"}) is None
    assert v({"cmd": "configure", "signal_fire_mode": "sometimes"}) == "invalid_signal_fire_mode"


@pytest.mark.asyncio
async def test_signal_fire_mode_is_forwarded_to_scanner(monkeypatch):
    captured = {}

    class DummyScanner:
        def configure(self, **kwargs):
            captured.update(kwargs)
            return True

    class DummyWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload):
            self.messages.append(payload)

    ws = DummyWebSocket()
    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    await server.handle_command(
        ws,
        {
            "cmd": "configure",
            "request_id": "signal-mode-test",
            "signal_fire_mode": "CANDLE_CLOSE",
        },
    )

    assert captured["signal_fire_mode"] == "CANDLE_CLOSE"
    assert ws.messages[-1]["type"] == "config_ack"


@pytest.mark.asyncio
async def test_engine_lab_settings_are_forwarded_to_scanner(monkeypatch):
    captured = {}

    class DummyScanner:
        def configure(self, **kwargs):
            captured.update(kwargs)
            return True

    class DummyWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload):
            self.messages.append(payload)

    ws = DummyWebSocket()
    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    await server.handle_command(
        ws,
        {
            "cmd": "configure",
            "request_id": "engine-lab-settings",
            "engine_lab_enabled": True,
            "engine_lab_real_mode_only": False,
        },
    )

    assert captured["engine_lab_enabled"] is True
    assert captured["engine_lab_real_mode_only"] is False
    assert ws.messages[-1]["type"] == "config_ack"


@pytest.mark.asyncio
async def test_touch_quote_execution_settings_are_forwarded_to_scanner(monkeypatch):
    captured = {}

    class DummyScanner:
        def configure(self, **kwargs):
            captured.update(kwargs)
            return True

    class DummyWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload):
            self.messages.append(payload)

    ws = DummyWebSocket()
    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    await server.handle_command(
        ws,
        {
            "cmd": "configure",
            "request_id": "touch-settings",
            "execution_use_touch_quote": True,
            "execution_touch_quote_shadow_enabled": False,
            "execution_touch_quote_strict": True,
        },
    )

    assert captured["execution_use_touch_quote"] is True
    assert captured["execution_touch_quote_shadow_enabled"] is False
    assert captured["execution_touch_quote_strict"] is True
    assert ws.messages[-1]["type"] == "config_ack"


@pytest.mark.asyncio
async def test_late_session_reclaim_settings_are_forwarded_to_scanner(monkeypatch):
    captured = {}

    class DummyScanner:
        def configure(self, **kwargs):
            captured.update(kwargs)
            return True

    class DummyWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload):
            self.messages.append(payload)

    ws = DummyWebSocket()
    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    await server.handle_command(
        ws,
        {
            "cmd": "configure",
            "request_id": "late-reclaim-settings",
            "late_session_reclaim_override_enabled": True,
            "late_session_reclaim_start": "14:25",
            "late_session_reclaim_end": "15:10",
            "late_session_reclaim_core_votes": 3,
            "late_session_reclaim_total_votes": 3,
            "late_session_reclaim_reclaim_votes": 2,
            "late_session_reclaim_max_skew_seconds": 240.0,
            "late_session_reclaim_min_close_location": 0.55,
            "late_session_reclaim_min_volume_ratio": 1.10,
            "late_session_reclaim_volume_votes": 3,
            "late_session_reclaim_options_sl_pct": 5.0,
            "late_session_reclaim_futures_sl_pct": 0.05,
            "late_session_reclaim_rr": 1.5,
        },
    )

    assert captured["late_session_reclaim_override_enabled"] is True
    assert captured["late_session_reclaim_start"] == "14:25"
    assert captured["late_session_reclaim_end"] == "15:10"
    assert captured["late_session_reclaim_core_votes"] == 3
    assert captured["late_session_reclaim_min_volume_ratio"] == 1.10
    assert captured["late_session_reclaim_volume_votes"] == 3
    assert ws.messages[-1]["type"] == "config_ack"


@pytest.mark.asyncio
async def test_quad_prime_choppy_scalp_settings_are_forwarded_to_scanner(monkeypatch):
    captured = {}

    class DummyScanner:
        def configure(self, **kwargs):
            captured.update(kwargs)
            return True

    class DummyWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload):
            self.messages.append(payload)

    ws = DummyWebSocket()
    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    await server.handle_command(
        ws,
        {
            "cmd": "configure",
            "request_id": "quad-prime-choppy-scalp-settings",
            "fifteen_min_choppy_scalp_guard_enabled": True,
            "fifteen_min_choppy_scalp_min_intel_score": 0.20,
            "fifteen_min_choppy_scalp_min_volume_ratio": 1.05,
            "fifteen_min_choppy_scalp_micro_max_skew_seconds": 240.0,
            "fifteen_min_choppy_scalp_min_close_location": 0.55,
            "fifteen_min_choppy_scalp_profit_lock_r_multiple": 0.20,
            "fifteen_min_choppy_scalp_min_peak_rs": 250.0,
            "midcap_15min_risky_stabilization_seconds": 60.0,
            "midcap_15min_risky_stabilization_min_scans": 3,
            "midcap_15min_risky_stabilization_min_volume_ratio": 1.05,
            "midcap_15min_risky_stabilization_min_intel_score": 0.25,
        },
    )

    assert captured["fifteen_min_choppy_scalp_guard_enabled"] is True
    assert captured["fifteen_min_choppy_scalp_min_volume_ratio"] == 1.05
    assert captured["midcap_15min_risky_stabilization_seconds"] == 60.0
    assert captured["midcap_15min_risky_stabilization_min_scans"] == 3
    assert ws.messages[-1]["type"] == "config_ack"


@pytest.mark.asyncio
async def test_er_promotion_settings_are_forwarded_to_scanner(monkeypatch):
    captured = {}

    class DummyScanner:
        def configure(self, **kwargs):
            captured.update(kwargs)
            return True

    class DummyWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload):
            self.messages.append(payload)

    ws = DummyWebSocket()
    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    await server.handle_command(
        ws,
        {
            "cmd": "configure",
            "request_id": "er-promotion-settings",
            "regime_early_trend_er_promotion_enabled": True,
            "regime_early_trend_er_period": 10,
            "regime_early_trend_er_threshold": 0.60,
            "regime_early_trend_er_adx_ceiling": 20.0,
            "regime_early_trend_er_range_day_min_confidence": 0.60,
        },
    )

    assert captured["regime_early_trend_er_promotion_enabled"] is True
    assert captured["regime_early_trend_er_period"] == 10
    assert captured["regime_early_trend_er_threshold"] == 0.60
    assert captured["regime_early_trend_er_adx_ceiling"] == 20.0
    assert captured["regime_early_trend_er_range_day_min_confidence"] == 0.60
    assert ws.messages[-1]["type"] == "config_ack"


@pytest.mark.asyncio
async def test_quality_hybrid_settings_are_forwarded_to_scanner(monkeypatch):
    captured = {}

    class DummyScanner:
        def configure(self, **kwargs):
            captured.update(kwargs)
            return True

    class DummyWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload):
            self.messages.append(payload)

    ws = DummyWebSocket()
    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    await server.handle_command(
        ws,
        {
            "cmd": "configure",
            "request_id": "quality-hybrid-settings",
            "quality_hybrid_min_vote_score": 3.0,
            "quality_hybrid_broad_agreement": 4,
            "quality_hybrid_chop_min_agreement": 4,
            "quality_hybrid_trend_adx": 22.0,
            "quality_hybrid_trend_er": 0.50,
            "quality_hybrid_thrust_volume_ratio": 1.6,
            "quality_hybrid_thrust_range_atr": 1.05,
            "quality_hybrid_thrust_close_loc": 0.60,
        },
    )

    assert captured["quality_hybrid_min_vote_score"] == 3.0
    assert captured["quality_hybrid_broad_agreement"] == 4
    assert captured["quality_hybrid_chop_min_agreement"] == 4
    assert captured["quality_hybrid_trend_adx"] == 22.0
    assert captured["quality_hybrid_trend_er"] == 0.50
    assert captured["quality_hybrid_thrust_volume_ratio"] == 1.6
    assert captured["quality_hybrid_thrust_range_atr"] == 1.05
    assert captured["quality_hybrid_thrust_close_loc"] == 0.60
    assert ws.messages[-1]["type"] == "config_ack"


@pytest.mark.asyncio
async def test_blocked_mode_switch_returns_actual_mode_and_reason(monkeypatch):
    class DummyScanner:
        mode = "HISTORICAL"
        _last_configure_block_reason = "outside regular market hours"

        def configure(self, **kwargs):
            assert kwargs["mode"] == "REAL"
            assert kwargs["confirm_real_mode"] is True
            assert kwargs["real_mode_verification"] == "YES"
            return False

    class DummyWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload):
            self.messages.append(payload)

    ws = DummyWebSocket()
    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    await server.handle_command(
        ws,
        {
            "cmd": "set_mode",
            "request_id": "mode-test",
            "mode": "REAL",
            "confirm_real_mode": True,
            "real_mode_verification": "YES",
        },
    )

    response = ws.messages[-1]
    assert response["type"] == "mode_updated"
    assert response["request_id"] == "mode-test"
    assert response["data"] == {
        "status": "blocked",
        "mode_command": True,
        "requested_mode": "REAL",
        "mode": "HISTORICAL",
        "reason": "outside regular market hours",
    }


def test_command_specific_checks_retained():
    assert v({"cmd": "set_power", "state": "MAYBE"}) == "invalid_state"
    assert v({"cmd": "close_trade", "trade_id": "", "price": 1}) == "invalid_trade_id"
    assert v({"cmd": "close_trade", "trade_id": "T1", "price": -1}) == "invalid_price"
    assert v({"cmd": "close_trade", "trade_id": "T1", "price": 0}) == "invalid_price"
    assert v({"cmd": "update_fyers_token", "auth_code": "x" * 5000}) == "invalid_auth_code"


def test_non_dict_rejected():
    assert v("not a dict") == "invalid_payload"
