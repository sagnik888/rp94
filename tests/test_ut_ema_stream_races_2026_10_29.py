"""Deterministic regressions for observation-worker authorization races."""
import threading
import json
from unittest.mock import Mock

import pandas as pd

from engine.ut_ema_stream import UTEmaTickService
from tests.test_ut_ema_stream_2026_10_29 import real_host


def test_inflight_observation_cannot_clear_a_later_queue_overflow(monkeypatch, tmp_path):
    scanner, adapter, _ = real_host(monkeypatch)
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path, capacity=1)
    service._schedule = Mock()
    at = pd.Timestamp("2026-10-29 12:35")
    for sequence, price in enumerate((998.05, 1000., 997.95), start=1):
        service.process_observation("NIFTY", price, at + pd.Timedelta(milliseconds=sequence), sequence)
    assert adapter.stream_snapshot()["utema_executable_event_ids"]
    service._schedule.reset_mock()

    entered, release = threading.Event(), threading.Event()
    observe = service._observe
    errors = []

    def blocked_observation(*args):
        entered.set()
        if not release.wait(timeout=5.):
            raise AssertionError("Observation barrier timed out")
        return observe(*args)

    service._observe = blocked_observation

    def advance():
        try:
            # The short's native target is touched, so the unpatched worker
            # publishes an event and resets health to READY after overflow.
            service.process_observation("NIFTY", 993., at + pd.Timedelta(milliseconds=4), 4)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=advance, daemon=True)
    worker.start()
    try:
        assert entered.wait(timeout=5.)
        # The in-flight observation is outside the bounded queue. A concurrent
        # producer fills the remaining slot, then loses the following tick.
        with service._condition:
            service._queue.append(("NIFTY", 993.1, at + pd.Timedelta(milliseconds=5), 5))
        assert not service.submit("NIFTY", 993.2, at + pd.Timedelta(milliseconds=6))
        assert service.health["NIFTY_5min"]["status"] == "OVERFLOW"
    finally:
        release.set()
        worker.join(timeout=5.)
    assert not worker.is_alive()
    assert not errors
    assert service.health["NIFTY_5min"]["status"] == "OVERFLOW"
    assert adapter.stream_snapshot()["utema_executable_event_ids"] == []
    service._schedule.assert_not_called()


def test_overflow_survives_restart_and_requires_a_disclosed_rebuild(monkeypatch, tmp_path):
    scanner, adapter, _ = real_host(monkeypatch)
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path, capacity=1)
    at = pd.Timestamp("2026-10-29 12:35")
    for sequence, price in enumerate((998.05, 1000., 997.95), start=1):
        service.process_observation("NIFTY", price, at + pd.Timedelta(milliseconds=sequence), sequence)
    with service._condition:
        service._queue.append(("NIFTY", 1000., at + pd.Timedelta(milliseconds=4), 4))
    assert not service.submit("NIFTY", 997.95, at + pd.Timedelta(milliseconds=5))
    service.close()
    marker = tmp_path/"NIFTY_5min.gap.json"
    assert json.loads(marker.read_text())["requires_rebuild"]

    scanner2, adapter2, _ = real_host(monkeypatch)
    restored = UTEmaTickService(scanner2, checkpoint_dir=tmp_path)
    restored.process_observation("NIFTY", 997.95, at + pd.Timedelta(milliseconds=6), 6)
    result = adapter2.stream_snapshot()
    health = restored.health["NIFTY_5min"]
    assert health["status"] == "READY"
    assert health["historical_rebuild"]
    assert health["live_tick_path_restored"] is False
    assert health["missing_path_reason"] == "queue_overflow"
    assert not health.get("restored", False)
    assert result["utema_live_path_available"] is False
    assert result["utema_executable_event_ids"] == []
    assert not marker.exists()
    checkpoint = json.loads((tmp_path/"NIFTY_5min.json").read_text())
    assert checkpoint["missing_path"]["live_tick_path_restored"] is False
    assert checkpoint["missing_path"]["requires_rebuild"] is False
    # Subsequent clean restarts retain the rebuilt owner's new state and its
    # missing-path provenance instead of repeatedly discarding healthy ticks.
    scanner3, adapter3, _ = real_host(monkeypatch)
    again = UTEmaTickService(scanner3, checkpoint_dir=tmp_path)
    again.process_observation("NIFTY", 997.95, at + pd.Timedelta(milliseconds=7), 7)
    assert again.health["NIFTY_5min"]["restored"]
    assert again.health["NIFTY_5min"]["live_tick_path_restored"] is False
    assert adapter3.stream_snapshot()["utema_live_path_available"] is False
    again.process_observation("NIFTY", 1000., at + pd.Timedelta(milliseconds=8), 8)
    again.process_observation("NIFTY", 997.95, at + pd.Timedelta(milliseconds=9), 9)
    fresh = adapter3.stream_snapshot()
    assert fresh["utema_executable_event_ids"]
    assert fresh["utema_live_path_available"] is False
    for signal in fresh["signals"]:
        if signal.utema_event_id in fresh["utema_executable_event_ids"]:
            assert signal.utema_event["observed_at"] == (at + pd.Timedelta(milliseconds=9)).isoformat()


