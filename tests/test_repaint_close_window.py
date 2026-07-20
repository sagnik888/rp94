"""
Repaint guard is an INTRABAR mechanism only.

Rule (operator): a repaint may abort only while the source candle is forming, plus
a brief grace at the close moment so a live scan can make the final check. Once
that grace passes, the candle has long confirmed; a still-open trade is a NORMAL
tracked/trailed position and must NEVER be repaint-aborted afterwards.

Field bug: NIFTY 15min intrabar entry at 10:49 (candle 10:45 closes 11:00) was
REPAINT-ABORTED at 11:08 — 8 minutes after close. With the grace window (90s) the
late evaluation is treated as a confirmed signal instead.

Intra-candle repaint stays functional for both 5min and 15min (unchanged path).
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from scanner import Scanner
from config.settings import get_settings


def _stub():
    st = SimpleNamespace()
    st._repaint_close_check_in_window = Scanner._repaint_close_check_in_window.__get__(st, Scanner)
    return st


def _post_repaint_stub():
    st = SimpleNamespace(_post_repaint_recheck={}, _diag_counts={})
    st._repaint_signal_type = Scanner._repaint_signal_type
    st._naive_ist_time = Scanner._naive_ist_time
    st._post_repaint_recheck_interval_seconds = Scanner._post_repaint_recheck_interval_seconds.__get__(st, Scanner)
    st._mark_post_repaint_recheck = Scanner._mark_post_repaint_recheck.__get__(st, Scanner)
    st._post_repaint_recheck_due = Scanner._post_repaint_recheck_due.__get__(st, Scanner)

    def _diag_inc(category, key, amount=1):
        st._diag_counts[(category, key)] = st._diag_counts.get((category, key), 0) + amount

    st._diag_inc = _diag_inc
    return st


@pytest.fixture(autouse=True)
def _grace_90():
    get_settings().repaint_close_check_grace_seconds = 90.0


SIG_15M = datetime(2026, 6, 19, 10, 45)   # 15min candle closes 11:00
SIG_5M = datetime(2026, 6, 19, 10, 45)    # 5min candle closes 10:50


def test_late_post_close_eval_is_not_a_repaint():
    st = _stub()
    # The exact field case: evaluated at 11:08, 8 min after close -> confirmed, no abort.
    assert st._repaint_close_check_in_window(SIG_15M, "15min", datetime(2026, 6, 19, 11, 8)) is False


def test_prompt_close_check_still_aborts_window_15m():
    st = _stub()
    assert st._repaint_close_check_in_window(SIG_15M, "15min", datetime(2026, 6, 19, 11, 0, 30)) is True
    assert st._repaint_close_check_in_window(SIG_15M, "15min", datetime(2026, 6, 19, 11, 1, 0)) is True   # == grace edge
    assert st._repaint_close_check_in_window(SIG_15M, "15min", datetime(2026, 6, 19, 11, 2, 0)) is False  # past grace


def test_prompt_close_check_still_aborts_window_5m():
    st = _stub()
    assert st._repaint_close_check_in_window(SIG_5M, "5min", datetime(2026, 6, 19, 10, 51, 0)) is True
    assert st._repaint_close_check_in_window(SIG_5M, "5min", datetime(2026, 6, 19, 10, 55, 0)) is False


def test_intra_candle_recheck_cadence_default_is_20s():
    # Intra-candle repaint re-checks run every repaint_check_interval_seconds (20s)
    # until candle close, for both 5min and 15min.
    s = get_settings()
    assert getattr(s, "repaint_check_interval_seconds", None) == 20.0
    # Cadence must be well inside both candle periods so multiple checks occur.
    assert s.repaint_check_interval_seconds < 5 * 60
    assert s.repaint_check_interval_seconds < 15 * 60


def test_post_repaint_abort_same_candle_rechecks_once_per_minute():
    st = _post_repaint_stub()
    settings = get_settings()
    old_interval = settings.live_repaint_post_exit_recheck_seconds
    settings.live_repaint_post_exit_recheck_seconds = 60.0
    try:
        candidate = SimpleNamespace(
            instrument="NIFTY",
            timeframe="5min",
            direction="LONG",
            source_signal_type="BUY",
            signal_timestamp=SIG_5M,
        )
        st._mark_post_repaint_recheck(
            candidate,
            SIG_5M,
            datetime(2026, 6, 19, 10, 46),
        )

        assert st._post_repaint_recheck_due(
            "NIFTY", "5min", "BUY", SIG_5M, datetime(2026, 6, 19, 10, 46, 59)
        ) is False
        assert st._post_repaint_recheck_due(
            "NIFTY", "5min", "BUY", SIG_5M, datetime(2026, 6, 19, 10, 47)
        ) is True
        assert st._post_repaint_recheck_due(
            "NIFTY", "5min", "BUY", SIG_5M, datetime(2026, 6, 19, 10, 47, 30)
        ) is False
        assert st._post_repaint_recheck_due(
            "NIFTY", "5min", "BUY", SIG_5M, datetime(2026, 6, 19, 10, 48)
        ) is True
        assert st._diag_counts[("repaint_guard", "post_repaint_recheck_armed")] == 1
        assert st._diag_counts[("repaint_guard", "post_repaint_recheck_due")] == 2
    finally:
        settings.live_repaint_post_exit_recheck_seconds = old_interval


def test_post_repaint_recheck_stops_when_pending_or_candle_closes():
    st = _post_repaint_stub()
    candidate = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        source_signal_type="BUY",
        signal_timestamp=SIG_5M,
    )
    st._mark_post_repaint_recheck(
        candidate,
        SIG_5M,
        datetime(2026, 6, 19, 10, 46),
    )

    assert st._post_repaint_recheck_due(
        "NIFTY", "5min", "BUY", SIG_5M, datetime(2026, 6, 19, 10, 47), pending=True
    ) is False
    assert st._post_repaint_recheck_due(
        "NIFTY", "5min", "BUY", SIG_5M, datetime(2026, 6, 19, 10, 50)
    ) is False
    assert "NIFTY_5min" not in st._post_repaint_recheck


def test_grace_is_configurable():
    st = _stub()
    get_settings().repaint_close_check_grace_seconds = 30.0
    try:
        # close 11:00 + 30s = 11:00:30
        assert st._repaint_close_check_in_window(SIG_15M, "15min", datetime(2026, 6, 19, 11, 0, 20)) is True
        assert st._repaint_close_check_in_window(SIG_15M, "15min", datetime(2026, 6, 19, 11, 0, 45)) is False
    finally:
        get_settings().repaint_close_check_grace_seconds = 90.0
