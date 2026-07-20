from pathlib import Path
from types import SimpleNamespace
import importlib.util

import main

_TRACKER_SPEC = importlib.util.spec_from_file_location(
    "live_session_tracker", Path("scripts") / "live_session_tracker.py"
)
tracker = importlib.util.module_from_spec(_TRACKER_SPEC)
assert _TRACKER_SPEC and _TRACKER_SPEC.loader
_TRACKER_SPEC.loader.exec_module(tracker)


def test_pytest_does_not_attach_live_ut1_log_sink():
    assert main.RUNNING_UNDER_PYTEST is True
    assert main.FILE_LOG_ENABLED is False


def test_api_get_sends_dashboard_token_header(monkeypatch):
    """Sensitive /api/* reads need x-dashboard-token after Audit #10."""
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=6.0):
        captured["url"] = getattr(req, "full_url", None) or str(req)
        captured["headers"] = dict(getattr(req, "headers", {}) or {})
        return _Resp()

    monkeypatch.setattr(tracker, "_resolve_dashboard_token", lambda force=False: "tok-xyz")
    monkeypatch.setattr(tracker.urllib.request, "urlopen", fake_urlopen)

    out = tracker.api_get("/api/state")
    assert out == {"ok": True}
    assert "/api/state" in str(captured.get("url") or "")
    # urllib lower-cases header names
    headers = {str(k).lower(): v for k, v in (captured.get("headers") or {}).items()}
    assert headers.get("x-dashboard-token") == "tok-xyz"


def test_resolve_dashboard_token_from_client_config(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"dashboard_token": "runtime-abc", "local": true}'

    monkeypatch.setattr(tracker, "_DASHBOARD_TOKEN_CACHE", "")
    monkeypatch.setattr(tracker, "_DASHBOARD_TOKEN_FETCHED_AT", 0.0)
    monkeypatch.delenv("DASHBOARD_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("UT_DASHBOARD_TOKEN", raising=False)

    def fake_urlopen(req, timeout=4.0):
        return _Resp()

    monkeypatch.setattr(tracker.urllib.request, "urlopen", fake_urlopen)
    # Force settings path empty
    monkeypatch.setattr(
        tracker,
        "_resolve_dashboard_token",
        tracker._resolve_dashboard_token,
    )
    # Prevent get_settings from supplying a token
    import sys
    from types import ModuleType
    # Call underlying logic directly after clearing cache
    token = tracker._resolve_dashboard_token(force=True)
    assert token == "runtime-abc"


def test_live_session_tracker_process_uses_watch_until_and_pid_guard(tmp_path, monkeypatch):
    calls = []

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakeProc()

    monkeypatch.setattr(main.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(main, "_pid_is_running", lambda pid: False)

    pid_file = tmp_path / "tracker.pid"
    proc = main._start_tracker_process(
        since="09:00",
        until="15:40",
        interval=60,
        out_dir=tmp_path,
        pid_file=pid_file,
    )

    assert proc.pid == 4242
    assert pid_file.read_text(encoding="utf-8") == "4242"
    cmd = calls[0][0]
    assert cmd[0] == main.sys.executable
    assert str(Path("scripts") / "live_session_tracker.py") in cmd[1]
    assert cmd[-7:] == ["--watch", "--since", "09:00", "--until", "15:40", "--interval", "60"]
    assert calls[0][1]["cwd"] == str(Path(main.__file__).parent)


def test_live_session_tracker_process_skips_existing_running_pid(tmp_path, monkeypatch):
    pid_file = tmp_path / "tracker.pid"
    pid_file.write_text("9999", encoding="utf-8")
    monkeypatch.setattr(main, "_pid_is_running", lambda pid: pid == 9999)

    proc = main._start_tracker_process(
        since="09:00",
        until="15:40",
        interval=60,
        out_dir=tmp_path,
        pid_file=pid_file,
    )

    assert proc is None
    assert pid_file.read_text(encoding="utf-8") == "9999"


def test_main_starts_live_session_tracker_scheduler():
    source = Path("main.py").read_text(encoding="utf-8")
    assert "start_live_session_tracker_scheduler()" in source
    assert 'os.getenv("LIVE_SESSION_TRACKER_START", "09:00")' in source
    assert 'os.getenv("LIVE_SESSION_TRACKER_UNTIL", "15:40")' in source


def test_tracker_flags_system_function_lag_and_broker_gaps(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    monkeypatch.setattr(tracker, "LOG_FILE", tmp_path / "ut1.log")

    def fake_api_get(path, timeout=6.0):
        if path == "/api/status":
            return {
                "mode": "REAL",
                "scanner_running": False,
                "gateway_status": {
                    "broker_degraded": True,
                    "all_brokers_unavailable": True,
                    "fyers_auth_required": True,
                    "freshest": {"latency_class": "DELAYED"},
                },
            }
        if path == "/api/diagnostics":
            return {
                "scanner_freshness": {"age_seconds": 12.5, "is_stale": True},
                "latency": {"last_ms": 6500, "high_latency_events": 2},
                "worker_resources": {"async_execution_queue": {"queue_fill_pct": 80}},
                "risk_configuration": {"warnings": ["risk cap mismatch"]},
            }
        if path == "/api/trades":
            return {"open": [{"id": "T1"}], "closed": [{"id": "T0"}]}
        raise AssertionError(path)

    monkeypatch.setattr(tracker, "api_get", fake_api_get)

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, until=None))
    report = audit.cycle()

    assert report.exists()
    assert audit.system_issues["scanner_not_running"] == 1
    assert audit.system_issues["scanner_stale"] == 1
    assert audit.system_issues["scanner_lag"] == 1
    assert audit.system_issues["broker_degraded"] == 1
    assert audit.system_issues["all_brokers_unavailable"] == 1
    assert audit.system_issues["fyers_auth_required_api"] == 1
    assert audit.system_issues["delayed_market_source"] == 1
    assert audit.system_issues["high_latency_metric"] >= 1
    assert audit.system_issues["queue_backlog"] == 1
    assert audit.system_issues["risk_config_warning"] == 1
    assert audit.system_issues["open_trades_without_broker_sources"] == 1
    assert audit.max_open_trades == 1
    assert audit.max_closed_trades == 1
    text = report.read_text(encoding="utf-8")
    assert "Function / lag / redundancy audit" in text
    assert "open_trades_without_broker_sources" in text


def test_tracker_lock_blocks_duplicate_writer_and_clears_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path)
    args = SimpleNamespace(once=False, watch=True, interval=60, since="09:00", until="15:40")

    first = tracker.TrackerRunLock(args)
    assert first.acquire() is True
    assert (tmp_path / "tracker.lock").exists()
    assert (tmp_path / "tracker.pid").read_text(encoding="utf-8") == str(first.pid)

    second = tracker.TrackerRunLock(args)
    assert second.acquire() is False
    assert second.owner["pid"] == first.pid
    assert second.owner["run_id"] == first.run_id

    first.release()
    assert not (tmp_path / "tracker.lock").exists()
    assert not (tmp_path / "tracker.pid").exists()


