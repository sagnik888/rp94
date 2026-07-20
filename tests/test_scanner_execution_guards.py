from datetime import datetime
from types import SimpleNamespace

from config.settings import get_settings
from engine.signal_processor import SignalProcessor
import scanner as scanner_module
from scanner import Scanner


def test_partial_configure_does_not_null_advanced_settings():
    # Regression: a partial configure (e.g. the auto-mode toggle) forwards every other
    # field as None via msg.get(). The kwargs setattr loop must SKIP None so it never
    # nulls advanced settings (price-action / regime-veto / volume-spike / time cutoffs).
    s = get_settings()
    saved = (s.regime_veto_adx_floor, s.price_action_module_enabled,
             s.ut_no_entry_after, s.volume_spike_entry_min_volume_ratio)
    s.regime_veto_adx_floor = 18.0
    s.price_action_module_enabled = True
    s.ut_no_entry_after = "15:01"
    s.volume_spike_entry_min_volume_ratio = 2.5
    try:
        sc = Scanner.__new__(Scanner)
        sc.mode = "HISTORICAL"
        sc.auto_mode = False
        sc.on_notification = None
        sc.futures_sl_pct = 0.3
        sc.options_sl_pct = 15.0
        sc.max_daily_loss_pct = 3.0
        sc.risk_fut_pct = 2.0
        sc.risk_opt_pct = 8.0
        sc.capital_fut = 250000.0
        sc.capital_opt = 100000.0
        sc.capital_total = 1100000.0
        sc.trades = SimpleNamespace(
            update_risk_settings=lambda *a, **k: None,
            has_live_exposure=lambda: False,
        )
        sc.risk_manager = SimpleNamespace(update_settings=lambda *a, **k: None)
        # the tail of configure() publishes the full broadcast snapshot (needs whole-scanner
        # state); stub it — this test only cares that the kwargs loop didn't null settings.
        sc._publish_config_snapshot = lambda *a, **k: None
        sc.mtf = SimpleNamespace(apply_engine_params=lambda *a, **k: None,
                                 set_scanner_engine=lambda *a, **k: None)
        # advanced fields arrive as None (absent from a partial toggle message)
        sc.configure(
            regime_veto_adx_floor=None,
            price_action_module_enabled=None,
            ut_no_entry_after=None,
            volume_spike_entry_min_volume_ratio=None,
        )
        assert s.regime_veto_adx_floor == 18.0
        assert s.price_action_module_enabled is True
        assert s.ut_no_entry_after == "15:01"
        assert s.volume_spike_entry_min_volume_ratio == 2.5
    finally:
        (s.regime_veto_adx_floor, s.price_action_module_enabled,
         s.ut_no_entry_after, s.volume_spike_entry_min_volume_ratio) = saved


def test_runtime_configure_can_skip_settings_persistence(monkeypatch):
    calls = []
    monkeypatch.setattr(scanner_module, "schedule_settings_save", lambda settings: calls.append(settings))

    sc = Scanner.__new__(Scanner)
    sc.mode = "HISTORICAL"
    sc.auto_mode = True
    sc.on_notification = None
    sc.futures_sl_pct = 0.3
    sc.options_sl_pct = 15.0
    sc.max_daily_loss_pct = 3.0
    sc.risk_fut_pct = 2.0
    sc.risk_opt_pct = 8.0
    sc.capital_fut = 250000.0
    sc.capital_opt = 100000.0
    sc.capital_total = 1100000.0
    sc.trades = SimpleNamespace(
        update_risk_settings=lambda *a, **k: None,
        has_live_exposure=lambda: False,
    )
    sc.risk_manager = SimpleNamespace(update_settings=lambda *a, **k: None)
    sc._publish_config_snapshot = lambda *a, **k: None
    sc.mtf = SimpleNamespace(apply_engine_params=lambda *a, **k: None)

    assert sc.configure(auto_mode=False, persist_settings=False) is True
    assert sc.auto_mode is False
    assert calls == []

    assert sc.configure(auto_mode=False) is True
    assert len(calls) == 1


