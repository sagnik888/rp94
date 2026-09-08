from datetime import datetime
from types import SimpleNamespace

import scanner as scanner_module
from scanner import Scanner


def _settings(enabled=True):
    return SimpleNamespace(
        auto_real_mode_startup_enabled=enabled,
        real_mode_live_start="09:15:00",
        real_mode_live_end="15:30:00",
    )


def _scanner(summary_complete=True):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = False
    scanner._configured_startup_auto_armed_date = None
    scanner._configured_startup_auto_last_attempt_at = 0.0
    scanner._morning_refresh_summary = {
        "session_day": "2026-09-08",
        "complete": summary_complete,
    }
    scanner._morning_refresh_last_date = "2026-09-08" if summary_complete else None
    scanner.log_event = lambda *_args, **_kwargs: None
    scanner._last_configure_block_reason = ""
    return scanner


def test_real_startup_auto_waits_for_live_start_and_complete_warmup(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings(True))
    scanner = _scanner(True)
    calls = []

    def configure(**kwargs):
        calls.append(kwargs)
        scanner.auto_mode = True
        return True

    scanner.configure = configure
    scanner._maybe_arm_configured_startup_auto(
        "2026-09-08", datetime(2026, 9, 8, 9, 14, 59)
    )
    assert calls == []

    scanner._maybe_arm_configured_startup_auto(
        "2026-09-08", datetime(2026, 9, 8, 9, 15, 0)
    )
    assert calls == [
        {
            "auto_mode": True,
            "confirm_real_mode": True,
            "real_mode_verification": "YES",
            "persist_settings": False,
        }
    ]
    assert scanner._configured_startup_auto_armed_date == "2026-09-08"


def test_real_startup_auto_never_arms_without_explicit_opt_in_or_warmup(monkeypatch):
    scanner = _scanner(False)
    scanner.configure = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not arm"))
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings(True))
    scanner._maybe_arm_configured_startup_auto(
        "2026-09-08", datetime(2026, 9, 8, 9, 16, 0)
    )

    scanner = _scanner(True)
    scanner.configure = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not arm"))
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings(False))
    scanner._maybe_arm_configured_startup_auto(
        "2026-09-08", datetime(2026, 9, 8, 9, 16, 0)
    )
