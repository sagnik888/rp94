"""Auto-mode gate fixes (2026-07-30): volume pipeline + runtime watchdog.

VOLUME: AngelOne history rate-limiting (189 hits in one session) forced the Yahoo
fallback (279 times), and Yahoo reports volume=0 for Indian index tickers (verified live:
^NSEI / ^NSEBANK, 75 bars, all zero). Because the candle merge keeps the LAST duplicate,
those zeros overwrote genuine AngelOne volume — every index read vol_sum=0 from
2026-07-27, which silently disabled every volume-gated entry path. Volume gates fail
CLOSED (volume_ratio -> 0.0), so this over-filtered rather than mis-traded, but auto mode
would have run with a large part of its entry logic dead.

WATCHDOG: unattended operation cannot tolerate silent failure — a dead scanner behind a
live dashboard, Auto Mode reverting to OFF (it is never persisted), or the Fyers token
expiring (cannot auto-refresh: broker code -16, SEBI).
"""
import os
import sys
import time
from types import SimpleNamespace

import data.kite_auth  # noqa: F401  (imported so _watchdog can stub get_kite_auth)

import pandas as pd
import pytest

from data.candle_builder import CandleBuilder


def _bars(start, vol):
    idx = pd.date_range(start, periods=3, freq="1min")
    return pd.DataFrame(
        {"open": [100, 101, 102], "high": [101, 102, 103], "low": [99, 100, 101],
         "close": [100.5, 101.5, 102.5], "volume": vol},
        index=idx,
    )


def test_zero_volume_fallback_does_not_wipe_real_volume():
    cb = CandleBuilder(max_candles=500)
    cb.update_candles("NIFTY", _bars("2026-07-30 09:15", [500000, 600000, 700000]), "1min")
    assert cb._candles_1min["NIFTY"]["volume"].sum() == 1_800_000
    # Yahoo returns the SAME bars with volume 0
    cb.update_candles("NIFTY", _bars("2026-07-30 09:15", [0, 0, 0]), "1min")
    assert cb._candles_1min["NIFTY"]["volume"].sum() == 1_800_000


def test_real_volume_updates_are_still_applied():
    """Preservation must not freeze volume: a genuine non-zero update still wins."""
    cb = CandleBuilder(max_candles=500)
    cb.update_candles("NIFTY", _bars("2026-07-30 09:15", [100, 100, 100]), "1min")
    cb.update_candles("NIFTY", _bars("2026-07-30 09:15", [900, 900, 900]), "1min")
    assert cb._candles_1min["NIFTY"]["volume"].sum() == 2700


def test_new_bars_with_zero_volume_are_kept_as_zero():
    """Only OVERLAPPING bars are protected; a brand-new zero-volume bar is untouched."""
    cb = CandleBuilder(max_candles=500)
    cb.update_candles("NIFTY", _bars("2026-07-30 09:15", [500, 500, 500]), "1min")
    cb.update_candles("NIFTY", _bars("2026-07-30 10:00", [0, 0, 0]), "1min")
    assert len(cb._candles_1min["NIFTY"]) == 6
    assert cb._candles_1min["NIFTY"]["volume"].sum() == 1500


def test_history_pacing_is_configurable_and_conservative(monkeypatch):
    """`_env_file=None` still reads os.environ, and importing main.py runs load_dotenv,
    so the host's values leak in during a full-suite run. Clear them to test the shipped
    code defaults."""
    from config.settings import Settings

    monkeypatch.delenv("HISTORY_RATE_LIMIT_PER_SECOND", raising=False)
    monkeypatch.delenv("HISTORY_CACHE_TTL_SECONDS", raising=False)
    s = Settings(_env_file=None)
    assert s.history_rate_limit_per_second <= 0.5, "pacing must stay under AngelOne's limit"
    assert s.history_cache_ttl_seconds >= 240.0, "longer TTL reduces history pressure"


# ── watchdog ────────────────────────────────────────────────────────────────

