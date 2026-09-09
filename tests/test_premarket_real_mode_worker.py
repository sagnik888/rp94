from datetime import datetime
from types import SimpleNamespace

import main


class _FakeScanner:
    def __init__(self, *, mode="HISTORICAL", auto_mode=True, trading_day=True):
        self.mode = mode
        self.auto_mode = auto_mode
        self.trading_day = trading_day
        self.calls = []
        self.events = []
        self._last_configure_block_reason = ""

    def _is_trading_day(self, _now):
        return self.trading_day

    def configure(self, **kwargs):
        self.calls.append(kwargs)
        if "auto_mode" in kwargs:
            self.auto_mode = bool(kwargs["auto_mode"])
        if kwargs.get("mode") == "REAL":
            if not kwargs.get("confirm_real_mode") or kwargs.get("real_mode_verification") != "YES":
                self._last_configure_block_reason = "missing confirmation"
                return False
            self.mode = "REAL"
        return True

    def log_event(self, message, event_type="info"):
        self.events.append((message, event_type))


def _settings(**overrides):
    data = {
        "auto_real_mode_premarket_enabled": True,
        "auto_real_mode_premarket_switch_time": "09:00:00",
        "auto_real_mode_premarket_retry_seconds": 30.0,
        "real_mode_live_end": "15:30:00",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _ist(ts: str):
    return main.IST.localize(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"))


def test_auto_real_worker_waits_before_premarket_switch_time():
    scanner = _FakeScanner(auto_mode=True)

    ok, reason = main._auto_real_mode_attempt(
        scanner,
        _settings(),
        _ist("2026-07-03 08:59:59"),
    )

    assert ok is False
    assert reason == "before_switch_time"
    assert scanner.mode == "HISTORICAL"
    assert scanner.calls == []


def test_auto_real_worker_switches_to_real_manual_after_9am():
    scanner = _FakeScanner(auto_mode=True)

    ok, reason = main._auto_real_mode_attempt(
        scanner,
        _settings(),
        _ist("2026-07-03 09:00:00"),
    )

    assert ok is True
    assert reason == "switched_real_manual"
    assert scanner.mode == "REAL"
    assert scanner.auto_mode is False
    assert scanner.calls[0] == {"auto_mode": False, "persist_settings": False}
    assert scanner.calls[1] == {
        "mode": "REAL",
        "auto_mode": False,
        "confirm_real_mode": True,
        "real_mode_verification": "YES",
        "persist_settings": False,
    }
    assert scanner.events[-1][1] == "system"


def test_auto_real_worker_forces_auto_off_when_already_real():
    scanner = _FakeScanner(mode="REAL", auto_mode=True)

    ok, reason = main._auto_real_mode_attempt(
        scanner,
        _settings(),
        _ist("2026-07-03 09:05:00"),
    )

    assert ok is True
    assert reason == "already_real_manual"
    assert scanner.mode == "REAL"
    assert scanner.auto_mode is False
    assert scanner.calls == [{"auto_mode": False, "persist_settings": False}]


def test_auto_real_worker_never_disarms_authorized_auto_after_live_start():
    scanner = _FakeScanner(mode="REAL", auto_mode=True)

    ok, reason = main._auto_real_mode_attempt(
        scanner,
        _settings(),
        _ist("2026-07-03 09:34:58"),
    )

    assert ok is True
    assert reason == "already_real"
    assert scanner.mode == "REAL"
    assert scanner.auto_mode is True
    assert scanner.calls == []


def test_auto_real_worker_skips_non_trading_day():
    scanner = _FakeScanner(auto_mode=True, trading_day=False)

    ok, reason = main._auto_real_mode_attempt(
        scanner,
        _settings(),
        _ist("2026-07-03 09:00:00"),
    )

    assert ok is False
    assert reason == "not_trading_day"
    assert scanner.mode == "HISTORICAL"
    assert scanner.auto_mode is True
    assert scanner.calls == []


def test_auto_real_worker_respects_disable_setting():
    scanner = _FakeScanner(auto_mode=True)

    ok, reason = main._auto_real_mode_attempt(
        scanner,
        _settings(auto_real_mode_premarket_enabled=False),
        _ist("2026-07-03 09:00:00"),
    )

    assert ok is False
    assert reason == "disabled"
    assert scanner.calls == []


def test_auto_real_worker_waits_until_scanner_is_running():
    scanner = _FakeScanner(auto_mode=False)
    scanner.is_running = False

    ok, reason = main._auto_real_mode_attempt(
        scanner,
        _settings(),
        _ist("2026-07-03 09:00:00"),
    )

    assert ok is False
    assert reason == "scanner_not_running"
    assert scanner.mode == "HISTORICAL"
    assert scanner.calls == []
