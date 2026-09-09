import ast
import asyncio
import inspect
import math
import threading
from datetime import datetime
from types import SimpleNamespace

import trading.trade_dashboard as trade_dashboard
from data.market_data import MarketDataProvider
from data.market_source_health import build_market_source_snapshot, build_source_health
from engine.candidate_normalization import normalize_loaded_exit_candidate
from engine.scanner_dashboard import ScannerTradePayloadBuilder
from engine.scanner_historical_results import HistoricalTradeResultBuilder
from engine.signal_processor import SignalProcessor
from engine.signal_quality_policy import passes_quality_gate
from engine.trade_row_adapter import reconcile_legacy_trade_row, trade_row_to_signal
from engine.scanner_recalculation import ScannerRecalculationMixin
from engine.scanner_telemetry import ScannerTelemetryMixin
from trading.trade_dashboard import build_dashboard_rows
from trading.trade_manager import IST, TradeManager
from scanner import Scanner


def test_scanner_owns_telemetry_and_recalculation_through_mixins():
    assert issubclass(Scanner, ScannerTelemetryMixin)
    assert issubclass(Scanner, ScannerRecalculationMixin)
    assert "_get_system_metrics_payload" not in Scanner.__dict__
    assert "_perform_full_recalculation_impl" not in Scanner.__dict__
    assert Scanner._get_system_metrics_payload is (
        ScannerTelemetryMixin._get_system_metrics_payload
    )
    assert Scanner._perform_full_recalculation_impl is (
        ScannerRecalculationMixin._perform_full_recalculation_impl
    )


def test_scanner_dict_literals_do_not_repeat_string_keys():
    tree = ast.parse(inspect.getsource(Scanner))
    duplicates = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        seen = {}
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                previous = seen.get(key.value)
                if previous is not None:
                    duplicates.append((key.value, previous, key.lineno))
                else:
                    seen[key.value] = key.lineno

    assert duplicates == []


def test_scanner_startup_uses_fast_one_day_window():
    scanner_source = inspect.getsource(Scanner.__init__)

    assert "Keep startup light" in scanner_source
    assert "self.backtest_days = 1" in scanner_source


def test_broker_capable_startup_never_inherits_persisted_auto_authority():
    assert Scanner._startup_auto_mode("REAL", True) is False
    assert Scanner._startup_auto_mode("PAPER", True) is False
    assert Scanner._startup_auto_mode("MANUAL", True) is False
    assert Scanner._startup_auto_mode("HISTORICAL", True) is True
    assert Scanner._startup_auto_mode("HISTORICAL", False) is False


def test_empty_session_ledger_persist_is_noop_when_db_rows_exist(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "scanner.db.save_session_signals",
        lambda session_day, rows: calls.append((session_day, rows)),
    )

    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._session_candidate_day = "2026-07-01"
    scanner._loaded_session_candidate_day = "2026-07-01"
    scanner._loaded_session_payload_count = 44
    scanner._session_trade_candidates = {}
    scanner._repair_same_side_superseded_session_rows = lambda: False
    scanner._get_current_session_day = lambda: "2026-07-01"

    Scanner._persist_session_trade_candidates(scanner)

    assert calls == []
    assert scanner._last_empty_session_persist_skip == "2026-07-01|2026-07-01|44"


def _session_persist_scanner(payload):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._session_candidate_day = "2026-09-03"
    scanner._session_trade_candidates = {"NIFTY": {"row": payload}}
    scanner._repair_same_side_superseded_session_rows = lambda: False
    scanner._normalize_loaded_exit_candidate = lambda _candidate: None
    scanner._serialize_trade_candidate = lambda candidate: dict(candidate)
    scanner._get_current_session_day = lambda: "2026-09-03"
    return scanner


def test_graceful_session_ledger_drain_persists_latest_coalesced_snapshot(monkeypatch):
    calls = []
    first_write_started = threading.Event()
    release_first_write = threading.Event()

    def save_session_signals(session_day, rows):
        calls.append((session_day, [dict(row) for row in rows]))
        if len(calls) == 1:
            first_write_started.set()
            assert release_first_write.wait(timeout=2.0)

    monkeypatch.setattr("scanner.db.save_session_signals", save_session_signals)
    scanner = _session_persist_scanner(
        {"id": "entry", "timestamp": "2026-09-03T09:30:00+05:30", "action": "ENTRY"}
    )

    async def exercise():
        Scanner._persist_session_trade_candidates(scanner)
        assert await asyncio.to_thread(first_write_started.wait, 2.0)
        scanner._session_trade_candidates["NIFTY"]["row"] = {
            "id": "exit",
            "timestamp": "2026-09-03T09:35:00+05:30",
            "action": "EXIT",
        }
        Scanner._persist_session_trade_candidates(scanner)
        release_first_write.set()
        await Scanner.flush_session_trade_candidates(scanner)

    asyncio.run(exercise())

    assert calls[-1] == (
        "2026-09-03",
        [{"id": "exit", "timestamp": "2026-09-03T09:35:00+05:30", "action": "EXIT"}],
    )
    assert scanner._session_persist_pending is None
    assert scanner._session_persist_task.done()