def test_tracker_flags_trade_contract_mismatch_and_reports_diagnosis(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    monkeypatch.setattr(tracker, "LOG_FILE", tmp_path / "ut1.log")

    def fake_api_get(path, timeout=6.0):
        if path == "/api/status":
            return {
                "status": "running",
                "mode": "REAL",
                "scanner_running": True,
                "scanner_engine": "quad_prime",
                "grade_preference": "B",
                "scan_count": 42,
                "gateway_status": {"angel": True, "fyers": True, "broker_degraded": False},
            }
        if path == "/api/diagnostics":
            return {
                "scanner_freshness": {"age_seconds": 0.5, "is_stale": False},
                "latency": {"last_ms": 350, "high_latency_events": 0},
                "worker_resources": {"logical_cpu_count": 16, "active_threads": 20, "default_executor_max_workers": 20},
                "session_stats": {"entries": 24, "exits": 24},
                "risk_configuration": {"ok": True, "issues": [], "warnings": []},
                "sources": {"angel": True, "fyers": True, "broker_degraded": False, "all_brokers_unavailable": False},
                "option_history": {"attempts": 4, "hits": 13, "misses": 0, "synthetic": 0, "fallback_to_fut": 0},
                "repaint_guard": {"checked": 3, "aborted": 1, "passed": 2},
                "instrument_selection": {"OPT": 58, "FUT": 0},
                "system_performance": {"status": "OK", "issues": []},
            }
        if path == "/api/trades":
            return {
                "open": [],
                "closed": [{"id": "SESSION|NIFTY|5min|ENTRY|LONG|2026-07-01T09:45:00"}],
                "signals_total": 21,
                "closed_total": 21,
            }
        raise AssertionError(path)

    monkeypatch.setattr(tracker, "api_get", fake_api_get)

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, since="09:00", until=None), run_id="testrun")
    report = audit.cycle()
    text = report.read_text(encoding="utf-8")

    assert audit.latest_trade_contract["status"] == "MISMATCH"
    assert audit.system_issues["trade_contract_mismatch"] == 1
    assert "System Function Diagnosis" in text
    assert "Trade contract mismatch detail" in text
    assert "run_id=testrun" in text


