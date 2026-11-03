"""Gap-open shadow rule (2026-10-09): records what prior-session structure cost, admits nothing."""
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from engine import gap_open_shadow
from engine.gap_open_shadow import evaluate, gap_state

SETTINGS = NS(gap_open_shadow_enabled=True, gap_open_window_minutes=5.0,
              gap_open_min_gap_pct=0.8, gap_open_min_aligned=3)
ADVERSE_DOWN = {"structure": "DOWN", "score": -0.62, "verdict": "ADVERSE", "adverse_threshold": -0.5}


def _frame(prev_close=100.0, gap_pct=1.0, retained=1.0):
    opened = prev_close * (1 + gap_pct / 100)
    last = prev_close + (opened - prev_close) * retained
    index = pd.DatetimeIndex(["2026-10-08 15:29", "2026-10-09 09:15", "2026-10-09 09:16"])
    return pd.DataFrame({"open": [prev_close, opened, last], "close": [prev_close, opened, last]}, index=index)


def _frames(*gaps, retained=1.0):
    names = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    return {name: _frame(gap_pct=g, retained=retained) for name, g in zip(names, gaps)}


def test_gap_state_measures_gap_and_whether_it_holds():
    held = gap_state(_frame(gap_pct=1.0, retained=0.8), "2026-10-09")
    assert held["gap_pct"] == pytest.approx(1.0) and held["direction"] == 1 and held["holding"]
    assert not gap_state(_frame(gap_pct=1.0, retained=0.3), "2026-10-09")["holding"]


def test_holding_gap_with_three_aligned_removes_the_structure_term():
    verdict = evaluate("NIFTY", "LONG", "2026-10-09 09:15", _frames(1.0, 0.9, 0.85, 0.1), SETTINGS, ADVERSE_DOWN)
    assert verdict["active"] and verdict["aligned_indices"] == ["BANKNIFTY", "NIFTY", "SENSEX"]
    assert verdict["shadow_score"] == pytest.approx(-0.22)
    assert verdict["shadow_verdict"] == "NEUTRAL" and verdict["would_change"]


@pytest.mark.parametrize("case", ["small_gap", "two_aligned", "late_bar", "filled_gap"])
def test_rule_stays_inactive_outside_its_conditions(case):
    frames = _frames(0.5, 0.5, 0.5, 0.5) if case == "small_gap" else _frames(1.0, 1.0, -1.0, 0.2)
    if case in {"late_bar", "filled_gap"}:
        frames = _frames(1.0, 1.0, 1.0, 1.0, retained=0.2 if case == "filled_gap" else 1.0)
    signal_time = "2026-10-09 09:20" if case == "late_bar" else "2026-10-09 09:15"
    verdict = evaluate("NIFTY", "LONG", signal_time, frames, SETTINGS, ADVERSE_DOWN)
    assert not verdict["active"] and not verdict["would_change"]


def test_trade_against_the_gap_is_recorded_but_never_would_change():
    verdict = evaluate("NIFTY", "SHORT", "2026-10-09 09:15", _frames(1.0, 1.0, 1.0, 1.0), SETTINGS,
                       {"structure": "UP", "score": -0.7, "verdict": "ADVERSE", "adverse_threshold": -0.5})
    assert verdict["active"] and not verdict["with_gap"] and not verdict["would_change"]


def test_record_stamps_logs_archives_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(gap_open_shadow, "LOG_PATH", tmp_path / "gap.jsonl")
    frames = _frames(1.0, 1.0, 1.0, 1.0)
    scanner = NS(mode="REAL", active_indices=list(frames),
                 candles=NS(get_candles=lambda name, tf: frames[name]))
    signal = NS(signal_type="BUY", timestamp=pd.Timestamp("2026-10-09 09:15"))
    pa = dict(ADVERSE_DOWN)
    verdict = gap_open_shadow.record(scanner, "NIFTY", "5min", signal, SETTINGS, pa)
    assert verdict["would_change"] and pa["gap_open_shadow"] is verdict
    assert (tmp_path / "gap.jsonl").read_text(encoding="utf-8").count("\n") == 1
    broken = NS(candles=None)
    assert gap_open_shadow.record(broken, "NIFTY", "5min", signal, SETTINGS, dict(ADVERSE_DOWN))["active"] is False
    off = NS(**{**vars(SETTINGS), "gap_open_shadow_enabled": False})
    assert gap_open_shadow.record(scanner, "NIFTY", "5min", signal, off, dict(ADVERSE_DOWN)) is None
