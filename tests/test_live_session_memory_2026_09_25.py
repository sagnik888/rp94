"""Today's Auto Mode and trading mode survive a restart; the 09:00 switch leaves Auto alone.

2026-09-25 user:
- the morning job only switches HISTORICAL -> REAL; it must not change the Auto toggle
  ("that's up to the user"),
- after a restart during the live session the system starts the way it was: Auto ON if it
  was ON, OFF if it was OFF; other settings keep the operator's changes (.env already
  holds those).

Auto is never armed straight from the saved state: the readiness-gated arm restores it,
so a fresh process still proves its feeds, chains and warmup first.
"""
from datetime import datetime, timedelta

import pytest

import main
from engine import live_session_state
from scanner import IST, Scanner
from tests.test_premarket_real_mode_worker import _FakeScanner, _ist
from tests.test_premarket_real_mode_worker import _settings as worker_settings
from tests.test_real_switch_with_prearmed_auto_2026_09_17 import _switch_to_real, harness  # noqa: F401


@pytest.fixture(autouse=True)
def _state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(live_session_state, "PATH", tmp_path / "live_session_state.json")


def _today():
    return datetime.now(IST).date().isoformat()


def test_state_is_kept_for_the_current_day_only():
    live_session_state.update("2026-09-24", auto_mode=True)
    assert live_session_state.load("2026-09-24")["auto_mode"] is True
    assert live_session_state.load("2026-09-25") == {}
    live_session_state.update("2026-09-25", mode="REAL")
    assert live_session_state.load("2026-09-25").get("auto_mode") is None   # a new day starts empty


# ---------------------------------------------------------------- the morning switch


class _SessionScanner(_FakeScanner):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.carried = []
        self.kept = False

    def _carry_auto_intent(self, day):
        self.carried.append(day)

    def _operator_kept_historical_today(self, now_ist, switch_at):
        return self.kept


@pytest.mark.parametrize("auto_before", [True, False])
def test_the_real_switch_keeps_the_operators_auto_choice(auto_before):
    scanner = _SessionScanner(auto_mode=auto_before)
    ok, reason = main._auto_real_mode_attempt(scanner, worker_settings(), _ist("2026-09-25 09:00:00"))
    assert (ok, reason) == (True, "switched_real_manual") and scanner.mode == "REAL"
    # Auto is held while entering REAL; an ON is remembered and restored after readiness.
    assert scanner.carried == (["2026-09-25"] if auto_before else [])


def test_a_restart_after_the_operator_went_back_to_historical_stays_historical():
    scanner = _SessionScanner(auto_mode=False)
    scanner.kept = True
    ok, reason = main._auto_real_mode_attempt(scanner, worker_settings(), _ist("2026-09-25 11:00:00"))
    assert (ok, reason) == (False, "operator_kept_historical")
    assert scanner.mode == "HISTORICAL" and scanner.calls == []


def test_only_an_operator_choice_after_the_switch_time_counts_as_kept_historical():
    sc = Scanner.__new__(Scanner)
    sc._live_session_state_enabled = True
    now = _ist("2026-09-25 11:00:00")
    live_session_state.update("2026-09-25", mode="HISTORICAL", mode_source="operator", mode_at="10:30:00")
    assert sc._operator_kept_historical_today(now, datetime.strptime("09:00:00", "%H:%M:%S").time()) is True
    live_session_state.update("2026-09-25", mode_at="08:50:00")            # before the switch
    assert sc._operator_kept_historical_today(now, datetime.strptime("09:00:00", "%H:%M:%S").time()) is False
    live_session_state.update("2026-09-25", mode_at="10:30:00", mode_source="system")
    assert sc._operator_kept_historical_today(now, datetime.strptime("09:00:00", "%H:%M:%S").time()) is False


# ---------------------------------------------------------------- configure + readiness-gated restore


def _complete_morning(sc, day):
    sc._morning_refresh_summary = {"session_day": day, "complete": True}
    sc._morning_refresh_last_date = day
    sc._configured_startup_auto_armed_date = None
    sc._configured_startup_auto_last_attempt_at = 0.0


def test_prearmed_auto_is_held_through_the_switch_then_restored_after_readiness(harness):  # noqa: F811
    h = harness(mode="HISTORICAL", auto_mode=True)
    sc = h.scanner
    sc._live_session_state_enabled = True
    assert _switch_to_real(sc) is True
    assert sc.mode == "REAL" and sc.auto_mode is False              # held: morning readiness missing
    day = _today()
    assert live_session_state.load(day)["auto_mode"] is True         # the operator's ON is kept
    h.state["morning"] = []                                          # the morning refresh completed
    _complete_morning(sc, day)
    sc._maybe_arm_configured_startup_auto(day, datetime.now(IST).replace(hour=10, minute=0))
    assert sc.auto_mode is True


