"""Regression proof for replay scalar access and worker ledger lock stalls."""
import asyncio
import threading

import numpy as np
import pandas as pd
import pytest

from engine.ut_ema_engine import Params, Timeline, UTEmaEngine, compute_indicators


@pytest.mark.parametrize("mode", ["Signal Bar Close", "Realtime Intrabar", "Next Bar Open"])
@pytest.mark.parametrize("heikin", [False, True])
@pytest.mark.parametrize("timeframe", ["1min", "5min", "15min"])
def test_historical_replay_uses_array_rows(monkeypatch, mode, heikin, timeframe):
    close = 22000. + np.sin(np.arange(180)/3.)*100.
    frame = pd.DataFrame(dict(open=close, high=close+10., low=close-10., close=close),
                         index=pd.date_range("2026-10-29 09:15", periods=180, freq="min"))
    period = pd.Timedelta(minutes=int(timeframe.replace("min", "")))
    frame.index = pd.date_range("2026-10-29 09:15", periods=len(frame), freq=period, tz="Asia/Kolkata")
    params = Params(execution_mode=mode, ut_heikin=heikin)
    frame = UTEmaEngine.prepare(frame)
    indicators = compute_indicators(frame, params)
    reference = Timeline(params, "NIFTY", timeframe)
    for i in range(len(frame)):
        reference.step(i, frame, indicators, observed_at=frame.index[i]+period)
    expected = UTEmaEngine(params).result(reference, frame, indicators, len(frame))
    def forbidden(*args, **kwargs):
        raise AssertionError("Full replay must not perform pandas scalar access per bar")
    monkeypatch.setattr(Timeline, "step", forbidden)
    actual = UTEmaEngine(params).process(frame, "NIFTY", timeframe, realtime=False,
                                        as_of=frame.index[-1]+period)
    for key in ("utema_events", "utema_records", "state", "indicator_overlays", "positions", "native_markers"):
        assert actual[key] == expected[key]


def test_worker_ledger_save_releases_scan_lock(monkeypatch):
    from scanner import Scanner
    from tests.test_scanner_modules import _session_persist_scanner
    scanner = _session_persist_scanner({"id": "entry", "action": "ENTRY", "nested": {"stop": 12.}})
    write_started = threading.Event()
    release_write = threading.Event()
    writes = []
    def save(day, rows):
        writes.append(rows)
        write_started.set()
        assert release_write.wait(3.)
    monkeypatch.setattr(scanner, "_save_session_signal_rows", save)
    async def exercise():
        scanner._event_loop = asyncio.get_running_loop()
        worker = threading.Thread(target=scanner._persist_session_trade_candidates)
        worker.start()
        try:
            assert await asyncio.to_thread(write_started.wait, 2.)
            assert await asyncio.to_thread(lambda: (worker.join(.2), not worker.is_alive())[1])
            assert scanner._session_breach_lock.acquire(timeout=.2)
            scanner._session_breach_lock.release()
            scanner._session_trade_candidates["NIFTY"]["row"]["nested"]["stop"] = 15.
            assert writes[0][0]["nested"]["stop"] == 12.
        finally:
            release_write.set()
            await asyncio.to_thread(worker.join, 3.)
            task = getattr(scanner, "_session_persist_task", None)
            if task:
                await task
        await Scanner.flush_session_trade_candidates(scanner)
        assert writes[-1][0]["nested"]["stop"] == 15.
    asyncio.run(exercise())


def test_delayed_worker_snapshot_cannot_overwrite_newer_shutdown_snapshot():
    from tests.test_scanner_modules import _session_persist_scanner
    scanner = _session_persist_scanner({"id": "entry"})
    writes = []
    scanner._save_session_signal_rows = lambda day, rows: writes.append(rows)
    async def exercise():
        scanner._queue_session_signal_rows("2026-09-03", [{"action": "EXIT"}], 2)
        scanner._queue_session_signal_rows("2026-09-03", [{"action": "ENTRY"}], 1)
        await scanner._session_persist_task
    asyncio.run(exercise())
    assert writes == [[{"action": "EXIT"}]]