def test_graceful_session_ledger_drain_retries_once(monkeypatch):
    attempts = []

    def save_session_signals(session_day, rows):
        attempts.append((session_day, [dict(row) for row in rows]))
        if len(attempts) == 1:
            raise OSError("transient sqlite failure")

    monkeypatch.setattr("scanner.db.save_session_signals", save_session_signals)
    scanner = _session_persist_scanner(
        {"id": "exit", "timestamp": "2026-09-03T09:35:00+05:30", "action": "EXIT"}
    )

    asyncio.run(Scanner.flush_session_trade_candidates(scanner))

    assert len(attempts) == 2
    assert attempts[-1][1][0]["action"] == "EXIT"
    assert scanner._last_session_persist_error == ""


def test_scanner_stop_schedules_final_session_ledger_snapshot(monkeypatch):
    scanner = Scanner.__new__(Scanner)
    scanner.is_running = True
    calls = []
    scanner._save_warm_memory = lambda force=False: calls.append(("warm", force))
    scanner._persist_entry_shadow_book = lambda: calls.append(("shadow", True))
    scanner._persist_session_trade_candidates = lambda: calls.append(("session", True))
    scanner.generate_daily_signal_report = lambda: calls.append(("report", True))

    Scanner.stop(scanner)

    assert calls == [
        ("warm", True),
        ("shadow", True),
        ("session", True),
        ("report", True),
    ]
    assert scanner.is_running is False


def test_pre_candle_live_tape_alerts_are_throttled(monkeypatch):
    now = {"value": 1000.0}
    monkeypatch.setattr("scanner.time.time", lambda: now["value"])
    logs = []

    scanner = Scanner.__new__(Scanner)
    scanner._pre_candle_prediction_last_log = {}
    scanner.log_event = lambda msg, level="info": logs.append((msg, level))

    base_prediction = {
        "state": "LIKELY_5M_FLIP",
        "direction": "LONG",
        "confidence": 0.65,
        "forming_start": "2026-07-02T10:05:00+05:30",
        "reason": "tick activity 323/min",
    }

    Scanner._log_pre_candle_prediction(scanner, "MIDCPNIFTY", "5min", base_prediction)
    Scanner._log_pre_candle_prediction(
        scanner,
        "MIDCPNIFTY",
        "5min",
        {**base_prediction, "confidence": 0.69, "reason": "tick activity 330/min"},
    )

    assert len(logs) == 1

    now["value"] += 61.0
    Scanner._log_pre_candle_prediction(
        scanner,
        "MIDCPNIFTY",
        "5min",
        {**base_prediction, "confidence": 0.70},
    )

    assert len(logs) == 2


def test_pre_candle_live_tape_alert_allows_direction_change(monkeypatch):
    monkeypatch.setattr("scanner.time.time", lambda: 1000.0)
    logs = []

    scanner = Scanner.__new__(Scanner)
    scanner._pre_candle_prediction_last_log = {}
    scanner.log_event = lambda msg, level="info": logs.append((msg, level))

    Scanner._log_pre_candle_prediction(
        scanner,
        "BANKNIFTY",
        "15min",
        {
            "state": "LIKELY_15M_FLIP",
            "direction": "SHORT",
            "confidence": 0.88,
            "forming_start": "2026-07-02T10:00:00+05:30",
            "reason": "OI agrees",
        },
    )
    Scanner._log_pre_candle_prediction(
        scanner,
        "BANKNIFTY",
        "15min",
        {
            "state": "LIKELY_15M_FLIP",
            "direction": "LONG",
            "confidence": 0.72,
            "forming_start": "2026-07-02T10:00:00+05:30",
            "reason": "order-flow agrees",
        },
    )

    assert len(logs) == 2
    assert "SHORT" in logs[0][0]
    assert "LONG" in logs[1][0]


def test_historical_scan_replays_indices_in_stable_order():
    scan_source = inspect.getsource(Scanner._scan_cycle)

    assert "Historical replay mutates shared scanner state" in scan_source
    assert "scan_results.append(await task)" in scan_source
    assert "scan_results = await asyncio.gather(*tasks)" in scan_source


