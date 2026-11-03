"""Trend-day sub-1R give-back gap is recorded in shadow, never exited (2026-10-09)."""
import json
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from config.settings import get_settings
from scanner import Scanner


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sc = Scanner.__new__(Scanner)
    sc._profit_giveback_arm = lambda risk, now=None, nontrend_scope=False, trade_like=None: 353.0
    return sc


def _row():
    return NS(instrument="BANKNIFTY", timeframe="5min", direction="LONG", source_lane="QP",
              signal_timestamp=datetime(2026, 10, 9, 11, 15), price=919.15)


def _record(sc, row, current):
    # BANKNIFTY 55100CE on 2026-10-09: peak Rs.464, 1R = Rs.1,176, giveback arm Rs.353.
    return sc._record_trend_gap_shadow(row, current_pnl_rs=current, peak_pnl=464.0, initial_risk_rs=1176.0,
                                       smart_min_peak=1176.0, latest_time=datetime(2026, 10, 9, 11, 21, 3),
                                       trend_runner_active=False)


def test_faded_trend_day_peak_is_recorded_once(scanner, tmp_path):
    row = _row()
    assert _record(scanner, row, 300.0) is None            # still keeps more than half
    record = _record(scanner, row, 174.0)
    assert record["pnl_at_trigger"] == 174.0 and record["scope"] == "trend_day"
    assert row.trend_gap_shadow is record
    assert _record(scanner, row, -451.0) is None           # once per trade
    lines = (tmp_path / "data_store" / "trend_gap_shadow.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["instrument"] == "BANKNIFTY"


def test_peaks_outside_the_gap_are_ignored(scanner):
    assert scanner._record_trend_gap_shadow(_row(), current_pnl_rs=50.0, peak_pnl=300.0, initial_risk_rs=1176.0,
                                            smart_min_peak=1176.0, latest_time=None,
                                            trend_runner_active=False) is None  # below the arm
    assert scanner._record_trend_gap_shadow(_row(), current_pnl_rs=100.0, peak_pnl=1300.0, initial_risk_rs=1176.0,
                                            smart_min_peak=1176.0, latest_time=None,
                                            trend_runner_active=True) is None   # the 1R lock owns it


def test_shadow_can_be_switched_off(scanner, monkeypatch):
    monkeypatch.setattr(get_settings(), "trend_gap_shadow_enabled", False)
    assert _record(scanner, _row(), 174.0) is None
