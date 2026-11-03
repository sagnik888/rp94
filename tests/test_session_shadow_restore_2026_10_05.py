"""Pending executable evidence survives the normal replay-to-live startup."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import threading
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from scanner import Scanner
from trading.trade_manager import IST


def watch(key="accepted", *, age_minutes=10, emitted=()):
    return {"key": key, "lifecycle_id": key, "decision_group": "ACCEPTED",
            "instrument": "NIFTY", "inst_type": "OPT", "symbol_token": "token",
            "trading_symbol": "NIFTYTESTCE", "registered_at":
            (datetime.now(IST) - timedelta(minutes=age_minutes)).isoformat(),
            "horizons": [15, 30, 60], "emitted_horizons": list(emitted),
            "entry_executable_price": 100., "accepted_actual_fill_price": 0.}


def shell(tmp_path, monkeypatch, *, mode="REAL", maximum=10):
    from config.settings import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "entry_shadow_state_file", str(tmp_path / "pending.json"))
    monkeypatch.setattr(settings, "entry_shadow_outcomes_enabled", True)
    monkeypatch.setattr(settings, "entry_shadow_horizons_minutes", [15, 30, 60])
    monkeypatch.setattr(settings, "entry_shadow_max_watches", maximum)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = mode
    scanner._entry_shadow_lock = threading.RLock()
    scanner._entry_shadow_book = {}
    scanner._entry_shadow_last_persist = 0.
    scanner._entry_shadow_persist_requested = False
    scanner.subscriptions, scanner.outcomes = [], []
    scanner._subscribe_entry_shadow_watch = lambda row: scanner.subscriptions.append(row["key"])
    scanner._record_entry_shadow_outcome = lambda row, **data: scanner.outcomes.append((row["key"], data)) or True
    return scanner


def save(scanner, rows):
    scanner._entry_shadow_state_path().write_text(json.dumps({"watches": rows}), encoding="utf8")


def configure_shell(scanner, monkeypatch):
    from config.settings import get_settings
    settings = get_settings()
    monkeypatch.setattr(scanner_module, "validate_real_mode_readiness", lambda *a, **k:
                        {"ok": True, "blockers": [], "warnings": []})
    scanner.auto_mode = False
    scanner.on_notification = None
    scanner.active_indices = ["NIFTY"]
    scanner.inst_pref = "OPT"
    scanner.capital_total, scanner.capital_fut, scanner.capital_opt = 100000., 50000., 50000.
    scanner.risk_fut_pct, scanner.risk_opt_pct = 1., 1.
    scanner.futures_sl_pct, scanner.options_sl_pct, scanner.max_daily_loss_pct = .15, 15., 3.
    scanner.data = SimpleNamespace(enable_live_streams=lambda: None)
    broker = SimpleNamespace(execution_lease_established=lambda: True, acquire_execution_lease=lambda: True)
    scanner.trades = SimpleNamespace(real_broker=broker, broker=None, mode=scanner.mode,
                                    has_live_exposure=lambda: False, update_risk_settings=lambda *a: None)
    scanner.risk_manager = SimpleNamespace(update_settings=lambda **kw: None)
    scanner.perf = SimpleNamespace(reset=lambda: None)
    scanner.mtf = SimpleNamespace()
    scanner._cached_results = {}
    scanner.latest_results = {}
    scanner._purge_intel_regime_caches = lambda **kw: None
    scanner._purge_session_contamination = lambda **kw: None
    scanner._set_candle_history_capacity = lambda: None
    scanner._next_simulation_id = lambda: 1
    scanner._stamp_simulation_diagnostics = lambda: None
    scanner._load_session_trade_candidates = lambda: None
    scanner.get_broadcast_config = lambda: {}
    scanner._build_dashboard_trade_payload = lambda: {}
    scanner._update_dashboard_cache = lambda results: None
    scanner._publish_config_snapshot = lambda: None
    scanner._record_live_session_state = lambda **kw: None
    scanner.log_event = lambda *a: None
    return settings, broker


def test_replay_constructor_deferred_book_restores_after_real_mode_is_verified(tmp_path, monkeypatch):
    scanner = shell(tmp_path, monkeypatch, mode="HISTORICAL")
    save(scanner, [watch()])
    scanner._load_entry_shadow_book()
    assert scanner._entry_shadow_book == {}
    configure_shell(scanner, monkeypatch)
    assert scanner.configure(mode="REAL", confirm_real_mode=True, real_mode_verification="YES", persist_settings=False)
    assert scanner.mode == "REAL"
    assert "accepted" in scanner._entry_shadow_book
    assert scanner.subscriptions == ["accepted"]
    assert scanner.configure(mode="REAL", persist_settings=False)
    assert scanner.subscriptions == ["accepted"]


def test_refused_real_mode_does_not_load_live_shadow_evidence(tmp_path, monkeypatch):
    scanner = shell(tmp_path, monkeypatch, mode="HISTORICAL")
    save(scanner, [watch()])
    _, broker = configure_shell(scanner, monkeypatch)
    broker.acquire_execution_lease = lambda: False
    assert not scanner.configure(mode="REAL", confirm_real_mode=True, real_mode_verification="YES", persist_settings=False)
    assert scanner.mode == "HISTORICAL" and scanner._entry_shadow_book == {}


def test_restore_preserves_newer_fill_and_progress_without_duplicate_subscription(tmp_path, monkeypatch):
    scanner = shell(tmp_path, monkeypatch)
    disk = watch(emitted=[15])
    live = {**deepcopy(disk), "accepted_actual_fill_price": 103., "emitted_horizons": [15, 30], "entry_executable_price": 102.}
    scanner._entry_shadow_book["accepted"] = live
    save(scanner, [disk])
    scanner._load_entry_shadow_book()
    scanner._load_entry_shadow_book()
    assert scanner._entry_shadow_book["accepted"] is live
    assert scanner._entry_shadow_book["accepted"] == live
    assert not scanner.subscriptions


def test_existing_watch_is_not_expired_by_older_disk_copy(tmp_path, monkeypatch):
    scanner = shell(tmp_path, monkeypatch)
    live = watch()
    scanner._entry_shadow_book["accepted"] = live
    save(scanner, [watch(age_minutes=90)])
    scanner._load_entry_shadow_book()
    assert scanner._entry_shadow_book["accepted"] is live
    assert scanner.outcomes == []


def test_restore_counts_existing_watches_against_capacity(tmp_path, monkeypatch):
    scanner = shell(tmp_path, monkeypatch, maximum=2)
    scanner._entry_shadow_book["live"] = watch("live")
    save(scanner, [watch("disk1"), watch("disk2")])
    scanner._load_entry_shadow_book()
    assert set(scanner._entry_shadow_book) == {"live", "disk1"}
    assert [data["horizon_minutes"] for _, data in scanner.outcomes] == [15, 30, 60]
    assert all(data["unavailable_reason"] == "restart_watch_capacity_reached" for _, data in scanner.outcomes)


def test_expired_restore_records_only_missing_horizons_then_is_idempotent(tmp_path, monkeypatch):
    scanner = shell(tmp_path, monkeypatch)
    save(scanner, [watch(age_minutes=90, emitted=[15])])
    scanner._load_entry_shadow_book()
    scanner._load_entry_shadow_book()
    assert [data["horizon_minutes"] for _, data in scanner.outcomes] == [30, 60]
    assert all(data["unavailable_reason"] == "process_restart_missed_horizon" for _, data in scanner.outcomes)
    assert scanner._entry_shadow_book == {}


def test_completed_watch_is_not_resubscribed_or_reintroduced(tmp_path, monkeypatch):
    scanner = shell(tmp_path, monkeypatch)
    save(scanner, [watch(emitted=[15, 30, 60])])
    scanner._load_entry_shadow_book()
    assert scanner._entry_shadow_book == {} and scanner.subscriptions == []


@pytest.mark.parametrize("mode", ["HISTORICAL", "PAPER"])
def test_non_real_restore_never_changes_pending_file_or_book(mode, tmp_path, monkeypatch):
    scanner = shell(tmp_path, monkeypatch, mode=mode)
    save(scanner, [watch()])
    before = scanner._entry_shadow_state_path().read_bytes()
    scanner._load_entry_shadow_book()
    assert scanner._entry_shadow_state_path().read_bytes() == before
    assert scanner._entry_shadow_book == {}


def test_duplicate_rows_preserve_first_immutable_snapshot_and_subscribe_once(tmp_path, monkeypatch):
    scanner = shell(tmp_path, monkeypatch)
    first = watch()
    save(scanner, [first, {**first, "entry_executable_price": 200.}])
    scanner._load_entry_shadow_book()
    scanner._load_entry_shadow_book()
    assert scanner._entry_shadow_book["accepted"]["entry_executable_price"] == 100.
    assert scanner.subscriptions == ["accepted"]


def test_corrupt_empty_instrument_does_not_abort_restoring_remaining_valid_rows(tmp_path, monkeypatch):
    scanner = shell(tmp_path, monkeypatch)
    save(scanner, [{**watch("corrupt"), "instrument": ""}, watch("valid")])
    scanner._load_entry_shadow_book()
    assert set(scanner._entry_shadow_book) == {"valid"}
    assert scanner.subscriptions == ["valid"]
    persisted = json.loads(scanner._entry_shadow_state_path().read_text(encoding="utf8"))
    assert [row["key"] for row in persisted["watches"]] == ["valid"]