def test_process_instrument_does_not_shadow_datetime():
    process_source = inspect.getsource(Scanner._process_instrument)

    assert "from datetime import datetime" not in process_source
    assert "datetime =" not in process_source


def test_historical_trade_reconstruction_lives_in_historical_builder():
    dashboard_source = inspect.getsource(ScannerTradePayloadBuilder.build)
    historical_source = inspect.getsource(HistoricalTradeResultBuilder.apply)

    assert "build_historical_trade_result" in dashboard_source
    assert "historical_session_plus_simulation" not in dashboard_source
    assert "historical_trade_manager" not in dashboard_source
    assert "historical_session_plus_simulation" in historical_source
    assert "historical_trade_manager" in historical_source


def test_real_signal_startup_does_not_run_historical_trade_reconstruction():
    source = inspect.getsource(SignalProcessor.process_best_signal)

    assert 'if (scanner.mode == "HISTORICAL" or is_live)' not in source
    assert 'if scanner.mode == "HISTORICAL":' in source
    assert "if is_live:" in source
    assert "naive_ts(sigs[-2].timestamp)" in source


def test_loaded_exit_candidate_normalization_lives_in_engine_helper():
    scanner_source = inspect.getsource(Scanner._normalize_loaded_exit_candidate)
    helper_source = inspect.getsource(normalize_loaded_exit_candidate)

    assert "normalize_loaded_exit_candidate" in scanner_source
    assert "reconciled from persisted exit" not in scanner_source
    assert "reconciled from persisted exit" in helper_source


def test_loaded_futures_candidate_is_repaired_to_regime_stop_cap():
    candidate = SimpleNamespace(
        price=20000.0,
        inst_type="FUT",
        direction="LONG",
        rr=2.0,
        stop=19960.0,
        target=20080.0,
        runner_mode=False,
        action="ENTRY",
        market_regime="RANGING",
    )

    changed = normalize_loaded_exit_candidate(
        candidate,
        futures_sl_pct=0.15,
        options_sl_pct=15.0,
        candidate_exit_resolution=lambda *_: (0.0, "", ""),
        candidate_net_pnl=lambda *_: 0.0,
    )

    assert changed is True
    assert candidate.stop == 19980.0
    assert candidate.target == 20040.0


def test_session_candidate_restore_preserves_advanced_telemetry():
    scanner = Scanner.__new__(Scanner)
    payload = {
        "instrument": "NIFTY",
        "direction": "LONG",
        "price": 100.0,
        "stop": 98.0,
        "target": 104.0,
        "lots": 1,
        "lot_size": 50,
        "grade": "A",
        "confidence": 0.72,
        "timeframe": "5min",
        "inst_type": "OPT",
        "option_type": "CE",
        "atm_strike": 25000,
        "multiplier": 0.5,
        "trading_symbol": "NIFTYTEST",
        "symbol_token": "123",
        "signal_timestamp": "2026-06-23T10:15:00",
        "market_regime": "TRENDING_UP",
        "intelligence_score": 0.44,
        "strategy_revision": "ut1-test",
        "strategy_direction_mode": "LONG_SHORT",
        "data_provenance": "LIVE",
        "price_action_verdict": "ADVERSE",
        "price_action_score": -0.55,
    }

    candidate = scanner._candidate_from_payload(payload)
    row = scanner._serialize_trade_candidate(candidate)

    assert candidate.market_regime == "TRENDING_UP"
    assert candidate.intelligence_score == 0.44
    assert candidate.price_action_verdict == "ADVERSE"
    assert row["strategy_revision"] == "ut1-test"
    assert row["strategy_direction_mode"] == "LONG_SHORT"
    assert row["data_provenance"] == "LIVE"
    assert row["price_action_score"] == -0.55


def test_legacy_trade_row_guards_live_in_adapter_module():
    dashboard_source = inspect.getsource(ScannerTradePayloadBuilder.build)
    historical_source = inspect.getsource(HistoricalTradeResultBuilder.apply)

    assert "reconcile_legacy_trade_row" in dashboard_source
    assert "Final accounting guard for legacy rows" not in dashboard_source
    assert "trade_row_to_signal" in historical_source
    assert callable(reconcile_legacy_trade_row)
    assert callable(trade_row_to_signal)


def test_reconcile_legacy_signal_row_prefers_accounting_stop_over_display_stop():
    row = reconcile_legacy_trade_row({
        "instrument": "NIFTY",
        "direction": "SHORT",
        "inst_type": "OPT",
        "entry_price": 182.30,
        "current_price": 169.539,
        "exit_price": 169.539,
        "stop": 169.539,
        "current_stop": 170.30,
        "lots": 1,
        "lot_size": 65,
        "exit_reason": "STOP_HIT",
    })

    assert row["exit_price"] == 170.30
    assert row["current_price"] == 170.30
    assert row["pnl"] == -860.0


