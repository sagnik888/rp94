"""Global isolation fixtures for the test suite.

No tests are collection-time quarantined here. A green run therefore means the
collected assertions actually executed (apart from ordinary, test-local skips).
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Isolate SQLite from production data_store/utbot_data.db for the whole suite.
# Must run at import time (before test modules pull in DatabaseManager).
_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="utbot_pytest_db_"))
_TEST_DB_PATH = _TEST_DB_DIR / "utbot_data.db"
os.environ["UTBOT_TEST_DB_PATH"] = str(_TEST_DB_PATH)
os.environ.setdefault("UTBOT_DB_PATH", str(_TEST_DB_PATH))

@pytest.fixture(autouse=True)
def _restore_settings_singleton_after_each_test():
    """Undo any mutation a test leaves on the process-wide settings object.

    get_settings() is a singleton, and a bare `s.field = x` (no monkeypatch) survives
    the test that wrote it and every test after it. That is not hypothetical: until
    2026-08-24 tests/test_audit_trading_fixes.py left slippage_opt_pct at 0.0 and
    slippage_auto_calibration_enabled at False, so every later test in a full run priced
    trades with NO slippage at all — estimate_trade_charges returned the flat Rs.80
    instead of ~Rs.123 — silently weakening every cost floor, profit lock and breakeven
    assertion downstream of it. The tell is always the same: passes alone, fails in the
    full run, or worse, passes in both while asserting against a fiction.

    test_loss_threat_churn_2026_08_10.py had already grown its own save/restore fixture
    naming that exact file as the culprit. One test defending itself is a workaround;
    the leak belongs here, once, for everyone.

    Snapshot/restore of the instance __dict__ only — this neither prevents nor replaces
    monkeypatch, it just guarantees the object each test starts from is the one .env
    describes.
    """
    from config.settings import get_settings

    try:
        settings = get_settings()
        before = dict(vars(settings))
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            current = vars(settings)
            if current != before:
                current.clear()
                current.update(before)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _disable_real_env_persistence_during_tests(monkeypatch):
    """Tests may mutate the singleton settings object; never persist that to .env."""
    import engine.settings_persistence as persistence

    def _no_persist(_settings, *, immediate=False):
        return None

    monkeypatch.setattr(persistence, "schedule_settings_save", _no_persist)
    monkeypatch.setattr(persistence, "flush_settings_save", lambda: None)
    with persistence._PERSISTER._lock:
        persistence._PERSISTER._pending = None
        if persistence._PERSISTER._timer is not None:
            persistence._PERSISTER._timer.cancel()
            persistence._PERSISTER._timer = None

    for module_name in ("scanner", "dashboard.server"):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "schedule_settings_save"):
            monkeypatch.setattr(module, "schedule_settings_save", _no_persist)


@pytest.fixture(autouse=True)
def _keep_morning_readiness_out_of_data_store(tmp_path, monkeypatch):
    """Tests exercise the morning refresh; never let it write real runtime state.

    `_persist_morning_refresh_summary` keeps today's 4-index warmup record so a
    mid-session restart does not erase it and block Auto Mode. Harmless in production,
    but the premarket-scheduling tests drive the refresh with SIMULATED dates and were
    writing data_store/morning_refresh_summary.json for real. That file is gitignored
    and its session_day made it inert, so nothing broke -- but a test that happened to
    use today's date with complete=True would have written a record that satisfies a
    LIVE auto-execution safety gate, which is not a thing a test run may do.

    Same reasoning as _disable_real_env_persistence_during_tests above.
    """
    try:
        from scanner import Scanner
    except Exception:
        return
    target = tmp_path / "morning_refresh_summary.json"
    monkeypatch.setattr(
        Scanner, "_morning_refresh_state_path", lambda self: target, raising=False
    )