def _watchdog(scanner, alerts, kite_auth_required=False, startup_grace="0"):
    """Drive the watchdog deterministically.

    The liveness checks are gated on `now.weekday() < 5`, so these tests silently
    stopped exercising them on weekends — a Saturday run reported "no alert" and
    looked like a regression. The clock is pinned to a Wednesday so the result no
    longer depends on the day the suite happens to run.

    Kite session state is injected for the same reason: the real one depends on
    whether a token happens to be cached on this machine.
    """
    os.environ["RUNTIME_WATCHDOG_INTERVAL_SECONDS"] = "1"
    os.environ["RUNTIME_WATCHDOG_START"] = "00:00"
    os.environ["RUNTIME_WATCHDOG_UNTIL"] = "23:59"
    # Default 0 so the existing liveness cases still assert immediately. The grace
    # itself is exercised explicitly by the startup tests below.
    os.environ["RUNTIME_WATCHDOG_STARTUP_GRACE_SECONDS"] = str(startup_grace)
    import main

    real_datetime = main.datetime
    midweek = real_datetime(2026, 7, 29, 12, 0, 0)  # a Wednesday

    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return tz.localize(midweek) if tz is not None else midweek

    status = {
        "auth_required": bool(kite_auth_required),
        "reason": "token expired" if kite_auth_required else "",
    }

    main.datetime = _FrozenDatetime
    main.send_desktop_notification = lambda msg, level="info": alerts.append(msg)
    sys.modules["data.kite_auth"].get_kite_auth = lambda *a, **k: SimpleNamespace(
        status=lambda: status
    )
    try:
        main.start_runtime_watchdog(
            scanner, SimpleNamespace(dashboard_host="127.0.0.1", dashboard_port=8500)
        )
        time.sleep(2.0)
    finally:
        main.datetime = real_datetime


def _scanner(**over):
    base = dict(
        is_running=True, last_scan_time=None, auto_mode=False,
        log_event=lambda *a, **k: None,
        data=SimpleNamespace(get_fyers_auth_status=lambda: {"auth_required": False}),
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_watchdog_alerts_when_scanner_is_dead():
    alerts = []
    _watchdog(_scanner(is_running=False), alerts)
    assert any("NOT running" in a for a in alerts)


def test_a_restart_inside_market_hours_does_not_cry_wolf():
    """The scanner sets is_running only once it has finished coming up.

    Observed live on 2026-08-07: the watchdog fired "scanner is NOT running during
    market hours" at 10:02:32.033 — the same MILLISECOND the scanner logged that it
    had started. A CRITICAL and a desktop alert on every routine restart is how an
    operator learns to ignore CRITICALs.
    """
    alerts = []
    _watchdog(_scanner(is_running=False), alerts, startup_grace="60")
    assert not any("NOT running" in a for a in alerts), (
        "still alerting during the startup window"
    )


# The loop sleeps a floor of 15s between iterations, so a test can never observe a
# SECOND poll in a sane window. The decision itself is tested directly instead.

def test_the_grace_defers_the_check_it_does_not_cancel_it():
    """A scanner that never comes up must still be reported once grace expires."""
    import main

    assert main._scanner_liveness_alert_due(False, False, 121.0, 120.0) is True


def test_still_silent_while_the_grace_is_open():
    import main

    assert main._scanner_liveness_alert_due(False, False, 5.0, 120.0) is False


def test_a_scanner_that_dies_after_coming_up_alerts_immediately():
    """Grace applies only before the scanner has EVER been seen running.

    Without the seen-running latch, a long grace would also mute a mid-session
    death — the incident the watchdog was written for.
    """
    import main

    assert main._scanner_liveness_alert_due(False, True, 1.0, 9999.0) is True


def test_a_running_scanner_is_never_alerted_on():
    import main

    assert main._scanner_liveness_alert_due(True, False, 0.0, 0.0) is False
    assert main._scanner_liveness_alert_due(True, True, 99999.0, 0.0) is False


def test_the_watchdog_loop_actually_consults_the_decision():
    """A helper wired to nothing is the dominant defect pattern in this codebase."""
    import ast
    import inspect
    import textwrap

    import main

    tree = ast.parse(textwrap.dedent(inspect.getsource(main.start_runtime_watchdog)))
    called = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_scanner_liveness_alert_due" in called


def test_watchdog_alerts_when_fyers_login_required():
    alerts = []
    _watchdog(
        _scanner(data=SimpleNamespace(
            get_fyers_auth_status=lambda: {"auth_required": True, "reason": "token expired"})),
        alerts,
    )
    assert any("Fyers login required" in a for a in alerts)
    assert any("/api/fyers_login" in a for a in alerts)


def test_watchdog_is_quiet_when_healthy():
    alerts = []
    _watchdog(_scanner(), alerts)
    assert alerts == []


def test_watchdog_is_wired_into_startup():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
    assert "def start_runtime_watchdog(" in src
    assert "start_runtime_watchdog(scanner, settings)" in src