def test_tracker_tracks_state_readiness_direction_and_engine_lab(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    monkeypatch.setattr(tracker, "LOG_FILE", tmp_path / "ut1.log")

    toggles = {
        "engine_lab_enabled": True,
        "execution_touch_quote_shadow_enabled": True,
        "stabilization_momentum_override_enabled": True,
        "reversal_entry_relief_enabled": True,
        "directional_census_reversal_relief_enabled": True,
        "price_action_reversal_relief_enabled": True,
        "late_session_choppy_entry_guard_enabled": True,
        "market_wide_reversal_enabled": True,
        "volume_spike_entry_enabled": True,
        "power_move_override_enabled": True,
        "one_min_scalping_enabled": True,
        "live_15min_momentum_override_enabled": True,
        "late_session_reclaim_override_enabled": True,
        "fifteen_min_choppy_scalp_guard_enabled": True,
        "ut_confidence_calibration_enabled": True,
        "live_intrabar_signal_entries": True,
    }
    filter_config = {
        "scanner_engine": "quad_prime",
        "grade_preference": "B",
        "ut_regime_adaptation": False,
        "late_session_reclaim_start": "14:25",
        "late_session_reclaim_end": "15:10",
        "late_session_reclaim_core_votes": 3,
        "late_session_reclaim_total_votes": 3,
        "late_session_reclaim_reclaim_votes": 2,
        "late_session_reclaim_volume_votes": 3,
        "late_session_reclaim_min_volume_ratio": 1.10,
        "late_session_reclaim_min_close_location": 0.55,
        "late_session_reclaim_max_skew_seconds": 240.0,
        "late_session_reclaim_options_sl_pct": 5.0,
        "late_session_reclaim_futures_sl_pct": 0.05,
        "late_session_reclaim_rr": 1.5,
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
        "market_wide_reversal_core_votes": 3,
        "market_wide_reversal_total_votes": 3,
        "market_wide_reversal_min_range_atr_ratio": 1.10,
        "market_wide_reversal_min_range_atr_ratio_5min": 1.10,
        "market_wide_reversal_min_range_atr_ratio_15min": 0.50,
        "market_wide_reversal_midcap_min_range_atr_ratio_5min": 0.65,
        "market_wide_reversal_midcap_min_range_atr_ratio_15min": 0.45,
        "market_wide_reversal_min_close_location": 0.65,
        "market_wide_reversal_max_skew_seconds_5min": 180.0,
        "market_wide_reversal_max_skew_seconds_15min": 900.0,
        "power_move_spike_bypass_15m": True,
        "power_move_spike_range_atr_mult": 2.5,
        "volume_spike_entry_min_volume_ratio": 2.8,
        "volume_spike_entry_strong_1min_min_volume_ratio": 2.0,
        "volume_spike_entry_strong_1min_adx_threshold": 25.0,
        "volume_spike_entry_strong_1min_intel_threshold": 0.70,
        "volume_spike_entry_min_range_atr_ratio": 1.5,
        "volume_spike_entry_min_close_location": 0.65,
        "volume_spike_entry_futures_sl_pct": 15.0,
        "volume_spike_entry_options_sl_pct": 30.0,
        "volume_spike_entry_1min_scalp_rr": 1.5,
        "volume_spike_entry_stabilization_seconds": 5,
        "live_signal_stabilization_seconds_15min": 30,
        "ut_no_entry_after": "15:01",
        "live_15min_momentum_min_confidence": 0.20,
        "live_15min_momentum_volume_ratio": 2.0,
        "live_15min_momentum_range_atr_ratio": 1.25,
        "live_15min_momentum_close_location": 0.65,
        "live_15min_momentum_adx_threshold": 24.0,
        "live_15min_momentum_intel_threshold": 0.65,
    }

    def instrument_payload(name):
        return {
            "spot_price": 24000.0,
            "change_pct": 0.4,
            "mtf": {
                "confluence_signal": "BUY",
                "confluence_score": 0.82,
                "state_5min": {"position_label": "LONG", "adx_value": 28.0},
                "state_15min": {"position_label": "LONG", "adx_value": 31.0},
            },
            "intelligence": {
                "volume": {"volume_ratio": 2.2},
                "oi": {"signal": "BULLISH"},
                "pcr": {"signal": "BULLISH", "primary_pcr": 1.12},
                "regime": {"regime": "BREAKOUT_UP", "direction": "UP", "confidence": 72},
                "order_flow": {"signal": "BULLISH", "buy_sell_ratio": 1.4},
                "aggregate": {"signal": "BUY", "score": 0.78},
            },
            "pre_candle_prediction": {
                "5min": {"state": "LIKELY_5M_FLIP", "direction": "LONG", "confidence": 0.66, "reason": "core agrees"},
                "15min": {"state": "EARLY_CONTINUATION", "direction": "LONG", "confidence": 0.61, "reason": "partial agrees"},
            },
        }

    engine_rows = [
        {
            "engine": f"mode_{idx}",
            "active": idx == 9,
            "signals": 10 + idx,
            "fresh_signals": 0,
            "buy": 6,
            "sell": 4,
            "open": 0,
            "closed": 3,
            "wins": 2,
            "losses": 1,
            "win_rate": 66.7,
            "total_points": 10.0 + idx,
            "profit_factor": 1.5,
            "sharpe": 0.4,
            "errors": 0,
        }
        for idx in range(1, 13)
    ]

    def fake_api_get(path, timeout=6.0):
        if path == "/api/status":
            return {
                "status": "running",
                "mode": "REAL",
                "scanner_running": True,
                "config": {**toggles, **filter_config},
                "scan_count": 42,
                "gateway_status": {
                    "angel": True,
                    "fyers": True,
                    "broker_degraded": False,
                    "all_brokers_unavailable": False,
                    "fyers_auth_required": False,
                },
            }
        if path == "/api/diagnostics":
            return {
                "scanner_freshness": {"age_seconds": 0.5, "is_stale": False},
                "latency": {"last_ms": 350, "high_latency_events": 0},
                "worker_resources": {"logical_cpu_count": 16, "active_threads": 20, "default_executor_max_workers": 20},
                "session_stats": {"entries": 0, "exits": 0},
                "risk_configuration": {"ok": True, "issues": [], "warnings": []},
                "sources": {
                    "angel": True,
                    "fyers": True,
                    "broker_degraded": False,
                    "all_brokers_unavailable": False,
                    "fyers_auth_required": False,
                },
                "option_history_quality": {"status": "OK", "fallback_rate_pct": 0},
            }
        if path == "/api/trades":
            return {"open": [], "closed": [], "signals": [], "open_total": 0, "closed_total": 0, "signals_total": 0}
        if path == "/api/state":
            return {
                "mode": "REAL",
                "timestamp": "2026-07-03T09:10:00+05:30",
                "config": {**toggles, **filter_config},
                "instruments": {name: instrument_payload(name) for name in tracker.EXPECTED_INDICES},
                "engine_lab": {
                    "enabled": True,
                    "active_engine": "quad_prime",
                    "rows": engine_rows,
                    "summary": {
                        "engines": 12,
                        "signals": 120,
                        "fresh_signals": 0,
                        "open": 0,
                        "closed": 30,
                        "latency_ms": 120,
                        "basis": "RAW SHADOW",
                    },
                },
                "activity_log": [
                    {"time": "09:10:00", "type": "system", "msg": "scanner ready"},
                    {"time": "09:09:59", "type": "trade", "msg": "no open trades"},
                ],
            }
        raise AssertionError(path)

    monkeypatch.setattr(tracker, "api_get", fake_api_get)

    audit = tracker.Tracker(
        SimpleNamespace(once=True, watch=False, interval=60, since="09:00", until=None, track_state=True),
        run_id="testrun",
    )
    report = audit.cycle()
    text = report.read_text(encoding="utf-8")

    assert audit.latest_readiness_assessment["status"] == "READY"
    assert audit.latest_readiness_assessment["score"] == 100
    assert audit.latest_market_direction["broad_direction"] == "BULLISH"
    assert audit.max_engine_lab_rows == 12
    assert audit.system_issues["engine_lab_incomplete"] == 0
    assert audit.system_issues["market_intel_missing"] == 0
    assert audit.latest_feature_matrix["late_session_reclaim_override_enabled"]["status"] == "on"
    assert audit.latest_feature_matrix["fifteen_min_choppy_scalp_guard_enabled"]["status"] == "on"
    assert audit.latest_quad_prime_filters["missing_count"] == 0
    assert audit.latest_quad_prime_filters["groups"]["late_session_reclaim"]["settings"][1]["value"] == "14:25"
    assert audit.latest_quad_prime_filters["groups"]["midcap_15min_buffer"]["settings"][0]["value"] == 60.0
    assert "Upcoming Session Readiness" in text
    assert "Broad Market Direction & Intelligence" in text
    assert "Engine Lab 12-Mode Coverage" in text
    assert "Quad Prime / Late-Session Filter Settings" in text
    assert "late_session_reclaim" in text
    assert "Basket votes=3" in text
    assert "MIDCP 15m risky buffer" in text
    assert "Status `READY`" in text


def test_tracker_accepts_canonical_closed_rows_without_mirrored_signals(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    monkeypatch.setattr(tracker, "LOG_FILE", tmp_path / "ut1.log")

    def fake_api_get(path, timeout=6.0):
        if path == "/api/status":
            return {
                "status": "running",
                "mode": "HISTORICAL",
                "scanner_running": True,
                "scanner_engine": "quad_prime",
                "grade_preference": "B",
                "scan_count": 42,
                "gateway_status": {"angel": True, "fyers": True, "broker_degraded": False},
            }
        if path == "/api/diagnostics":
            return {
                "scanner_freshness": {"age_seconds": 0.5, "is_stale": False},
                "latency": {"last_ms": 350, "high_latency_events": 0},
                "worker_resources": {"logical_cpu_count": 16, "active_threads": 20, "default_executor_max_workers": 20},
                "session_stats": {"entries": 22, "exits": 22},
                "risk_configuration": {"ok": True, "issues": [], "warnings": []},
            }
        if path == "/api/trades":
            return {
                "open": [],
                "closed": [{"id": "SESSION|NIFTY|5min|ENTRY|LONG|2026-07-01T09:45:00"}],
                "signals": [],
                "signals_total": 0,
                "closed_total": 22,
                "open_total": 0,
            }
        raise AssertionError(path)

    monkeypatch.setattr(tracker, "api_get", fake_api_get)

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, since="09:00", until=None), run_id="testrun")
    report = audit.cycle()
    text = report.read_text(encoding="utf-8")

    assert audit.latest_trade_contract["status"] == "OK"
    assert audit.latest_trade_contract["trades_rows_total"] == 22
    assert audit.latest_trade_contract["trades_signals_total"] == 0
    assert audit.system_issues["trade_contract_mismatch"] == 0
    assert "api rows/closed `22/22`" in text


