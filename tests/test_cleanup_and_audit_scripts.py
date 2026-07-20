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