def test_auto_that_was_off_stays_off_after_readiness(harness):  # noqa: F811
    h = harness(mode="HISTORICAL", auto_mode=False)
    sc = h.scanner
    sc._live_session_state_enabled = True
    assert _switch_to_real(sc) is True
    day = _today()
    h.state["morning"] = []
    _complete_morning(sc, day)
    sc._maybe_arm_configured_startup_auto(day, datetime.now(IST).replace(hour=10, minute=0))
    assert sc.auto_mode is False


def test_turning_auto_off_during_the_session_is_remembered_as_off(harness):  # noqa: F811
    h = harness(mode="REAL", auto_mode=True, morning=())
    sc = h.scanner
    sc._live_session_state_enabled = True
    assert sc.configure(auto_mode=False) is True
    assert live_session_state.load(_today())["auto_mode"] is False


def test_a_refused_real_switch_is_not_remembered_as_the_operator_choosing_historical(harness):  # noqa: F811
    from tests.test_real_switch_with_prearmed_auto_2026_09_17 import _RealBroker

    h = harness(mode="HISTORICAL", auto_mode=False, broker=_RealBroker(acquired=False))
    sc = h.scanner
    sc._live_session_state_enabled = True
    assert _switch_to_real(sc) is False
    assert live_session_state.load(_today()).get("mode") is None


def test_a_test_shell_never_writes_the_live_state(harness):  # noqa: F811
    h = harness(mode="REAL", auto_mode=True, morning=())
    assert h.scanner.configure(auto_mode=False) is True             # no _live_session_state_enabled
    assert live_session_state.load(_today()) == {}


# ---------------------------------------------------------------- restart


def _restarted_scanner():
    sc = Scanner.__new__(Scanner)
    sc._live_session_state_enabled = True
    sc.mode = "REAL"
    sc.auto_mode = False
    sc.log_event = lambda *a, **k: None
    sc._last_configure_block_reason = ""
    calls = []

    def configure(**kwargs):
        calls.append(kwargs)
        sc.auto_mode = bool(kwargs.get("auto_mode"))
        return True

    sc.configure = configure
    return sc, calls


@pytest.mark.parametrize("was_on", [True, False])
def test_after_a_restart_auto_comes_back_exactly_as_it_was(monkeypatch, was_on):
    import scanner as scanner_module
    from types import SimpleNamespace

    monkeypatch.setattr(scanner_module, "get_settings", lambda: SimpleNamespace(
        auto_real_mode_startup_enabled=False, real_mode_live_start="09:15:00", real_mode_live_end="15:30:00"))
    day = _today()
    live_session_state.update(day, auto_mode=was_on)
    sc, calls = _restarted_scanner()
    _complete_morning(sc, day)
    now = datetime.now(IST).replace(hour=11, minute=0, tzinfo=None)
    sc._maybe_arm_configured_startup_auto(day, now)
    assert sc.auto_mode is was_on
    assert len(calls) == (1 if was_on else 0)
    # Yesterday's Auto never carries into today.
    sc2, calls2 = _restarted_scanner()
    _complete_morning(sc2, (datetime.now(IST) + timedelta(days=1)).date().isoformat())
    sc2._maybe_arm_configured_startup_auto((datetime.now(IST) + timedelta(days=1)).date().isoformat(), now)
    assert calls2 == []


def test_a_real_switch_that_fell_back_to_historical_is_not_the_operators_choice():
    """"Kite not connected" leaves the scanner HISTORICAL after a REAL request."""
    sc = Scanner.__new__(Scanner)
    sc._live_session_state_enabled = True
    sc.mode = "HISTORICAL"
    sc._record_live_session_state(auto_requested=False, auto_intent=False, mode_requested="REAL", operator=True)
    assert live_session_state.load(_today()).get("mode") is None
    sc._record_live_session_state(auto_requested=False, auto_intent=False, mode_requested="HISTORICAL", operator=True)
    assert live_session_state.load(_today())["mode"] == "HISTORICAL"


@pytest.mark.parametrize("was_on", [True, False])
def test_the_real_restart_flow_through_the_morning_worker_keeps_the_days_auto(harness, was_on):  # noqa: F811
    """Restart mid-session: Auto starts OFF, the worker's defensive 'Auto OFF' and REAL
    switch must not overwrite the day's saved choice (live bug 2026-09-25 09:50)."""
    day = _today()
    live_session_state.update(day, auto_mode=was_on)
    h = harness(mode="HISTORICAL", auto_mode=False)        # a fresh process: Auto OFF
    sc = h.scanner
    sc._live_session_state_enabled = True
    sc.is_running = True
    sc._is_trading_day = lambda _now: True
    now = datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0)
    ok, reason = main._auto_real_mode_attempt(sc, worker_settings(), now)
    assert ok and sc.mode == "REAL"
    assert live_session_state.load(day)["auto_mode"] is was_on       # not overwritten
    h.state["morning"] = []
    _complete_morning(sc, day)
    sc._maybe_arm_configured_startup_auto(day, now.replace(tzinfo=None))
    assert sc.auto_mode is was_on
