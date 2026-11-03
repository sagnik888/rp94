"""System-wide last-entry times (2026-09-26 user).

Normal trading runs continuously from 09:15 to the 15:15 hard exit. The last NEW entry per
timeframe is 15m 14:50 (entries stop at 14:51), 5m 15:10 and 1m 15:14 -- the dashboard's
ut_no_entry_after / ut_5min_no_entry_after / ut_1min_no_entry_after.

The limit is on the ENTRY time, inclusive of its minute: the clock when live (a forming bar
may enter while it forms), the signal bar's close in replay (the replay enters there).

The lanes (UT SMART EXIT, S1) apply it directly. The other engines keep their own
bar-time comparison and the EOD-protect clamp (engine.signal_processor._entry_cutoff).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

LAST_ENTRY_DEFAULTS = {"15min": "14:50", "5min": "15:10", "1min": "15:14"}
SETTING_NAMES = {"15min": "ut_no_entry_after", "5min": "ut_5min_no_entry_after", "1min": "ut_1min_no_entry_after"}
_TF_MINUTES = {"1min": 1, "5min": 5, "15min": 15}


def _settings(settings=None):
    if settings is not None:
        return settings
    try:
        from config.settings import get_settings

        return get_settings()
    except Exception:
        return None


def _hhmm(text, default: int) -> int:
    try:
        hh, mm = str(text).split(":")[:2]
        hour, minute = int(hh), int(mm)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return default
        return hour * 60 + minute
    except (ValueError, AttributeError):
        return default


def _tf(timeframe) -> str:
    tf = str(timeframe or "").lower()
    return tf if tf in SETTING_NAMES else "5min"


def _naive(value: datetime) -> datetime:
    if getattr(value, "tzinfo", None) is None:
        return value
    import pytz

    return value.astimezone(pytz.timezone("Asia/Kolkata")).replace(tzinfo=None)


def last_entry(timeframe, settings=None) -> str:
    """"HH:MM" of the last new entry on this timeframe (15m 14:50, 5m 15:10, 1m 15:14)."""
    tf = _tf(timeframe)
    default = LAST_ENTRY_DEFAULTS[tf]
    s = _settings(settings)
    value = str((getattr(s, SETTING_NAMES[tf], None) if s is not None else None) or default)
    return ":".join(value.split(":")[:2]) if _hhmm(value, -1) >= 0 else default


def entry_allowed(signal_time, timeframe, settings=None, now: Optional[datetime] = None) -> bool:
    """May a signal whose bar starts at ``signal_time`` still open a trade?

    The entry time is ``now`` when given (live), otherwise the signal bar's close (replay).
    """
    if signal_time is None:
        return False
    start = _naive(signal_time)
    entry = _naive(now) if now is not None else start + timedelta(minutes=_TF_MINUTES[_tf(timeframe)])
    if entry.date() != start.date():
        return False
    return entry.hour * 60 + entry.minute <= _hhmm(last_entry(timeframe, settings), 15 * 60 + 10)