def test_tracker_accepts_restored_session_signals_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    monkeypatch.setattr(tracker, "LOG_FILE", tmp_path / "ut1.log")

    def fake_api_get(path, timeout=6.0):
        if path == "/api/status":
            return {
                "status": "running",
                "mode": "REAL",
                "scanner_running": True,
                "config": {"scanner_engine": "quad_prime", "grade_preference": "B"},
                "scan_count": 286,
                "gateway_status": {"angel": True, "fyers": True, "broker_degraded": False},
            }
        if path == "/api/diagnostics":
            return {
                "scanner_freshness": {"age_seconds": 0.5, "is_stale": False},
                "latency": {"last_ms": 350, "high_latency_events": 0},
                "worker_resources": {"logical_cpu_count": 16, "active_threads": 20, "default_executor_max_workers": 20},
                "session_stats": {"entries": 3, "exits": 3},
                "risk_configuration": {"ok": True, "issues": [], "warnings": []},
            }
        if path == "/api/trades":
            return {
                "open": [],
                "closed": [],
                "summary": {"total_trades": 3},
                "signals": [
                    {"id": "SESSION|NIFTY|5min|EXIT|LONG|2026-07-02T09:05:00", "action": "EXIT"},
                    {"id": "SESSION|SENSEX|5min|EXIT|LONG|2026-07-02T09:15:00", "is_exit": True},
                    {"id": "SESSION|SENSEX|15min|EXIT|SHORT|2026-07-02T10:00:00", "action": "EXIT"},
                ],
                "signals_total": 3,
                "closed_total": 0,
                "open_total": 0,
                "meta": {"source": "restored_session_signals", "restored_signal_rows": 3},
            }
        raise AssertionError(path)

    monkeypatch.setattr(tracker, "api_get", fake_api_get)

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, since="09:00", until=None), run_id="testrun")
    report = audit.cycle()
    text = report.read_text(encoding="utf-8")

    assert audit.latest_status_summary["scanner_engine"] == "quad_prime"
    assert audit.latest_status_summary["grade_preference"] == "B"
    assert audit.latest_trade_contract["status"] == "OK"
    assert audit.latest_trade_contract["trades_rows_total"] == 3
    assert audit.latest_trade_contract["trades_closed_total"] == 3
    assert audit.latest_trade_contract["visible_signal_rows"] == 3
    assert audit.system_issues["trade_contract_mismatch"] == 0
    assert "Trade contract: `OK`" in text
    assert "api rows/closed `3/3`" in text


