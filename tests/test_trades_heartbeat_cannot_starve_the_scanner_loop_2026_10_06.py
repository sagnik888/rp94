"""The dashboard trades heartbeat starved the scanner loop on 2026-10-06.

Live process PID 35468 (started 12:57:37 IST, REAL mode, dashboard open, no positions):

    ERROR    | Error in periodic trades updater: TimeoutError: (no message)   ~7 per minute
    CRITICAL | WATCHDOG: no scan cycle for 231s (threshold 180s)            13:12:06
    WARNING  | High Latency: 166932ms (scan phase 165981ms)                  13:16:32

py-spy (--threads --idle, 13:07:31-13:11:52) had the ScannerEventLoop thread busy in
2852 of 2854 samples, 2737 of them (96%) inside the heartbeat's own request, while the
ScannerLive analysis pool sat idle:

    periodic_trades_updater -> _call_on_scanner_loop(build_trades_payload, 5s)
      -> _build_fresh_trades_payload
           build + serialize the trades                1208
           _get_diagnostics_payload for the cache       842  (runtime-build stat()s 543)
           make_serializable(cached dashboard state)    342
           make_serializable(the same trades) again     326

Two defects compounded:

1. Every 1s heartbeat rebuilt the WHOLE cached dashboard state (diagnostics, config and a
   second serialization of the trades) on the scanner loop just to refresh the trades
   inside it. The scan cycle republishes all of that itself on every cycle.
2. A build that overran the 5s wait was thrown away, and 1s later the next one was queued
   behind it, so the scanner loop ran heartbeat builds back to back and every scan-cycle
   step waited behind one.
"""
import asyncio
import contextlib
import threading
import time

import dashboard.server as server
from config.settings import get_settings
from engine.scanner_dashboard import ScannerDashboardCache


def _trades(pnl=0.0, tag="fresh"):
    return {
        "open": [],
        "closed": [{"id": tag, "pnl": pnl}],
        "signals": [],
        "summary": {
            "total_trades": 1,
            "open_count": 0,
            "wins": 1,
            "losses": 0,
            "win_rate": 100.0,
            "daily_pnl": pnl,
        },
        "meta": {"mode": "REAL"},
    }


class _LiveScanner:
    mode = "REAL"
    simulation_id = 7
    latest_results = {}

    def __init__(self, build_seconds=0.0):
        self.dashboard_cache = ScannerDashboardCache()
        self.build_seconds = build_seconds
        self.trade_builds = 0
        self.diagnostics_builds = 0
        self.config_builds = 0
        self.build_windows = []

    def _build_dashboard_trade_payload(self):
        started = time.monotonic()
        if self.build_seconds:
            # Holds the scanner loop, like the live build did.
            time.sleep(self.build_seconds)
        self.trade_builds += 1
        self.build_windows.append((started, time.monotonic()))
        return _trades(pnl=float(self.trade_builds), tag=f"build-{self.trade_builds}")

    def _get_diagnostics_payload(self):
        self.diagnostics_builds += 1
        return {"latency": {"last_ms": 12}}

    def get_broadcast_config(self):
        self.config_builds += 1
        return {"scanner_engine": "quad_prime_s1"}

    def _sync_engine_lab_active_row(self, payload, trade_summary=None):
        from scanner import Scanner

        return Scanner._sync_engine_lab_active_row(self, payload, trade_summary)


def _published_scan_state():
    return {
        "mode": "REAL",
        "simulation_id": 7,
        "instruments": {"NIFTY": {"ltp": 22711.2}},
        "diagnostics": {"latency": {"last_ms": 99}},
        "config": {"scanner_engine": "quad_prime_s1"},
        "engine_lab": {
            "active_engine": "quad_prime_s1",
            "rows": [
                {"engine": "quad_prime_s1", "active": True, "signals": 0, "total_money": 0.0},
                {"engine": "ut_smart_s1", "active": False, "signals": 4, "total_money": -5.0},
            ],
        },
        "trades": _trades(tag="scan"),
    }


