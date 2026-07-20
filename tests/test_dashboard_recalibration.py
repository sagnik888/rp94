import asyncio

from dashboard import server


class DummyLoop:
    def __init__(self):
        self.scheduled = []

    def is_running(self):
        return True

    def call_soon_threadsafe(self, callback, *args):
        callback(*args)

    def create_task(self, coro):
        self.scheduled.append(coro)


async def _dummy_coro():
    return "ok"


def test_recalibration_schedules_on_scanner_loop(monkeypatch):
    loop = DummyLoop()

    class DummyScanner:
        _event_loop = loop

    scanner = DummyScanner()
    monkeypatch.setattr(server, "scanner_ref", scanner)

    futures = []

    def fake_run_coroutine_threadsafe(coro, target_loop):
        futures.append((coro, target_loop))
        coro.close()

    monkeypatch.setattr(
        server.asyncio,
        "run_coroutine_threadsafe",
        fake_run_coroutine_threadsafe,
    )

    assert server._schedule_on_scanner_loop(_dummy_coro()) is True
    assert futures and futures[0][1] is loop
