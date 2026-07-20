"""
Restart recovery window must be timeframe-aware.

Bug: after a restart, a signal whose candle closed >120s earlier was dropped as a
"stale backfill echo" (flat live_restart_recovery_grace_seconds=120). A 15min signal
is still the current candle for several minutes, so it was still actionable.
Fix: restart recovery uses a fixed per-timeframe window measured from candle close —
180s (5min) / 360s (15min) — and recovers only the most recent just-closed candle.

Repro: system restarted 10:04, NIFTY/MIDCPNIFTY 15min candle closed 10:00, backfill
ran 10:05 -> old deadline 10:02 dropped it; new deadline 10:06 recovers it.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from scanner import Scanner


def _stub(started_at):
    st = SimpleNamespace(mode="REAL", _process_started_at=started_at)
    for m in (
        "_live_signal_processing_deadline",
        "_signal_in_restart_recovery_window",
        "_session_squareoff_clock",
    ):
        setattr(st, m, getattr(Scanner, m).__get__(st, Scanner))
    return st


RESTART = datetime(2026, 6, 19, 10, 4, 38)
SIG_15M = datetime(2026, 6, 19, 9, 45)   # 15min candle, closes 10:00


def test_15m_signal_recovery_window_is_360s():
    st = _stub(RESTART)
    deadline = st._live_signal_processing_deadline(SIG_15M, "15min")
    assert deadline == datetime(2026, 6, 19, 10, 6)  # close 10:00 + 360s
    assert st._signal_in_restart_recovery_window(SIG_15M, "15min", datetime(2026, 6, 19, 10, 5)) is True
    # The exact failure from the field: backfill at 10:05 must NOT drop it.
    assert st._signal_in_restart_recovery_window(SIG_15M, "15min", datetime(2026, 6, 19, 10, 2, 30)) is True


def test_15m_signal_expires_after_360s():
    st = _stub(RESTART)
    assert st._signal_in_restart_recovery_window(SIG_15M, "15min", datetime(2026, 6, 19, 10, 6, 30)) is False


def test_only_most_recent_candle_recovers():
    st = _stub(RESTART)
    older = datetime(2026, 6, 19, 9, 30)  # closes 9:45 — a previous candle (deadline 9:51)
    assert st._signal_in_restart_recovery_window(older, "15min", datetime(2026, 6, 19, 10, 5)) is False


def test_5min_restart_recovery_window_is_180s():
    st = _stub(RESTART)
    # Candle must have closed BEFORE the 10:04 restart to exercise the restart branch.
    sig5 = datetime(2026, 6, 19, 9, 55)  # 5min candle closes 10:00 (< 10:04 restart)
    assert st._live_signal_processing_deadline(sig5, "5min") == datetime(2026, 6, 19, 10, 3)  # close 10:00 + 180s


def test_5min_running_before_close_uses_300s():
    # Process started before the candle closed -> slow-refresh grace (300s), not 180s.
    st = _stub(RESTART)
    sig5 = datetime(2026, 6, 19, 10, 0)  # closes 10:05, after 10:04 start
    assert st._live_signal_processing_deadline(sig5, "5min") == datetime(2026, 6, 19, 10, 10)


def test_running_before_close_grace_unchanged():
    # Scanner already running when the candle closed -> slow-refresh grace (300s), unchanged.
    st = _stub(datetime(2026, 6, 19, 9, 50))
    assert st._live_signal_processing_deadline(SIG_15M, "15min") == datetime(2026, 6, 19, 10, 5)