def test_tracker_compares_mixed_historical_payload_to_restored_session_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    monkeypatch.setattr(tracker, "LOG_FILE", tmp_path / "ut1.log")

    def fake_api_get(path, timeout=6.0):
        if path == "/api/status":
            return {
                "status": "running",
                "mode": "HISTORICAL",
                "scanner_running": True,
                "scanner_engine": "auto",
                "grade_preference": "B",
                "scan_count": 80,
            }
        if path == "/api/diagnostics":
            return {
                "scanner_freshness": {"age_seconds": 0.5, "is_stale": False},
                "latency": {"last_ms": 350, "high_latency_events": 0},
                "worker_resources": {"logical_cpu_count": 16, "active_threads": 20, "default_executor_max_workers": 20},
                "session_stats": {"entries": 22, "exits": 22},
                "risk_configuration": {"ok": True, "issues": [], "warnings": []},
            }
        if path == "/api/trades":
            return {
                "open": [],
                "closed": [{"id": "SESSION|NIFTY|5min|ENTRY|LONG|2026-07-01T09:45:00"}],
                "signals": [],
                "signals_total": 0,
                "closed_total": 53,
                "open_total": 0,
                "meta": {
                    "source": "historical_session_plus_simulation",
                    "restored_signal_rows": 22,
                    "simulated_signal_rows": 31,
                },
            }
        raise AssertionError(path)

    monkeypatch.setattr(tracker, "api_get", fake_api_get)

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, since="09:00", until=None), run_id="testrun")
    audit.cycle()

    assert audit.latest_trade_contract["status"] == "OK"
    assert audit.latest_trade_contract["trades_rows_total"] == 53
    assert audit.latest_trade_contract["comparison_rows_total"] == 22
    assert audit.latest_trade_contract["restored_signal_rows"] == 22
    assert audit.latest_trade_contract["simulated_signal_rows"] == 31
    assert audit.system_issues["trade_contract_mismatch"] == 0