def test_signal_quality_gate_lives_in_policy_module():
    processor_source = inspect.getsource(SignalProcessor._passes_quality_gate)
    quality_source = inspect.getsource(passes_quality_gate)

    assert "passes_quality_gate" in processor_source
    assert "ut_timeframe_entry_policy" not in processor_source
    assert "evaluate_quality" in quality_source
    assert "ut_timeframe_entry_policy" not in quality_source


def test_trade_dashboard_rows_live_in_serializer_module():
    manager_source = inspect.getsource(TradeManager.get_dashboard_payload)
    serializer_source = inspect.getsource(trade_dashboard)

    assert "build_dashboard_rows" in manager_source
    assert '"entry_timestamp"' not in manager_source
    assert '"entry_timestamp"' in serializer_source


def test_market_source_calculations_live_in_health_module():
    health_wrapper = inspect.getsource(MarketDataProvider.get_source_health)
    snapshot_wrapper = inspect.getsource(MarketDataProvider.get_market_source_snapshot)
    health_source = inspect.getsource(build_source_health)
    snapshot_source = inspect.getsource(build_market_source_snapshot)

    assert "build_source_health" in health_wrapper
    assert "freshness_threshold" not in health_wrapper
    assert "freshness_threshold" in health_source
    assert "build_market_source_snapshot" in snapshot_wrapper
    assert "entry_eligible" not in snapshot_wrapper
    assert "entry_eligible" in snapshot_source


def test_signal_processor_option_entries_use_scanner_premium_resolver():
    source = inspect.getsource(SignalProcessor.process_best_signal)

    assert "_get_live_premium_cached" in source
    assert "quote_probe" in source


def test_live_dashboard_excludes_unexecuted_terminal_session_signals_from_trades():
    session_rows = [
        {
            "id": "entry-1",
            "lifecycle_id": "life-1",
            "action": "ENTRY",
            "inst_type": "OPT",
            "pnl": 50.0,
        },
        {
            "id": "exit-1",
            "lifecycle_id": "life-1",
            "action": "EXIT",
            "inst_type": "OPT",
            "pnl": 100.0,
            "exit_reason": "TARGET_HIT",
        },
        {
            "id": "entry-2",
            "lifecycle_id": "life-2",
            "action": "ENTRY",
            "inst_type": "OPT",
            "pnl": 25.0,
        },
    ]
    scanner = SimpleNamespace(
        mode="REAL",
        backtest_days=1,
        scan_count=1,
        simulation_id=1,
        inst_pref="AUTO",
        active_indices=["NIFTY"],
        _session_trade_candidates={"NIFTY": {}},
        _session_candidate_day="2026-06-15",
        trades=SimpleNamespace(
            get_dashboard_payload=lambda **kwargs: {
                "open": [],
                "closed": [],
                "signals": [],
                "summary": {},
                "analytics": {},
            }
        ),
        _setting_enabled=lambda value, default: default,
        _get_current_session_day=lambda: "2026-06-15",
        _loaded_session_is_current_day=lambda: True,
        _get_session_trade_candidates=lambda instrument: session_rows,
        _normalize_loaded_exit_candidate=lambda candidate: False,
        _serialize_trade_candidate=lambda candidate: dict(candidate),
    )

    payload = ScannerTradePayloadBuilder(scanner).build()

    assert {row["id"] for row in payload["signals"]} == {"entry-2"}
    assert payload["signals"][0]["execution_status"] == "SIGNAL_ONLY"
    assert payload["signals"][0]["execution_state"] == "NOT_PLACED"
    assert payload["signals"][0]["pnl"] == 0.0
    assert payload["meta"]["restored_signal_rows"] == 1
    assert payload["meta"]["unexecuted_terminal_signal_rows"] == 1
    assert payload["meta"]["source"] == "restored_session_signals"
    assert payload["summary"]["total_trades"] == 1
    assert payload["summary"]["daily_pnl"] == 0.0
    assert payload["summary"]["lane"] == "SIGNAL_TRACKER"
    assert payload["summary"]["pnl_label"] == "SIGNAL P&L"
    assert payload["summary"]["pnl_is_money"] is False
    assert payload["summary"]["is_signal_tracker"] is True
    assert payload["monetary"]["accounting_basis"]["is_live_money"] is False
    assert "no broker fill is implied" in payload["monetary"]["accounting_basis"]["description"]
    # Session-row contract: consumers reading only open/closed must still be able
    # to see the session's true row counts via explicit totals.
    assert payload["session_rows_total"] == 1
    assert payload["session_closed_total"] == 0
    assert payload["session_open_total"] == 1
    assert payload["meta"]["session_rows_total"] == 1
    assert payload["meta"]["session_closed_total"] == 0
    assert payload["meta"]["session_open_total"] == 1