def test_quality_hybrid_threshold_change_queues_historical_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(scanner_module, "schedule_settings_save", lambda settings: calls.append(settings))

    s = get_settings()
    original = s.quality_hybrid_min_vote_score
    try:
        s.quality_hybrid_min_vote_score = 3.0
        sc = Scanner.__new__(Scanner)
        sc.mode = "HISTORICAL"
        sc.auto_mode = False
        sc.on_notification = None
        sc.futures_sl_pct = 0.3
        sc.options_sl_pct = 15.0
        sc.max_daily_loss_pct = 3.0
        sc.risk_fut_pct = 2.0
        sc.risk_opt_pct = 8.0
        sc.capital_fut = 250000.0
        sc.capital_opt = 100000.0
        sc.capital_total = 1100000.0
        sc.scan_count = 9
        sc._historical_rebuild_pending = False
        sc._recalculation_status = {"status": "idle"}
        sc._cached_results = {"old": object()}
        sc._intel_cache = {"old": object()}
        sc._intel_cache_time = {"old": object()}
        sc.latest_regimes = {"NIFTY": "RANGING"}
        sc._latest_trade_candidates = {"old": object()}
        sc._latest_exit_candidates = {"old": object()}
        sc._last_signal_time = {"old": object()}
        sc._last_signal_identity = {"old": object()}
        sc._last_live_signal_candle_time = {"old": object()}
        sc._pending_live_signals = {"old": object()}
        sc._post_repaint_recheck = {"old": object()}
        queued = []
        sc.queue_full_recalculation = lambda reason: queued.append(reason)
        sc._load_session_trade_candidates = lambda: None
        sc._next_simulation_id = lambda: None
        sc._stamp_simulation_diagnostics = lambda: None
        sc._publish_config_snapshot = lambda *a, **k: None
        sc.trades = SimpleNamespace(
            closed_trades=[SimpleNamespace(id="H_OLD"), SimpleNamespace(id="LIVE_KEEP")],
            update_risk_settings=lambda *a, **k: None,
            has_live_exposure=lambda: False,
        )
        sc.perf = SimpleNamespace(reset=lambda: None)
        sc.risk_manager = SimpleNamespace(update_settings=lambda *a, **k: None)
        sc.mtf = SimpleNamespace(
            apply_engine_params=lambda *a, **k: None,
            _result_cache={"old": object()},
            _data_hash={"old": object()},
        )

        assert sc.configure(quality_hybrid_min_vote_score=3.5, persist_settings=False) is True
        assert s.quality_hybrid_min_vote_score == 3.5
        assert sc._historical_rebuild_pending is True
        assert sc._recalculation_status["status"] == "queued"
        assert sc._recalculation_status["reason"] == "settings-refresh"
        assert queued == ["settings-refresh"]
        assert sc.scan_count == 0
        assert sc._cached_results == {}
        assert [trade.id for trade in sc.trades.closed_trades] == ["LIVE_KEEP"]
        assert calls == []
    finally:
        s.quality_hybrid_min_vote_score = original


def _scanner_with_trade_guard(exposure):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = False
    scanner.capital_total = 500000.0
    scanner.on_notification = None
    scanner.trades = SimpleNamespace(has_live_exposure=lambda: exposure)
    return scanner


def test_mode_switch_is_blocked_while_real_exposure_exists():
    scanner = _scanner_with_trade_guard(True)

    configured = scanner.configure(mode="HISTORICAL")

    assert configured is False
    assert scanner.mode == "REAL"


def test_rejected_real_auto_configuration_does_not_partially_mutate_capital():
    scanner = _scanner_with_trade_guard(False)

    configured = scanner.configure(capital_total=900000, auto_mode=True)

    assert configured is False
    assert scanner.capital_total == 500000.0
    assert scanner.auto_mode is False


def test_rejected_batched_real_auto_switch_does_not_partially_mutate_capital():
    scanner = _scanner_with_trade_guard(False)
    scanner.mode = "HISTORICAL"

    configured = scanner.configure(mode="real", capital_total=900000, auto_mode=True)

    assert configured is False
    assert scanner.mode == "HISTORICAL"
    assert scanner.capital_total == 500000.0
    assert scanner.auto_mode is False


def test_auto_mode_configure_uses_fail_closed_boolean_parser():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner.auto_mode = True
    scanner.on_notification = None
    scanner.futures_sl_pct = 0.15
    scanner.options_sl_pct = 15.0
    scanner.capital_total = 500000.0
    scanner.capital_fut = 300000.0
    scanner.capital_opt = 200000.0
    scanner.risk_fut_pct = 1.0
    scanner.risk_opt_pct = 7.0
    scanner.max_daily_loss_pct = 3.0
    scanner.trades = SimpleNamespace(update_risk_settings=lambda *args, **kwargs: None)
    scanner.risk_manager = SimpleNamespace(update_settings=lambda *args, **kwargs: None)
    scanner._publish_config_snapshot = lambda: None

    assert scanner.configure(auto_mode="false") is True
    assert scanner.auto_mode is False

    assert scanner.configure(auto_mode="unexpected") is True
    assert scanner.auto_mode is False


def test_confirmed_real_switch_runs_safe_mode_before_mutating(monkeypatch):
    scanner = _scanner_with_trade_guard(False)
    scanner.mode = "HISTORICAL"
    scanner.data = SimpleNamespace()
    scanner.capital_total = 500000.0

    monkeypatch.setattr(
        "scanner.validate_real_mode_readiness",
        lambda settings, provider: {
            "ok": False,
            "blockers": ["risk configuration: segment capital exceeds total capital"],
            "warnings": [],
        },
    )

    configured = scanner.configure(
        mode="REAL",
        capital_total=900000,
        confirm_real_mode=True,
        real_mode_verification="YES",
    )

    assert configured is False
    assert scanner.mode == "HISTORICAL"
    assert scanner.capital_total == 500000.0