def test_tracker_treats_pure_historical_trades_as_backtest_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    monkeypatch.setattr(tracker, "LOG_FILE", tmp_path / "ut1.log")

    def fake_api_get(path, timeout=6.0):
        if path == "/api/status":
            return {
                "status": "running",
                "mode": "HISTORICAL",
                "scanner_running": True,
                "scanner_engine": "quad_prime",
                "scan_count": 4,
            }
        if path == "/api/diagnostics":
            return {
                "scanner_freshness": {
                    "age_seconds": 65.0,
                    "is_stale": True,
                    "is_calculating": False,
                    "calculation_lock": False,
                },
                "recalculation": {"status": "idle"},
                "latency": {"last_ms": 350, "high_latency_events": 0},
                "worker_resources": {"logical_cpu_count": 16, "active_threads": 20, "default_executor_max_workers": 20},
                "session_stats": {"entries": 0, "exits": 0},
                "risk_configuration": {"ok": True, "issues": [], "warnings": []},
            }
        if path == "/api/trades":
            return {
                "open": [],
                "closed": [
                    {"id": "H_NIFTY_5min_20260629100000"},
                    {"id": "H_NIFTY_5min_20260629100500"},
                ],
                "closed_total": 8,
                "open_total": 0,
                "signals_total": 8,
                "meta": {
                    "mode": "HISTORICAL",
                    "source": "historical_trade_manager",
                    "simulated_signal_rows": 8,
                },
            }
        raise AssertionError(path)

    monkeypatch.setattr(tracker, "api_get", fake_api_get)

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, since="09:00", until=None), run_id="testrun")
    report = audit.cycle()
    text = report.read_text(encoding="utf-8")

    assert audit.latest_trade_contract["status"] == "OK"
    assert audit.latest_trade_contract["comparison_basis"] == "historical_rows"
    assert audit.latest_trade_contract["trades_rows_total"] == 8
    assert audit.system_issues["trade_contract_mismatch"] == 0
    assert audit.system_issues["scanner_stale"] == 0
    assert "basis `historical_rows`" in text


def test_tracker_treats_historical_building_guard_as_pending_not_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    monkeypatch.setattr(tracker, "LOG_FILE", tmp_path / "ut1.log")

    def fake_api_get(path, timeout=6.0):
        if path == "/api/status":
            return {
                "status": "running",
                "mode": "HISTORICAL",
                "scanner_running": True,
                "scanner_engine": "quad_prime",
                "grade_preference": "B",
                "scan_count": 42,
            }
        if path == "/api/diagnostics":
            return {
                "scanner_freshness": {"age_seconds": 0.5, "is_stale": False, "is_calculating": True},
                "latency": {"last_ms": 350, "high_latency_events": 0},
                "worker_resources": {"logical_cpu_count": 16, "active_threads": 20, "default_executor_max_workers": 20},
                "session_stats": {"entries": 22, "exits": 22},
                "risk_configuration": {"ok": True, "issues": [], "warnings": []},
            }
        if path == "/api/trades":
            return {
                "open": [],
                "closed": [],
                "signals": [],
                "signals_total": 0,
                "closed_total": 0,
                "open_total": 0,
                "meta": {"source": "historical_building_guard"},
            }
        raise AssertionError(path)

    monkeypatch.setattr(tracker, "api_get", fake_api_get)

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, since="09:00", until=None), run_id="testrun")
    audit.cycle()

    assert audit.latest_trade_contract["status"] == "BUILDING"
    assert audit.latest_trade_contract["mismatches"] == []
    assert audit.system_issues["trade_contract_mismatch"] == 0


