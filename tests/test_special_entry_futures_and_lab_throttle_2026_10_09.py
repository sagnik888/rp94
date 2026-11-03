"""Special-entry futures-minute fallback and the market-hours engine-lab throttle (2026-10-09)."""
import calendar
from types import SimpleNamespace as NS

import pytest

from engine.entry_data_quality import evaluate_entry_data_quality

NO_CASH = {"volume_available": False, "volume_reliable": False, "completed_cash_volume_fresh": False,
           "completed_cash_session_bars": 0}


def _volume(available=True, reliable=False, completed=8):
    return {**NO_CASH, "futures_minute_volume": {"available": available, "reliable": reliable,
                                                 "completed_samples": completed}}


def _verdict(volume, *, completed_mode=True, accept=True):
    return evaluate_entry_data_quality(live=True, special_entry=True, one_minute_volume=volume,
                                       accept_completed_cash_bars=completed_mode, accept_futures_minute=accept)


def test_completed_futures_minutes_stand_in_for_missing_cash_bars():
    verdict = _verdict(_volume())
    assert verdict["reason_code"] != "ONE_MINUTE_VOLUME_UNAVAILABLE"
    assert verdict["checks"]["one_minute_volume_basis"] == "futures_minute"
    assert verdict["checks"]["one_minute_cash_volume"] is False


@pytest.mark.parametrize("volume,completed_mode,accept", [
    (_volume(completed=3), True, True),          # too few completed futures minutes
    (_volume(available=False), True, True),      # futures feed not current
    (_volume(reliable=False), False, True),      # spike/shock route needs the forming minute
    (_volume(), True, False),                    # fallback disabled
])
def test_futures_minute_must_be_genuinely_usable(volume, completed_mode, accept):
    verdict = _verdict(volume, completed_mode=completed_mode, accept=accept)
    assert verdict["reason_code"] == "ONE_MINUTE_VOLUME_UNAVAILABLE"


def test_spike_route_accepts_a_reliable_forming_futures_minute():
    verdict = _verdict(_volume(reliable=True), completed_mode=False)
    assert verdict["checks"]["one_minute_volume_basis"] == "futures_minute"


def test_cash_volume_keeps_priority():
    cash = {**_volume(), "volume_available": True, "volume_reliable": True, "volume_comparable": True}
    verdict = _verdict(cash, completed_mode=False)
    assert verdict["checks"]["one_minute_volume_basis"] == "intrabar"
    assert verdict["checks"]["one_minute_futures_volume"] is False


def _epoch_ist(hhmm):
    hour, minute = map(int, hhmm.split(":"))
    return calendar.timegm((2026, 10, 9, hour, minute, 0)) - 19800


@pytest.mark.parametrize("hhmm,refreshes", [("11:00", False), ("16:00", True), ("09:10", True)])
def test_engine_lab_refreshes_slowly_during_the_session(monkeypatch, hhmm, refreshes):
    import scanner as module
    from scanner import Scanner
    from config.settings import get_settings

    now = _epoch_ist(hhmm)
    monkeypatch.setattr(module.time, "time", lambda: now)
    monkeypatch.setattr(get_settings(), "engine_lab_refresh_seconds", 60.0)
    monkeypatch.setattr(get_settings(), "engine_lab_market_refresh_seconds", 300.0)
    sc = Scanner.__new__(Scanner)
    sc._engine_lab_live_refreshed_at = now - 100
    sc._engine_lab_refresh_inflight = False
    sc.scan_count = 50
    sc._initial_setup_completed = True
    computed = []
    sc._engine_lab_collect_frames = lambda copy=True: {}
    sc._engine_lab_compute_snapshot = lambda frames, include_actual_sweep=False: computed.append(1)
    sc._lower_current_thread_priority = lambda: None
    sc.engine_lab = NS(snapshot=lambda: {"ok": True})
    assert sc._engine_lab_snapshot_nonblocking() == {"ok": True}
    assert bool(computed) is refreshes