def test_overflow_rebuild_waits_for_complete_closed_history(monkeypatch, tmp_path):
    scanner, _, frame = real_host(monkeypatch)
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path)
    at = pd.Timestamp("2026-10-29 12:35:01")
    service.process_observation("NIFTY", 998.05, at, 1)
    service._overflowed.add("NIFTY_5min")
    service._mark_missing_path("NIFTY_5min", "queue_overflow", at)
    service.close()
    scanner2, adapter2, _ = real_host(monkeypatch)
    scanner2.candles.get_candles = lambda *args: frame.drop(frame.index[20])
    restored = UTEmaTickService(scanner2, checkpoint_dir=tmp_path)
    restored.process_observation("NIFTY", 998.1, at + pd.Timedelta(seconds=1), 2)
    assert restored.health["NIFTY_5min"]["status"] == "WAITING_HISTORY"
    assert adapter2._native._timeline is None
    assert (tmp_path/"NIFTY_5min.gap.json").exists()


def test_corrupt_journal_rebuild_discloses_missing_realtime_path(monkeypatch, tmp_path):
    scanner, _, _ = real_host(monkeypatch)
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path)
    at = pd.Timestamp("2026-10-29 12:35")
    for sequence, price in enumerate((998.05, 1000., 997.95), start=1):
        service.process_observation("NIFTY", price, at + pd.Timedelta(milliseconds=sequence), sequence)
    with next(tmp_path.glob("*.jsonl")).open("ab") as stream:
        stream.write(b'["2026-10-29T12:35:00.004",')
    scanner2, adapter2, _ = real_host(monkeypatch)
    restored = UTEmaTickService(scanner2, checkpoint_dir=tmp_path)
    restored.process_observation("NIFTY", 997.95, at + pd.Timedelta(milliseconds=5), 5)
    health = restored.health["NIFTY_5min"]
    assert health["historical_rebuild"]
    assert health["live_tick_path_restored"] is False
    assert health["missing_path_reason"] == "checkpoint_rejected"
    assert adapter2.stream_snapshot()["utema_live_path_available"] is False
    assert adapter2.stream_snapshot()["utema_executable_event_ids"] == []


def test_gap_marker_write_failure_keeps_runtime_overflow_closed(monkeypatch, tmp_path):
    scanner, adapter, _ = real_host(monkeypatch)
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path, capacity=1)
    at = pd.Timestamp("2026-10-29 12:35:01")
    service.process_observation("NIFTY", 998.05, at, 1)
    replace = __import__("os").replace

    def fail_marker(source, target):
        if str(target).endswith(".gap.json"):
            raise OSError("marker write unavailable")
        return replace(source, target)

    monkeypatch.setattr("engine.ut_ema_stream.os.replace", fail_marker)
    with service._condition:
        service._queue.append(("NIFTY", 1000., at + pd.Timedelta(milliseconds=1), 2))
    assert not service.submit("NIFTY", 997.95, at + pd.Timedelta(milliseconds=2))
    health = service.health["NIFTY_5min"]
    assert health["status"] == "OVERFLOW"
    assert health["durable_gap_saved"] is False
    assert "marker write unavailable" in health["persistence_error"]
    assert adapter.stream_snapshot()["utema_executable_event_ids"] == []
    service.close()
    checkpoint = json.loads((tmp_path/"NIFTY_5min.json").read_text())
    assert checkpoint["missing_path"]["requires_rebuild"]
