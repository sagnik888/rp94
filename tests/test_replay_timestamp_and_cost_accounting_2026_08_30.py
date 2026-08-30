"""Regressions for archived lifecycle replays with mixed timestamp formats.

The archive is immutable historical evidence.  Its older rows use naïve IST while
newer producers wrote ``+05:30`` or UTC ``Z`` values; both replay tools must interpret
those as the same market clock without changing the represented instant.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bt = _load_module("bt_exit_mixed_tz_regression", ROOT / "scripts" / "backtest_exit_lifecycle.py")
stop_bt = _load_module("bt_stop_mixed_tz_regression", ROOT / "scripts" / "backtest_stop_out_reentry.py")


def _write_mixed_contract(root: Path, day: str) -> None:
    candle_dir = root / "data_store" / "session_archive" / day / "candles"
    candle_dir.mkdir(parents=True)
    rows = [
        (f"{day}T09:29:00", 100.0, 101.0, 99.0, 100.0, 10),
        (f"{day}T04:00:00Z", 100.0, 102.0, 99.0, 101.0, 20),
        (f"{day}T09:31:00+05:30", 101.0, 103.0, 100.0, 102.0, 30),
        (f"{day} 09:32:00", 102.0, 103.0, 101.0, 102.0, 40),
        (f"{day}T04:03:00Z", 102.0, 104.0, 101.0, 103.0, 50),
        (f"{day}T09:34:00+05:30", 103.0, 104.0, 102.0, 103.0, 60),
        (f"{day} 09:35:00", 103.0, 104.0, 102.0, 103.0, 70),
        (f"{day}T04:06:00Z", 103.0, 105.0, 102.0, 104.0, 80),
        (f"{day}T09:37:00+05:30", 104.0, 105.0, 103.0, 104.0, 90),
    ]
    pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    ).to_csv(candle_dir / "NIFTY31JUL2624000CE_1min.csv", index=False)


def _write_events(root: Path, day: str, rows: list[dict]) -> None:
    event_dir = root / "data_store" / "rtracker" / "events"
    event_dir.mkdir(parents=True)
    (event_dir / f"{day}.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_market_timestamp_normalization_preserves_the_market_minute():
    values = pd.Series([
        "2026-07-24T09:30:00",
        "2026-07-24T09:30:00+05:30",
        "2026-07-24T04:00:00Z",
    ])

    normalized = bt._market_timestamp_series(values)

    assert str(normalized.dtype) == "datetime64[ns]"
    assert normalized.tolist() == [pd.Timestamp("2026-07-24 09:30:00")] * 3
    assert all(value.tzinfo is None for value in normalized)


def test_exit_lifecycle_run_day_accepts_one_mixed_format_archive(tmp_path, monkeypatch):
    day = "2026-07-24"
    _write_mixed_contract(tmp_path, day)
    _write_events(tmp_path, day, [{
        "decision": "ACCEPTED",
        "instrument": "NIFTY",
        "direction": "LONG",
        "timeframe": "5min",
        "price": 101.0,
        "stop": 91.0,
        "target": 121.0,
        "signal_timestamp": f"{day}T04:00:00Z",
        "regime": {"raw_regime": "TRENDING_UP", "adx": 30.0},
    }])
    monkeypatch.setattr(bt, "ROOT", tmp_path)
    monkeypatch.setattr(bt, "CANDLES", tmp_path / "data_store" / "candles")

    result = bt.run_day(day, quiet=True)

    assert result["trades"] == 1
    assert result["matched"] == 1
    assert result["old"] == pytest.approx(result["old_gross"] - result["old_costs"])
    assert result["runner"] == pytest.approx(
        result["runner_gross"] - result["runner_costs"]
    )


def test_exit_replay_reads_runtime_runner_parameters(monkeypatch):
    monkeypatch.setattr(
        bt,
        "get_settings",
        lambda: SimpleNamespace(
            dynamic_rr_runner_unlock_ratio=0.73,
            dynamic_rr_runner_lock_pct=0.67,
        ),
    )

    assert bt._runner_execution_parameters() == pytest.approx((0.73, 0.67))


def test_stop_out_replay_completes_mixed_event_and_candle_formats(tmp_path, monkeypatch, capsys):
    day = "2026-07-24"
    _write_mixed_contract(tmp_path, day)
    _write_events(tmp_path, day, [
        {
            "decision": "ACCEPTED",
            "instrument": "NIFTY",
            "direction": "LONG",
            "signal_timestamp": f"{day}T04:00:00Z",
            "regime": {"raw_regime": "TRENDING_UP", "adx": 30.0},
        },
        {
            "decision": "EXIT",
            "instrument": "NIFTY",
            "direction": "LONG",
            "exit_reason": "STOP_HIT",
            "exit_timestamp": f"{day}T09:30:00+05:30",
            "price": 101.0,
            "stop": 91.0,
            "pnl": -650.0,
            "regime": {"adx": 30.0},
        },
    ])
    monkeypatch.setattr(stop_bt, "ROOT", tmp_path)
    monkeypatch.setattr(stop_bt.bt, "ROOT", tmp_path)
    monkeypatch.setattr(stop_bt.bt, "CANDLES", tmp_path / "data_store" / "candles")
    monkeypatch.setattr(stop_bt, "get_runtime_settings", lambda: SimpleNamespace(
        opt_cost=80.0,
        slippage_opt_pct=0.30,
        slippage_auto_calibration_enabled=False,
    ))
    monkeypatch.setattr(sys, "argv", ["backtest_stop_out_reentry.py"])
    original_scanner_settings = stop_bt.scanner_module.get_settings
    try:
        stop_bt.main()
    finally:
        stop_bt.scanner_module.get_settings = original_scanner_settings

    output = capsys.readouterr().out
    assert "RE-ENTRIES taken: 1" in output
    assert "MODELED COSTS/SLIPPAGE" in output
    assert "TOTAL NET P&L" in output


def test_stop_out_replay_uses_shared_round_trip_cost_and_reports_net():
    settings = SimpleNamespace(
        opt_cost=80.0,
        slippage_opt_pct=0.30,
        slippage_auto_calibration_enabled=False,
    )

    gross, costs, net = stop_bt._option_replay_pnl(
        100.0, 110.0, 65, "NIFTY", settings,
    )

    assert gross == 650.0
    assert costs == pytest.approx(120.95)
    assert net == pytest.approx(529.05)