def test_trades_refresh_republishes_only_the_trades(monkeypatch):
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime_s1")
    scanner = _LiveScanner()
    scanner.dashboard_cache.update(_published_scan_state())
    monkeypatch.setattr(server, "scanner_ref", scanner)

    payload = server.build_trades_payload()

    assert scanner.trade_builds == 1
    assert scanner.diagnostics_builds == 0, "the trades heartbeat rebuilt the diagnostics"
    assert scanner.config_builds == 0, "the trades heartbeat rebuilt the broadcast config"
    state = scanner.dashboard_cache.state()
    assert state["trades"] == payload
    assert scanner.dashboard_cache.trades() == payload
    # Everything the scan cycle published is kept as it was.
    assert state["diagnostics"] == {"latency": {"last_ms": 99}}
    assert state["instruments"] == {"NIFTY": {"ltp": 22711.2}}
    assert state["config"] == {"scanner_engine": "quad_prime_s1"}
    # The Engine Lab row derived from the trades follows them, as before.
    rows = {row["engine"]: row for row in state["engine_lab"]["rows"]}
    assert rows["quad_prime_s1"]["total_money"] == 1.0
    assert rows["ut_smart_s1"]["total_money"] == -5.0


def test_trades_refresh_leaves_the_previous_generation_untouched(monkeypatch):
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime_s1")
    scanner = _LiveScanner()
    scanner.dashboard_cache.update(_published_scan_state())
    previous = scanner.dashboard_cache.state()
    monkeypatch.setattr(server, "scanner_ref", scanner)

    server.build_trades_payload()

    # A dashboard request may still be serializing the old generation on another thread.
    assert previous["trades"]["closed"][0]["id"] == "scan"
    old_rows = {row["engine"]: row for row in previous["engine_lab"]["rows"]}
    assert old_rows["quad_prime_s1"]["total_money"] == 0.0


def test_trades_refresh_before_the_first_scan_still_publishes_a_full_state(monkeypatch):
    scanner = _LiveScanner()
    monkeypatch.setattr(server, "scanner_ref", scanner)

    async def build():
        payload = server.build_trades_payload()
        await asyncio.sleep(0.05)  # the full publish is scheduled on the (current) loop
        return payload

    payload = asyncio.run(build())

    state = scanner.dashboard_cache.state()
    assert state["trades"] == payload
    assert state["diagnostics"] == {"latency": {"last_ms": 12}}
    assert state["config"] == {"scanner_engine": "quad_prime_s1"}
    assert state["mode"] == "REAL"
    assert state["simulation_id"] == 7


def test_trades_refresh_rebuilds_a_state_left_by_another_mode(monkeypatch):
    scanner = _LiveScanner()
    stale = _published_scan_state()
    stale["mode"] = "HISTORICAL"
    scanner.dashboard_cache.update(stale)
    monkeypatch.setattr(server, "scanner_ref", scanner)

    async def build():
        payload = server.build_trades_payload()
        await asyncio.sleep(0.05)
        return payload

    payload = asyncio.run(build())

    assert scanner.diagnostics_builds == 1
    state = scanner.dashboard_cache.state()
    assert state["mode"] == "REAL"
    assert state["trades"] == payload


# --- the heartbeat against a real scanner loop thread ---------------------------------


class _ScannerLoopThread:
    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name="ScannerEventLoop", daemon=True
        )
        self.thread.start()
        return self.loop

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        self.loop.close()


