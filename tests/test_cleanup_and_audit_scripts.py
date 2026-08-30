"""Verify dead-code removal + modern audit scripts stay importable/usable offline."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_scanner_has_no_orphan_process_best_signal():
    text = Path("scanner.py").read_text(encoding="utf-8")
    assert "def _process_best_signal" not in text
    assert "self.signal_processor.process_best_signal" in text


def test_audit_session_ledger_offline_json_and_hygiene():
    # Load module by path (scripts/ is not a package)
    path = Path("scripts/audit_session_ledger.py")
    spec = importlib.util.spec_from_file_location("audit_session_ledger", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    rows = [
        {
            "id": "1",
            "instrument": "NIFTY",
            "timeframe": "5min",
            "direction": "LONG",
            "inst_type": "OPT",
            "trading_symbol": "NIFTYCE",
            "pnl": 1000,
            "status": "CLOSED",
            "entry_time": "2026-07-09T10:00:00",
        },
        {
            "id": "1_FUT",
            "instrument": "NIFTY",
            "timeframe": "5min",
            "direction": "LONG",
            "inst_type": "FUT",
            "trading_symbol": "FUT_SHADOW",
            "pnl": 500,
            "status": "CLOSED",
            "grade": "A (Hist, Fut Shadow)",
            "entry_time": "2026-07-09T10:00:00",
        },
    ]
    report = mod.audit_trade_rows(rows)
    assert report["closed_headline"] == 1
    assert report["shadow_rows"] == 1
    assert report["headline_pnl"] == 1000
    assert report["pnl_inflation_if_raw"] == 500


def test_audit_session_ledger_separates_replays_from_session_certification():
    path = Path("scripts/audit_session_ledger.py")
    spec = importlib.util.spec_from_file_location("audit_session_ledger_scopes", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    session = {
        "id": "SESSION|NIFTY|5min|ENTRY|LONG|2026-08-19T10:00:00",
        "instrument": "NIFTY 24400 CE",
        "timeframe": "5min",
        "direction": "LONG",
        "inst_type": "OPT",
        "trading_symbol": "NIFTY24400CE",
        "entry_price": 100.0,
        "exit_price": 110.0,
        "pnl": 570.0,
        "status": "CLOSED",
        "entry_timestamp": 1787104800.0,
    }
    replay = {
        **session,
        "data_provenance": "BACKTEST",
        "entry_timestamp": 1787105100.0,
        "pnl": 200.0,
    }
    rows = [
        session,
        {**replay, "id": "H_NIFTY_5min_EMA_20260819100500"},
        {**replay, "id": "H_NIFTY_5min_CONSENSUS_20260819100500"},
    ]

    report = mod.audit_trade_rows(rows)

    assert report["ok"] is True
    assert report["session_ledger"] == {
        "rows": 1,
        "open": 0,
        "closed": 1,
        "other_status": 0,
        "pnl": 570.0,
    }
    assert report["historical_replay"]["stream_variant_rows"] == 1
    assert report["historical_replay"]["raw_pnl_difference"] == 200.0
    assert report["historical_replay"]["certification_blocking"] is False
    assert report["hygiene"]["engine_replay"]["stream_variant_rows"] == 1


def test_audit_does_not_compare_breakeven_rule_exit_to_stale_price_stop():
    path = Path("scripts/audit_session_ledger.py")
    spec = importlib.util.spec_from_file_location("audit_session_ledger_breakeven", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    row = {
        "id": "SESSION|MIDCPNIFTY|5min|ENTRY|LONG|2026-08-19T11:15:00",
        "instrument": "MIDCPNIFTY 14850 CE",
        "timeframe": "5min",
        "direction": "LONG",
        "inst_type": "OPT",
        "entry_price": 108.55,
        "current_stop": 99.87,
        "trailing_stop": 99.87,
        "exit_price": 109.05,
        "exit_reason": "BREAKEVEN_STOP",
        "pnl": -20.0,
        "status": "CLOSED",
    }

    report = mod.audit_trade_rows([row])

    assert report["stop_target_clamp_mismatches"] == 0
    assert report["ok"] is True


def test_audit_compares_option_exit_levels_in_premium_space():
    path = Path("scripts/audit_session_ledger.py")
    spec = importlib.util.spec_from_file_location("audit_session_ledger_domains", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    put_target = {
        "id": "H_NIFTY_5min_UTBOT_20260819113000",
        "instrument": "NIFTY 24350 PE",
        "timeframe": "5min",
        "direction": "SHORT",
        "inst_type": "OPT",
        "data_provenance": "BACKTEST",
        "entry_price": 98.4,
        "target": 118.5,
        "spot_target": 24290.0,
        "exit_price": 120.0,
        "exit_reason": "TARGET_HIT",
        "pnl": 1200.0,
        "status": "CLOSED",
    }

    report = mod.audit_trade_rows([put_target])

    # SHORT is the underlying thesis. The PE itself is a long premium contract,
    # and its option exit must be compared with target=118.5, never spot=24290.
    assert report["stop_target_clamp_mismatches"] == 0
    assert report["ok"] is True


def test_audit_still_blocks_a_true_deterministic_level_mismatch():
    path = Path("scripts/audit_session_ledger.py")
    spec = importlib.util.spec_from_file_location("audit_session_ledger_bad_level", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    bad_replay = {
        "id": "H_NIFTY_5min_UTBOT_20260819113000",
        "instrument": "NIFTY 24350 CE",
        "timeframe": "5min",
        "direction": "LONG",
        "inst_type": "OPT",
        "data_provenance": "BACKTEST",
        "entry_price": 100.0,
        "current_stop": 90.0,
        "exit_price": 100.0,
        "exit_reason": "SL HIT",
        "pnl": -80.0,
        "status": "CLOSED",
    }

    report = mod.audit_trade_rows([bad_replay])

    assert report["stop_target_clamp_mismatches"] == 1
    assert report["certification_failures"] == [
        "deterministic_exit_level_mismatches"
    ]
    assert report["ok"] is False


def test_audit_does_not_call_a_live_fill_a_clamp_mismatch_without_trigger_tape():
    path = Path("scripts/audit_session_ledger.py")
    spec = importlib.util.spec_from_file_location("audit_session_ledger_live_fill", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    live_fill = {
        "id": "T001_NIFTY_08301000",
        "instrument": "NIFTY 24350 CE",
        "timeframe": "5min",
        "direction": "LONG",
        "inst_type": "OPT",
        "data_provenance": "LIVE",
        "entry_price": 100.0,
        "protective_trigger": 90.0,
        "exit_price": 91.0,
        "exit_reason": "STOP_HIT",
        "pnl": -665.0,
        "status": "CLOSED",
    }

    report = mod.audit_trade_rows([live_fill])

    assert report["stop_target_clamp_mismatches"] == 0
    assert report["live_level_exits_unverifiable"] == 1
    assert report["ok"] is True


def test_audit_backtest_windows_module_loads():
    path = Path("scripts/audit_backtest_windows.py")
    spec = importlib.util.spec_from_file_location("audit_backtest_windows", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    assert hasattr(mod, "collect_result")
    assert hasattr(mod, "configure_days")
    assert mod.WS_MAX_SIZE >= 1_000_000


@pytest.mark.asyncio
async def test_audit_backtest_windows_restores_original_days(monkeypatch):
    path = Path("scripts/audit_backtest_windows.py")
    spec = importlib.util.spec_from_file_location("audit_backtest_windows_restore", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    configured = []
    monkeypatch.setattr(mod, "fetch_client_config", lambda base, session: {"dashboard_token": "token"})
    monkeypatch.setattr(mod, "get_json", lambda base, path, session, token: {"config": {"backtest_days": 3}})

    async def fake_configure(base, token, days):
        configured.append(days)

    monkeypatch.setattr(mod, "configure_days", fake_configure)
    monkeypatch.setattr(mod, "collect_result", lambda base, session, token, days, timeout: {"days": days})
    args = SimpleNamespace(base_url="http://127.0.0.1:8001", days=[5, 14], timeout=1)
    assert await mod.run_audit(args) == [{"days": 5}, {"days": 14}]
    assert configured == [5, 14, 3]


def test_pytest_ini_ignores_tmp_base():
    ini = Path("pytest.ini").read_text(encoding="utf-8")
    assert "pytest-tmp-base" in ini


def test_cleanup_script_never_kills_python_or_crosses_shells():
    text = Path("scripts/cleanup_pytest_tmp_base.ps1").read_text(encoding="utf-8")
    assert "Stop-Process" not in text
    assert "cmd /c" not in text
    assert "StartsWith($rootPrefix" in text
