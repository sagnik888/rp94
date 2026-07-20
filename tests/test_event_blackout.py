"""Macro-event blackout calendar: parsing, window logic, gate wiring, default OFF."""
import json
from datetime import datetime

from data.market_calendar import load_event_blackout_windows, active_event_blackout


def _write(tmp_path, rows):
    p = tmp_path / "event_blackouts.json"
    p.write_text(json.dumps(rows), encoding="utf-8")
    return p


def test_load_normalizes_and_drops_invalid_rows(tmp_path):
    p = _write(tmp_path, [
        {"date": "2026-08-06", "start": "09:45", "end": "10:30", "label": "RBI policy"},
        {"date": "bad-date", "start": "09:45", "end": "10:30"},          # dropped
        {"date": "2026-08-07", "start": "11:00", "end": "10:00"},        # end<=start dropped
        "not-a-dict",                                                     # dropped
    ])
    wins = load_event_blackout_windows(p)
    assert len(wins) == 1
    assert wins[0]["label"] == "RBI policy"


def test_active_window_matches_date_and_time(tmp_path):
    p = _write(tmp_path, [{"date": "2026-08-06", "start": "09:45", "end": "10:30", "label": "RBI policy"}])
    wins = load_event_blackout_windows(p)
    assert active_event_blackout(datetime(2026, 8, 6, 10, 0), wins) == "RBI policy"
    assert active_event_blackout(datetime(2026, 8, 6, 10, 31), wins) == ""   # after window
    assert active_event_blackout(datetime(2026, 8, 7, 10, 0), wins) == ""    # other day


def test_missing_file_returns_empty():
    assert load_event_blackout_windows("data_store/__absent_blackouts__.json") == []


def test_default_off_and_wired_into_live_gate():
    from config.settings import Settings
    assert Settings.model_fields["event_blackout_enabled"].default is False
    import inspect
    from scanner import Scanner
    src = inspect.getsource(Scanner._passes_live_trade_ready_gate)
    assert "event_blackout_enabled" in src
    assert "active_event_blackout" in src
    # entries only: the check lives in the entry gate, guarded by the setting
    assert src.index("event_blackout_enabled") < src.index("active_event_blackout")