def _scanner_for_morning_refresh(market_open=False):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._morning_refresh_last_date = None
    scanner._morning_refresh_inflight = False
    scanner._morning_refresh_summary = {}
    scanner._morning_refresh_attempts = {}
    scanner._morning_refresh_catchup_done_date = None
    scanner.data = SimpleNamespace(
        is_special_trading_session=lambda dt: False,
        is_market_holiday=lambda dt: False,
        is_market_open=lambda dt=None: market_open,
    )
    scanner.log_event = lambda *args, **kwargs: None
    return scanner


def _premarket_clock_settings():
    return SimpleNamespace(
        real_mode_premarket_arm_start="09:00:00",
        real_mode_premarket_settle_end="09:08:00",
        real_mode_premarket_refresh_end="09:14:30",
        real_mode_live_start="09:15:00",
        morning_refresh_catchup_at_live_open=True,
        morning_refresh_max_attempts=3,
    )


def _capture_schedule(scanner):
    scheduled = []

    def _schedule(indices, now, *, reason="scheduled", force_full=False):
        scheduled.append({"indices": indices, "now": now, "reason": reason, "force_full": force_full})

    scanner._schedule_morning_market_refresh = _schedule
    return scheduled


def test_real_mode_premarket_refresh_schedules_before_market_open(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _premarket_clock_settings)
    scanner = _scanner_for_morning_refresh(market_open=False)
    scheduled = _capture_schedule(scanner)

    blocked_before_arm = scanner._ensure_market_session_refresh(
        {"NIFTY": {}},
        datetime(2026, 6, 16, 8, 59, 59),
    )
    premarket = scanner._ensure_market_session_refresh(
        {"NIFTY": {}},
        datetime(2026, 6, 16, 9, 0, 0),
    )

    assert blocked_before_arm is False
    assert premarket is True
    assert len(scheduled) == 1
    assert scheduled[0]["force_full"] is True


def test_real_mode_premarket_refresh_phase_boundaries(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _premarket_clock_settings)
    scanner = _scanner_for_morning_refresh(market_open=False)
    logs = []
    scheduled = _capture_schedule(scanner)
    scanner.log_event = lambda message, event_type="info": logs.append((message, event_type))

    for clock, expected in (
        (datetime(2026, 6, 16, 9, 0, 0), "Pre-Open Settlement"),
        (datetime(2026, 6, 16, 9, 8, 0), "Pre-Open Settlement"),
        (datetime(2026, 6, 16, 9, 8, 1), "Pre-Open Analysis"),
        (datetime(2026, 6, 16, 9, 14, 30), "Pre-Open Analysis"),
        (datetime(2026, 6, 16, 9, 14, 31), "Market-Open Handoff"),
    ):
        scanner._morning_refresh_inflight = False
        assert scanner._ensure_market_session_refresh({"NIFTY": {}}, clock) is True
        assert expected in logs[-1][0]

    scanner._morning_refresh_inflight = False
    # Never attempted / no incomplete summary → no catch-up, market closed → False
    assert scanner._ensure_market_session_refresh({"NIFTY": {}}, datetime(2026, 6, 16, 9, 15, 0)) is False


def test_dashboard_quote_session_uses_premarket_schedule_for_index_cards(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _premarket_clock_settings)
    scanner = _scanner_for_morning_refresh(market_open=False)
    quote = {"quote_label": "CLOSED", "session": "CLOSED", "is_preopen": False}

    label, session, is_preopen, is_settlement = scanner._dashboard_quote_session(
        quote,
        datetime(2026, 6, 16, 9, 5, 0),
    )
    assert (label, session, is_preopen, is_settlement) == (
        "PRE-OPEN",
        "PRE-OPEN",
        True,
        True,
    )

    label, session, is_preopen, is_settlement = scanner._dashboard_quote_session(
        quote,
        datetime(2026, 6, 16, 9, 10, 0),
    )
    assert (label, session, is_preopen, is_settlement) == (
        "PRE-OPEN",
        "PRE-OPEN",
        True,
        False,
    )


def test_dashboard_quote_session_keeps_closed_outside_premarket(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _premarket_clock_settings)
    scanner = _scanner_for_morning_refresh(market_open=False)

    label, session, is_preopen, is_settlement = scanner._dashboard_quote_session(
        {"quote_label": "CLOSED", "session": "CLOSED"},
        datetime(2026, 6, 16, 8, 59, 59),
    )

    assert (label, session, is_preopen, is_settlement) == (
        "CLOSED",
        "CLOSED",
        False,
        False,
    )