def test_tracker_flags_repeated_warning_noise(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    log_file = tmp_path / "ut1.log"
    monkeypatch.setattr(tracker, "LOG_FILE", log_file)
    today = tracker.now_ist().strftime("%Y-%m-%d")
    log_file.write_text(
        "".join(
            f"{today} 10:10:0{i} | WARNING | DIVERGENCE: SENSEX Price falling but Order Flow shows Accumulation {i}\n"
            for i in range(6)
        ),
        encoding="utf-8",
    )

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, until=None))

    assert audit.system_issues["repeated_log_noise"] >= 1
    assert any(count >= 5 for count in audit.repeated_log_fingerprints.values())


def test_tracker_since_filters_pre_window_log_noise(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    log_file = tmp_path / "ut1.log"
    monkeypatch.setattr(tracker, "LOG_FILE", log_file)
    today = tracker.now_ist().strftime("%Y-%m-%d")
    log_file.write_text(
        "\n".join(
            [
                f"{today} 05:10:00 | ERROR | data.market_data:on_error:404 - Fyers WS Error: early noise",
                f"{today} 09:01:00 | ERROR | data.market_data:on_error:404 - Fyers WS Error: tracked noise",
            ]
        ),
        encoding="utf-8",
    )

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, since="09:00", until=None))

    assert audit.counts["generic_error"] == 1
    assert audit.samples["generic_error"][0].endswith("tracked noise")


def test_tracker_ignores_test_harness_order_noise(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    log_file = tmp_path / "ut1.log"
    monkeypatch.setattr(tracker, "LOG_FILE", log_file)
    today = tracker.now_ist().strftime("%Y-%m-%d")
    log_file.write_text(
        "\n".join(
            [
                f"{today} 09:20:00 | ERROR | trading.trade_manager:place_bg:1323 - Broker entry failed for NIFTYTESTFUT after 7 attempts: Broker rejected order",
                f"{today} 09:20:01 | CRITICAL | trading.trade_manager:emergency_flatten_trade:747 - Recovery fallback flatten submitted for NIFTYTESTFUT: EXIT1",
                f"{today} 09:20:02 | INFO | trading.trade_manager:_finalize_closed_trade:560 - Trade Closed: NIFTY @ 103.00 (Reason: KILL_ALL_TEST)",
                f"{today} 09:21:00 | ERROR | trading.broker:get_positions:403 - Failed to fetch position book: live issue",
            ]
        ),
        encoding="utf-8",
    )

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, since="09:00", until=None))

    assert audit.ignored_artifacts["test_harness_log"] == 3
    assert audit.counts["broker_order_fail"] == 0
    assert audit.counts["auto_flatten"] == 0
    assert audit.counts["generic_error"] == 1


def test_tracker_clean_window_resets_on_latest_runtime_start(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    log_file = tmp_path / "ut1.log"
    monkeypatch.setattr(tracker, "LOG_FILE", log_file)
    today = tracker.now_ist().strftime("%Y-%m-%d")
    log_file.write_text(
        "\n".join(
            [
                f"{today} 09:05:00 | ERROR | scanner:_process_instrument:4578 - Intelligence analysis failed for NIFTY: old fixed bug",
                f"{today} 11:41:49 | INFO | __main__:main:454 - Starting dashboard server on 127.0.0.1:8001",
                f"{today} 11:45:00 | ERROR | data.market_data:on_error:440 - Fyers WS Error: clean-window feed issue",
            ]
        ),
        encoding="utf-8",
    )

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, since="09:00", until=None))
    report = audit.write_report()
    text = report.read_text(encoding="utf-8")

    assert audit.counts["generic_error"] == 2
    assert audit.clean_counts["generic_error"] == 1
    assert audit.clean_start_ts == f"{today} 11:41:49"
    assert "Clean Window Since Latest Boundary" in text
    assert audit.clean_start_reason == "after latest runtime start"
    assert "old fixed bug" not in "\n".join(audit.clean_samples["generic_error"])


def test_tracker_test_artifact_resets_clean_window(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    log_file = tmp_path / "ut1.log"
    monkeypatch.setattr(tracker, "LOG_FILE", log_file)
    today = tracker.now_ist().strftime("%Y-%m-%d")
    log_file.write_text(
        "\n".join(
            [
                f"{today} 11:41:49 | INFO | __main__:main:454 - Starting dashboard server on 127.0.0.1:8001",
                f"{today} 11:44:30 | ERROR | trading.broker:get_positions:403 - Failed before tests",
                f"{today} 11:44:31 | ERROR | trading.trade_manager:place_bg:1323 - Broker entry failed for NIFTYTESTFUT after 7 attempts",
                f"{today} 11:45:00 | ERROR | data.market_data:on_error:440 - Fyers WS Error: after tests",
            ]
        ),
        encoding="utf-8",
    )

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, since="09:00", until=None))

    assert audit.ignored_artifacts["test_harness_log"] == 1
    assert audit.clean_start_ts == f"{today} 11:44:31"
    assert audit.clean_start_reason == "after latest test/mock artifact"
    assert audit.clean_counts["generic_error"] == 1
    assert audit.clean_samples["generic_error"][0].endswith("after tests")