def test_live_dashboard_hides_signal_tracker_copy_of_executed_lifecycle():
    tracked = {
        "id": "signal-exit",
        "lifecycle_id": "life-executed",
        "action": "EXIT",
        "inst_type": "OPT",
        "pnl": 1132.53,
        "exit_reason": "STOP_HIT",
    }
    manager_row = {
        "id": "T0013",
        "lifecycle_id": "life-executed",
        "status": "CLOSED",
        "broker_order_id": "broker-entry",
        "exit_order_id": "broker-exit",
        "instrument": "BANKNIFTY 57200 PE",
        "inst_type": "OPT",
        "entry_price": 575.75,
        "exit_price": 576.0,
        "lots": 1,
        "lot_size": 30,
        "pnl": -183.66,
        "entry_timestamp": IST.localize(datetime(2026, 6, 15, 10, 14)).timestamp(),
    }
    scanner = SimpleNamespace(
        mode="REAL", backtest_days=1, scan_count=1, simulation_id=1,
        inst_pref="AUTO", active_indices=["BANKNIFTY"],
        _session_trade_candidates={"BANKNIFTY": {}},
        _session_candidate_day="2026-06-15",
        trades=SimpleNamespace(get_dashboard_payload=lambda **kwargs: {
            "open": [], "closed": [manager_row], "signals": [],
            "summary": {}, "analytics": {},
        }),
        _setting_enabled=lambda value, default: default,
        _get_current_session_day=lambda: "2026-06-15",
        _loaded_session_is_current_day=lambda: True,
        _get_session_trade_candidates=lambda instrument: [tracked],
        _normalize_loaded_exit_candidate=lambda candidate: False,
        _serialize_trade_candidate=lambda candidate: dict(candidate),
    )

    payload = ScannerTradePayloadBuilder(scanner).build()

    assert payload["signals"] == []
    assert [row["id"] for row in payload["closed"]] == ["T0013"]
    assert payload["summary"]["total_trades"] == 1
    assert payload["summary"]["daily_pnl"] == -72.5


def test_historical_dashboard_quote_invalid_signal_is_not_accounted_as_loss():
    ts = "2026-06-29T10:00:00+05:30"
    entry = SimpleNamespace(
        id="entry-1",
        lifecycle_id="life-quote-invalid",
        instrument="MIDCPNIFTY",
        direction="SHORT",
        underlying_direction="SHORT",
        action="ENTRY",
        status="TRADE SIGNAL",
        price=806.85,
        current_price=806.85,
        pnl=-80.0,
        stop=758.44,
        target=0.0,
        lots=1,
        lot_size=120,
        grade="A+",
        confidence=0.96,
        timeframe="5min",
        inst_type="OPT",
        option_type="PE",
        atm_strike=14425,
        trading_symbol="MIDCPNIFTY14425PE",
        timestamp=ts,
        signal_timestamp=ts,
        exit_reason="",
    )
    exit_row = SimpleNamespace(
        **{
            **entry.__dict__,
            "id": "exit-1",
            "action": "EXIT",
            "status": "EXIT SIGNAL",
            "current_price": 305.0,
            "exit_price": 305.0,
            "pnl": -60302.0,
            "exit_reason": "QUOTE_INVALID",
            "exit_timestamp": "2026-06-29T10:28:35+05:30",
            "is_exit": True,
        }
    )
    scanner = SimpleNamespace(
        mode="HISTORICAL",
        backtest_days=1,
        scan_count=1,
        simulation_id=1,
        inst_pref="AUTO",
        _session_trade_candidates={"MIDCPNIFTY": {}},
        _latest_filtered_candidates={},
        trades=SimpleNamespace(
            get_dashboard_payload=lambda **kwargs: {
                "open": [],
                "closed": [],
                "signals": [],
                "summary": {},
                "analytics": {},
            }
        ),
        _setting_enabled=lambda value, default: default,
        _get_current_session_day=lambda: "2026-06-29",
        historical_session_cutoff_date=lambda: datetime(2026, 6, 29).date(),
        _get_session_trade_candidates=lambda instrument: [entry, exit_row],
        _normalize_loaded_exit_candidate=lambda candidate: False,
        _serialize_trade_candidate=lambda candidate: dict(candidate.__dict__),
    )

    payload = ScannerTradePayloadBuilder(scanner).build()

    assert payload["meta"]["source"] == "historical_session_plus_simulation"
    assert payload["signals"] == []
    assert payload["closed"][0]["exit_reason"] == "QUOTE_INVALID"
    assert payload["closed"][0]["pnl"] == 0.0
    assert payload["closed"][0]["raw_pnl"] == -60302.0
    assert payload["summary"]["total_trades"] == 1
    assert payload["summary"]["daily_pnl"] == 0.0
    assert payload["summary"]["opt_pnl"] == 0.0
    assert payload["summary"]["wins"] == 0
    assert payload["summary"]["losses"] == 0