def test_real_mode_market_open_refresh_still_schedules_at_live_open(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _premarket_clock_settings)
    scanner = _scanner_for_morning_refresh(market_open=True)
    scheduled = _capture_schedule(scanner)

    result = scanner._ensure_market_session_refresh(
        {"NIFTY": {}},
        datetime(2026, 6, 16, 9, 15, 0),
    )

    assert result is True
    assert len(scheduled) == 1
    assert scheduled[0]["force_full"] is True
    assert scheduled[0]["reason"] == "live_session"


def _scanner_with_correlation_exposures(exposures):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner.trades = SimpleNamespace(open_trades={str(i): item for i, item in enumerate(exposures)})
    scanner.active_indices = []
    scanner.latest_regimes = {}
    return scanner


def _exposure(instrument, grade="A", confidence=0.80, timeframe="15min", inst_type="FUT"):
    return SimpleNamespace(
        instrument=instrument,
        direction="LONG",
        timeframe=timeframe,
        inst_type=inst_type,
        grade=grade,
        confidence=confidence,
    )


def _two_core_exposures(instrument, grade="A", confidence=0.80):
    return [
        _exposure(instrument, grade, confidence, timeframe="5min", inst_type="FUT"),
        _exposure(instrument, grade, confidence, timeframe="15min", inst_type="OPT"),
    ]


def test_correlated_core_guard_blocks_beyond_core_bucket_cap(monkeypatch):
    # Same-direction CORE limit tracks the normal CORE bucket cap (6 = 3 indices x 2).
    settings = SimpleNamespace(
        ut_correlated_index_guard=True,
        ut_concurrency_guard=True,
        core_bucket_max_positions_guarded=6,
        midcap_bucket_max_positions_guarded=2,
    )
    monkeypatch.setattr("scanner.get_settings", lambda: settings)
    five = _scanner_with_correlation_exposures(
        _two_core_exposures("NIFTY") + _two_core_exposures("BANKNIFTY") + [_exposure("SENSEX")]
    )
    assert five._passes_correlated_index_guard(_exposure("SENSEX"), "LONG") is True

    six = _scanner_with_correlation_exposures(
        _two_core_exposures("NIFTY") + _two_core_exposures("BANKNIFTY") + _two_core_exposures("SENSEX")
    )
    assert six._passes_correlated_index_guard(_exposure("NIFTY"), "LONG") is False


def test_correlated_core_guard_no_plain_aplus_overflow_and_midcap_is_independent(monkeypatch):
    settings = SimpleNamespace(
        ut_correlated_index_guard=True,
        ut_concurrency_guard=True,
        core_bucket_max_positions_guarded=6,
        midcap_bucket_max_positions_guarded=2,
    )
    monkeypatch.setattr("scanner.get_settings", lambda: settings)
    scanner = _scanner_with_correlation_exposures(
        _two_core_exposures("NIFTY", "A+", 0.95)
        + _two_core_exposures("BANKNIFTY", "A+", 0.95)
        + _two_core_exposures("SENSEX", "A+", 0.95)
    )

    assert scanner._passes_correlated_index_guard(_exposure("NIFTY", "A+", 0.95), "LONG") is False
    assert scanner._passes_correlated_index_guard(_exposure("MIDCPNIFTY"), "LONG") is True


def test_futures_shadow_gate_defaults_off_even_for_historical_auto(monkeypatch):
    monkeypatch.setattr(
        "engine.signal_processor.get_settings",
        lambda: SimpleNamespace(futures_shadow_enabled=False),
    )
    scanner = SimpleNamespace(mode="HISTORICAL", inst_pref="AUTO")

    assert SignalProcessor._hybrid_backtest_dual_futures_enabled(scanner) is False


def test_futures_shadow_gate_requires_setting_historical_and_auto_or_hybrid(monkeypatch):
    monkeypatch.setattr(
        "engine.signal_processor.get_settings",
        lambda: SimpleNamespace(futures_shadow_enabled=True),
    )

    assert SignalProcessor._hybrid_backtest_dual_futures_enabled(
        SimpleNamespace(mode="HISTORICAL", inst_pref="AUTO")
    ) is True
    assert SignalProcessor._hybrid_backtest_dual_futures_enabled(
        SimpleNamespace(mode="HISTORICAL", inst_pref="HYBRID")
    ) is True
    assert SignalProcessor._hybrid_backtest_dual_futures_enabled(
        SimpleNamespace(mode="REAL", inst_pref="AUTO")
    ) is False
    assert SignalProcessor._hybrid_backtest_dual_futures_enabled(
        SimpleNamespace(mode="HISTORICAL", inst_pref="OPT")
    ) is False