def _run_heartbeat(monkeypatch, scanner, seconds, *, during=None):
    sent = []

    async def record_broadcast(data):
        sent.append((time.monotonic(), data))

    monkeypatch.setattr(server, "broadcast", record_broadcast)
    monkeypatch.setattr(server, "active_connections", {object()})
    monkeypatch.setattr(server, "_last_periodic_trades_hash", None)
    monkeypatch.setattr(server, "_trade_dirty_event", None)
    monkeypatch.setattr(server, "_trade_dirty_loop", None)
    monkeypatch.setattr(server, "scanner_ref", scanner)

    async def drive():
        task = asyncio.create_task(server.periodic_trades_updater())
        started = time.monotonic()
        if during is not None:
            await during(started)
        await asyncio.sleep(max(0.0, seconds - (time.monotonic() - started)))
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return started, time.monotonic()

    with _ScannerLoopThread() as loop:
        scanner._event_loop = loop
        started, ended = asyncio.run(drive())
    return sent, started, ended


def _loop_share(scanner, started, ended):
    busy = sum(
        max(0.0, min(end, ended) - max(start, started))
        for start, end in scanner.build_windows
    )
    return busy / (ended - started)


def test_heartbeat_paces_itself_by_its_build_time(monkeypatch):
    scanner = _LiveScanner(build_seconds=0.5)

    sent, started, ended = _run_heartbeat(monkeypatch, scanner, 6.0)

    assert len(sent) >= 2
    # 0.5s builds on a 1s poll held the scanner loop a third of the time.
    assert _loop_share(scanner, started, ended) <= 0.25


def test_an_overrunning_build_is_awaited_not_discarded_and_requeued(monkeypatch):
    scanner = _LiveScanner(build_seconds=1.0)
    # Shrink the heartbeat's wait so a 1s build overruns it, as the live builds overran
    # 5s. Both spellings of that budget are shrunk so the scenario is the same either way.
    monkeypatch.setattr(server, "_PERIODIC_TRADES_WAIT_SECONDS", 0.25, raising=False)
    original = server._call_on_scanner_loop

    async def short_budget(func, *, timeout_seconds=15.0):
        return await original(func, timeout_seconds=min(timeout_seconds, 0.25))

    monkeypatch.setattr(server, "_call_on_scanner_loop", short_budget)

    sent, started, ended = _run_heartbeat(monkeypatch, scanner, 5.0)

    assert sent, "every overrunning build was thrown away"
    assert sent[0][1]["trades"]["closed"][0]["id"] == "build-1"
    assert _loop_share(scanner, started, ended) <= 0.35
    gaps = [
        later_start - earlier_end
        for (_, earlier_end), (later_start, _) in zip(scanner.build_windows, scanner.build_windows[1:])
    ]
    assert all(gap >= 1.0 for gap in gaps), gaps


def test_an_entry_or_exit_poke_still_refreshes_immediately(monkeypatch):
    scanner = _LiveScanner(build_seconds=0.5)
    poked = []

    async def poke_after_first_build(started):
        while scanner.trade_builds < 1:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.3)  # inside the pacing window that follows the build
        poked.append(time.monotonic())
        server.notify_trade_change()

    sent, started, ended = _run_heartbeat(
        monkeypatch, scanner, 3.0, during=poke_after_first_build
    )

    assert poked
    later_starts = [start for start, _ in scanner.build_windows if start >= poked[0]]
    assert later_starts and later_starts[0] - poked[0] < 0.3


def test_a_poke_during_a_build_is_not_lost(monkeypatch):
    scanner = _LiveScanner(build_seconds=0.5)
    poked = []

    async def poke_mid_build(started):
        # The first build starts after the 1s poll and runs 0.5s.
        await asyncio.sleep(1.2)
        poked.append(time.monotonic())
        server.notify_trade_change()

    sent, started, ended = _run_heartbeat(monkeypatch, scanner, 3.0, during=poke_mid_build)

    assert poked
    first_end = scanner.build_windows[0][1]
    assert scanner.build_windows[0][0] < poked[0] < first_end, scanner.build_windows
    assert len(scanner.build_windows) >= 2
    assert scanner.build_windows[1][0] - first_end < 0.3
