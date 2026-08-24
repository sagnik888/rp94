from datetime import datetime
from types import SimpleNamespace

import scanner as scanner_module
from scanner import Scanner


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        wall = cls(2026, 8, 21, 15, 16, 0)
        return tz.localize(wall) if tz is not None else wall


class _Candles:
    def __init__(self, timestamp):
        self.timestamp = timestamp

    def get_max_timestamp(self):
        return self.timestamp


def _subject(monkeypatch, market_timestamp):
    monkeypatch.setattr(scanner_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(clock_skew_max_seconds=180.0),
    )
    return SimpleNamespace(candles=_Candles(market_timestamp))


def test_stale_market_timestamp_cannot_postpone_the_eod_safety_clock(monkeypatch):
    # A stalled feed must not move the safety clock behind the host clock and
    # postpone the configured 15:15 hard square-off.
    subject = _subject(monkeypatch, datetime(2026, 8, 21, 15, 10, 0))

    observed = Scanner._session_squareoff_clock(subject)

    assert observed.hour == 15
    assert observed.minute == 16


def test_ahead_market_timestamp_can_advance_the_eod_safety_clock(monkeypatch):
    subject = _subject(monkeypatch, datetime(2026, 8, 21, 15, 20, 0))

    observed = Scanner._session_squareoff_clock(subject)

    assert observed.hour == 15
    assert observed.minute == 20


def test_previous_session_timestamp_cannot_replace_todays_wall_clock(monkeypatch):
    subject = _subject(monkeypatch, datetime(2026, 8, 20, 15, 30, 0))

    observed = Scanner._session_squareoff_clock(subject)

    assert observed.date().isoformat() == "2026-08-21"
    assert observed.hour == 15
    assert observed.minute == 16