def test_historical_session_payload_closed_matches_reconstructed_signals():
    ts = "2026-06-29T10:00:00+05:30"
    h_ts = datetime.fromisoformat(ts).timestamp()
    session_rows = [
        {
            "id": "SESSION|NIFTY|5min|ENTRY|LONG|2026-06-29T10:00:00",
            "lifecycle_id": "life-1",
            "instrument": "NIFTY",
            "timeframe": "5min",
            "direction": "LONG",
            "action": "ENTRY",
            "inst_type": "OPT",
            "price": 100.0,
            "current_price": 110.0,
            "lots": 1,
            "lot_size": 50,
            "timestamp": ts,
            "signal_timestamp": ts,
            "pnl": 0.0,
        },
        {
            "id": "SESSION|NIFTY|5min|EXIT|LONG|2026-06-29T10:00:00",
            "lifecycle_id": "life-1",
            "instrument": "NIFTY",
            "timeframe": "5min",
            "direction": "LONG",
            "action": "EXIT",
            "inst_type": "OPT",
            "price": 100.0,
            "current_price": 110.0,
            "exit_price": 110.0,
            "lots": 1,
            "lot_size": 50,
            "timestamp": ts,
            "signal_timestamp": ts,
            "exit_timestamp": "2026-06-29T10:05:00+05:30",
            "exit_reason": "TARGET_HIT",
            "pnl": 500.0,
        },
    ]
    raw_closed_rows = [
        {
            "id": "SESSION|NIFTY|5min|ENTRY|LONG|2026-06-29T10:00:00",
            "instrument": "NIFTY",
            "timeframe": "5min",
            "direction": "LONG",
            "inst_type": "OPT",
            "entry_timestamp": h_ts,
            "pnl": 500.0,
        },
        {
            "id": "H_NIFTY_5min_20260629100000",
            "instrument": "NIFTY",
            "timeframe": "5min",
            "direction": "LONG",
            "inst_type": "OPT",
            "entry_timestamp": h_ts,
            "pnl": 250.0,
        },
    ]
    scanner = SimpleNamespace(
        mode="HISTORICAL",
        backtest_days=1,
        scan_count=1,
        simulation_id=1,
        _historical_completed_simulation_id=1,
        _calculation_lock=False,
        is_calculating=False,
        _historical_rebuild_pending=False,
        _recalculation_status={"status": "idle"},
        inst_pref="AUTO",
        _session_trade_candidates={"NIFTY": {}},
        _latest_filtered_candidates={},
        trades=SimpleNamespace(
            get_dashboard_payload=lambda **kwargs: {
                "open": [],
                "closed": raw_closed_rows,
                "signals": [],
                "summary": {},
                "analytics": {},
            }
        ),
        _setting_enabled=lambda value, default: default,
        _get_current_session_day=lambda: "2026-06-29",
        historical_session_cutoff_date=lambda: datetime(2026, 6, 29).date(),
        _get_session_trade_candidates=lambda instrument: session_rows,
        _normalize_loaded_exit_candidate=lambda candidate: False,
        _serialize_trade_candidate=lambda candidate: dict(candidate),
    )

    payload = ScannerTradePayloadBuilder(scanner).build()

    assert payload["meta"]["source"] == "historical_session_plus_simulation"
    assert payload["meta"]["closed_rows"] == 1
    assert payload["meta"]["display_signal_rows"] == 0
    assert payload["meta"]["simulated_signal_rows"] == 0
    assert payload["summary"]["total_trades"] == 1
    assert len(payload["closed"]) == 1
    assert payload["signals"] == []
    assert payload["closed"][0]["id"].startswith("SESSION|")
    assert not any(str(row["id"]).startswith("H_") for row in payload["closed"])