def test_tracker_post_market_artifact_does_not_clear_market_clean_window(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    log_file = tmp_path / "ut1.log"
    monkeypatch.setattr(tracker, "LOG_FILE", log_file)
    today = tracker.now_ist().strftime("%Y-%m-%d")
    log_file.write_text(
        "\n".join(
            [
                f"{today} 09:20:00 | ERROR | scanner:process_task:3326 - Timeout processing NIFTY! Signal generation might be hanging.",
                f"{today} 18:01:41 | ERROR | trading.trade_manager:place_bg:1323 - Broker entry failed for NIFTYTESTFUT after 7 attempts",
            ]
        ),
        encoding="utf-8",
    )

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, since="09:00", until=None))
    report = audit.write_report()
    text = report.read_text(encoding="utf-8")

    # The 09:20 timeout line is now recognized specifically as signal_gen_timeout
    # (a more actionable category than the generic_error catch-all); the clean-window
    # boundary behavior it probes is unchanged.
    assert audit.clean_counts["signal_gen_timeout"] == 0
    assert audit.market_clean_counts["signal_gen_timeout"] == 1
    assert audit.market_clean_error_sources["scanner:process_task:3326"] == 1
    assert "Market-Hours Clean Window (Default View)" in text
    assert "scanner:process_task:3326" in text


def test_tracker_health_snapshot_reports_restored_session_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    monkeypatch.setattr(tracker, "LOG_FILE", tmp_path / "ut1.log")

    def fake_api_get(path, timeout=6.0):
        if path == "/api/status":
            return {"mode": "REAL", "scanner_running": True}
        if path == "/api/diagnostics":
            return {
                "scanner_freshness": {"age_seconds": 0.5, "is_stale": False},
                "session_stats": {"entries": 3, "exits": 3},
            }
        if path == "/api/trades":
            return {
                "open": [],
                "closed": [],
                "signals": [
                    {"id": f"SESSION|NIFTY|5min|EXIT|LONG|t{i}", "action": "EXIT", "is_exit": True}
                    for i in range(3)
                ],
                "meta": {"source": "restored_session_signals", "restored_signal_rows": 3},
            }
        raise AssertionError(path)

    monkeypatch.setattr(tracker, "api_get", fake_api_get)

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, until=None))
    report = audit.cycle()
    text = report.read_text(encoding="utf-8")

    # Raw open/closed arrays are empty for restored sessions; the health snapshot
    # must surface the contract-aware session-row count instead of "open 0 closed 0".
    assert audit.max_session_rows == 3
    assert audit.max_session_closed == 3
    assert "session rows `3`" in text
    assert audit.latest_trade_contract.get("status") == "OK"


def test_tracker_readiness_suggestions_ignore_pre_boundary_test_noise(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "OUT_DIR", tmp_path / "tracker")
    log_file = tmp_path / "ut1.log"
    monkeypatch.setattr(tracker, "LOG_FILE", log_file)
    today = tracker.now_ist().strftime("%Y-%m-%d")
    log_file.write_text(
        "\n".join(
            [
                f"{today} 10:00:00 | CRITICAL | data.market_data:_refresh_fyers_token:587 - Missing Fyers credentials in .env",
                f"{today} 10:01:00 | CRITICAL | engine.safe_mode:validate_real_mode_readiness:44 - segment capital exceeds total capital",
                f"{today} 10:05:00 | INFO | trading.broker:place_order:236 - PLACING REAL ORDER: BUY 10 NIFTYTESTFUT",
            ]
        ),
        encoding="utf-8",
    )

    audit = tracker.Tracker(SimpleNamespace(once=True, watch=False, interval=60, since="09:00", until=None))
    report = audit.write_report()
    text = report.read_text(encoding="utf-8")

    # Full-day counts keep the noise-aware totals...
    assert audit.counts["broker_creds_missing"] == 1
    assert audit.counts["capital_misconfig"] == 1
    # ...but the clean windows were reset by the in-market test artifact, so the
    # severity-ranked fix list must not raise false CRITICAL readiness items.
    assert audit.clean_counts["broker_creds_missing"] == 0
    assert "SESSION READINESS" not in text
    assert "Broker-auth noise (pre-boundary only" in text
    assert "Capital-config noise (pre-boundary only" in text
