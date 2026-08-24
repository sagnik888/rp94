from datetime import datetime
from types import SimpleNamespace

import pytz

from scanner import Scanner


IST = pytz.timezone("Asia/Kolkata")


def _scanner(summary):
    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    scanner._morning_refresh_summary = summary
    return scanner


def _settings(**overrides):
    values = {
        "morning_refresh_require_history": True,
        "morning_refresh_require_expiry": True,
        "morning_refresh_require_all_quotes": True,
        "morning_refresh_require_all_chains": True,
        "morning_refresh_require_vix": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_auto_readiness_rejects_missing_or_previous_day_summary(monkeypatch):
    import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "get_settings", _settings)
    now = IST.localize(datetime(2026, 8, 21, 9, 16))
    missing = _scanner({})
    prior = _scanner({"session_day": "2026-08-20", "complete": True})

    assert "unavailable" in missing._auto_execution_morning_readiness_blockers(now)[0]
    assert "unavailable" in prior._auto_execution_morning_readiness_blockers(now)[0]


def test_auto_readiness_requires_all_four_quotes_and_chains(monkeypatch):
    import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "get_settings", _settings)
    now = IST.localize(datetime(2026, 8, 21, 9, 16))
    summary = {
        "session_day": "2026-08-21",
        "history": True,
        "expiry": True,
        "index_quotes": 4,
        "chains": 3,
        "indices": {
            name: {"quote": True, "chain": name != "MIDCPNIFTY"}
            for name in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
        },
        "complete": False,
    }

    blocker = _scanner(summary)._auto_execution_morning_readiness_blockers(now)[0]

    assert "incomplete" in blocker
    assert "chains" in blocker
    assert "chain:MIDCPNIFTY" in blocker


def test_auto_readiness_accepts_complete_current_four_index_summary(monkeypatch):
    import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "get_settings", _settings)
    now = IST.localize(datetime(2026, 8, 21, 9, 16))
    summary = {
        "session_day": "2026-08-21",
        "history": True,
        "expiry": True,
        "index_quotes": 4,
        "chains": 4,
        "indices": {
            name: {"quote": True, "chain": True}
            for name in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
        },
        "complete": True,
    }

    assert _scanner(summary)._auto_execution_morning_readiness_blockers(now) == []