def test_live_dashboard_refreshes_restored_signal_premium_before_serializing():
    session_rows = [
        SimpleNamespace(
            id="entry-1",
            lifecycle_id="life-1",
            action="ENTRY",
            inst_type="OPT",
            current_price=806.85,
            pnl=-80.0,
        )
    ]
    refresh_calls = []

    def refresh_trade_candidates(candidates, cfg):
        refresh_calls.append((list(candidates), cfg))
        candidates[0].current_price = 812.35
        candidates[0].pnl = 580.0

    scanner = SimpleNamespace(
        mode="REAL",
        backtest_days=1,
        scan_count=1,
        simulation_id=1,
        inst_pref="AUTO",
        active_indices=["MIDCPNIFTY"],
        config={"indices": {"MIDCPNIFTY": {"enabled": True}}},
        _session_trade_candidates={"MIDCPNIFTY": {}},
        _session_candidate_day="2026-06-29",
        trades=SimpleNamespace(
            get_dashboard_payload=lambda **kwargs: {
                "open": [],
                "closed": [],
                "signals": [],
                "summary": {},
                "analytics": {},
            }
        ),
        _setting_enabled=lambda value, default: default,
        _get_current_session_day=lambda: "2026-06-29",
        _loaded_session_is_current_day=lambda: True,
        _get_session_trade_candidates=lambda instrument: session_rows,
        _refresh_trade_candidates=refresh_trade_candidates,
        _normalize_loaded_exit_candidate=lambda candidate: False,
        _serialize_trade_candidate=lambda candidate: dict(candidate.__dict__),
    )

    payload = ScannerTradePayloadBuilder(scanner).build()

    assert refresh_calls
    assert refresh_calls[0][1] == {"enabled": True}
    assert payload["signals"][0]["current_price"] == 812.35
    assert payload["signals"][0]["pnl"] == 0.0
    assert payload["signals"][0]["execution_status"] == "SIGNAL_ONLY"


def test_diagnostics_active_peak_ignores_closed_lifecycle_entries():
    scanner = SimpleNamespace(
        _diagnostics={
            "rejects": {},
            "latency": {"last_ms": 0},
            "stabilization": {},
            "timeouts": {},
            "repaint_guard": {},
            "exit_reasons": {},
            "instrument_selection": {},
            "source_fallback": {},
            "simulation": {},
        },
        data=SimpleNamespace(
            get_source_health=lambda: {
                "angel": True,
                "angel_tick_age_seconds": 0.1,
                "fyers": True,
                "fyers_tick_age_seconds": 0.2,
            }
        ),
        candles=SimpleNamespace(get_candles=lambda *_args, **_kwargs: None),
        active_indices=["MIDCPNIFTY", "SENSEX"],
        mode="REAL",
        latest_results={},
        scan_count=1,
        _session_trade_candidates={
            "MIDCPNIFTY": {
                "entry": SimpleNamespace(
                    id="SESSION|MIDCPNIFTY|5min|ENTRY|SHORT|2026-06-29T10:00:00",
                    action="ENTRY",
                    instrument="MIDCPNIFTY",
                    timeframe="5min",
                    direction="SHORT",
                    signal_timestamp="2026-06-29T10:00:00",
                    pnl=-59204.0,
                    peak_pnl=0.0,
                ),
                "exit": SimpleNamespace(
                    id="SESSION|MIDCPNIFTY|5min|EXIT|SHORT|2026-06-29T10:00:00",
                    action="EXIT",
                    instrument="MIDCPNIFTY",
                    timeframe="5min",
                    direction="SHORT",
                    signal_timestamp="2026-06-29T10:00:00",
                    is_exit=True,
                    exit_reason="QUOTE_INVALID",
                    pnl=0.0,
                ),
            },
            "SENSEX": {
                "entry": SimpleNamespace(
                    id="SESSION|SENSEX|15min|ENTRY|SHORT|2026-06-29T11:15:00",
                    action="ENTRY",
                    instrument="SENSEX",
                    timeframe="15min",
                    direction="SHORT",
                    signal_timestamp="2026-06-29T11:15:00",
                    pnl=1200.0,
                    peak_pnl=1500.0,
                )
            },
        },
        _scanner_freshness_payload=lambda: {},
        _recalculation_status_payload=lambda: {},
        _worker_resources_payload=lambda: {},
        _get_system_metrics_payload=lambda: {},
        _system_performance_payload=lambda: {},
    )

    payload = ScannerTelemetryMixin._get_diagnostics_payload(scanner)

    assert "MIDCPNIFTY" not in payload["active_peak"]
    assert payload["active_peak"]["SENSEX"] == {"peak": 1500.0, "current": 1200.0}


def test_loaded_option_candidate_resubscribes_live_quote_token():
    calls = []
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.data = SimpleNamespace(
        subscribe_tokens=lambda tokens, exchange_type=1: calls.append((tokens, exchange_type))
    )
    candidate = SimpleNamespace(
        action="ENTRY",
        instrument="MIDCPNIFTY",
        inst_type="OPT",
        trading_symbol="MIDCPNIFTY28JUL2614425PE",
        symbol_token="63490",
    )

    scanner._resubscribe_loaded_candidate_quote(candidate)

    assert calls == [(["63490"], 2)]


def test_accepted_option_candidate_subscribes_live_quote_token():
    calls = []
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._session_trade_candidates = {}
    scanner._ensure_session_candidate_day = lambda: None
    scanner._persist_session_trade_candidates = lambda: None
    scanner.data = SimpleNamespace(
        subscribe_tokens=lambda tokens, exchange_type=1: calls.append((tokens, exchange_type))
    )
    candidate = SimpleNamespace(
        action="ENTRY",
        instrument="MIDCPNIFTY",
        timeframe="15min",
        direction="LONG",
        signal_timestamp=datetime(2026, 6, 29, 13, 30),
        inst_type="OPT",
        trading_symbol="MIDCPNIFTY28JUL2614325CE",
        symbol_token="63481",
        accepted_by_gate=True,
    )

    scanner._remember_trade_candidates([candidate])

    assert calls == [(["63481"], 2)]


def test_dashboard_payload_exposes_daily_and_yearly_sharpe_pair():
    closed_rows = [
        {
            "id": "closed-1",
            "instrument": "NIFTY",
            "timeframe": "5min",
            "direction": "LONG",
            "inst_type": "FUT",
            "entry_price": 100.0,
            "lots": 1,
            "lot_size": 1,
            "pnl": 100.0,
            "entry_timestamp": "2026-06-15T09:20:00+05:30",
            "exit_timestamp": "2026-06-15T09:25:00+05:30",
        },
        {
            "id": "closed-2",
            "instrument": "NIFTY",
            "timeframe": "5min",
            "direction": "LONG",
            "inst_type": "FUT",
            "entry_price": 100.0,
            "lots": 1,
            "lot_size": 1,
            "pnl": 200.0,
            "entry_timestamp": "2026-06-15T09:30:00+05:30",
            "exit_timestamp": "2026-06-15T09:35:00+05:30",
        },
        {
            "id": "closed-3",
            "instrument": "NIFTY",
            "timeframe": "5min",
            "direction": "SHORT",
            "inst_type": "FUT",
            "entry_price": 100.0,
            "lots": 1,
            "lot_size": 1,
            "pnl": -50.0,
            "entry_timestamp": "2026-06-15T09:40:00+05:30",
            "exit_timestamp": "2026-06-15T09:45:00+05:30",
        },
    ]
    scanner = SimpleNamespace(
        mode="REAL",
        backtest_days=1,
        scan_count=1,
        simulation_id=1,
        inst_pref="AUTO",
        active_indices=["NIFTY"],
        _session_trade_candidates={},
        _session_candidate_day="2026-06-15",
        trades=SimpleNamespace(
            get_dashboard_payload=lambda **kwargs: {
                "open": [],
                "closed": closed_rows,
                "signals": [],
                "summary": {},
                "analytics": {},
            }
        ),
        _setting_enabled=lambda value, default: default,
        _get_current_session_day=lambda: "2026-06-15",
        _loaded_session_is_current_day=lambda: True,
        _get_session_trade_candidates=lambda instrument: [],
        _normalize_loaded_exit_candidate=lambda candidate: False,
        _serialize_trade_candidate=lambda candidate: dict(candidate),
    )

    payload = ScannerTradePayloadBuilder(scanner).build()

    pnls = [row["pnl"] for row in closed_rows]
    avg = sum(pnls) / len(pnls)
    std = math.sqrt(sum((pnl - avg) ** 2 for pnl in pnls) / (len(pnls) - 1))
    expected_daily = avg / std
    expected_yearly = expected_daily * math.sqrt(252)
    summary = payload["summary"]
    headline = payload["monetary"]["headline"]

    assert math.isclose(summary["sharpe_ratio_daily"], expected_daily)
    assert math.isclose(summary["sharpe_ratio_yearly"], expected_yearly)
    assert summary["sharpe_ratio"] == summary["sharpe_ratio_daily"]
    assert summary["sharpe_ratio_display"] == f"{expected_daily:.2f} / {expected_yearly:.2f}"
    assert headline["sharpe_ratio_daily"] == round(expected_daily, 2)
    assert headline["sharpe_ratio_yearly"] == round(expected_yearly, 2)
    assert headline["sharpe_ratio_display"] == f"{expected_daily:.2f} / {expected_yearly:.2f}"
