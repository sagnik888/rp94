"""The 100ms trade monitor must not make an HTTPS call on the scanner event loop.

_background_hft_worker calls _update_active_trades() synchronously every 100ms ON the
scanner's asyncio loop. _get_live_premium_cached falls through to two blocking calls when
the websocket cache misses -- a Fyers option chain and a Kite REST LTP -- and the premium
cache TTL is 2s, so per open OPT trade with a stale contract quote those fired every
couple of seconds on the loop that also runs the 1s scan cycle AND services every
dashboard _call_on_scanner_loop.

Observed live on 2026-08-05: repeated `api_chart -> _call_on_scanner_loop -> TimeoutError`
after the 5s ceiling, plus "Error in periodic trades updater: TimeoutError". This is the
400ms -> 5000ms regression pattern returning by a different route.

The fix is the one already used for the index-volume refresh: memory-only on the hot path,
HTTPS dispatched off-thread, throttled per token, stamped only once dispatch is certain.
"""
import threading
import time

import pytest

from scanner import Scanner


class _Data:
    def __init__(self, latency=0.0):
        self.latency = latency
        self.blocking_calls = []

    def get_cached_contract_ltp(self, exchange, symbol, token, max_age_seconds=10.0):
        return 0.0                      # force the fallback path

    def subscribe_tokens(self, tokens, exchange_type=2):
        return None

    def get_ltp(self, exchange, symbol, token):
        # Record the THREAD, not just the call. A refresh running on a worker thread is
        # exactly what we want; the same call on the caller's thread is the stall.
        self.blocking_calls.append((threading.current_thread().name, symbol))
        time.sleep(self.latency)
        return 210.0


class _Trade:
    def __init__(self):
        self.symbol_token = "16858626"
        self.trading_symbol = "NIFTY2680524500CE"
        self.instrument = "NIFTY"
        self.entry_price = 200.0
        self.inst_type = "OPT"
        self.id = "TH1"


def _scanner(latency=0.0):
    s = Scanner.__new__(Scanner)
    s.data = _Data(latency)
    s._premium_cache = {}
    s._premium_refresh_guard = threading.Lock()
    s._premium_refresh_inflight = set()
    s._premium_refresh_at = {}
    s.market_info = {}
    s._option_premium_quote_plausible = lambda t, p: True
    s._contract_quote_age_seconds = lambda t, n: 0.0
    s._get_option_chain_premium = lambda t, max_age_seconds=10.0: 0.0
    s._log_stale_option_premium_once = lambda *a, **k: None
    s._log_bad_option_premium_once = lambda *a, **k: None
    return s


def test_the_hot_path_makes_no_blocking_call_on_its_own_thread():
    """This is the whole point: no HTTPS on the thread running the event loop.

    A refresh on a worker thread is the intended behaviour, so the assertion is about
    WHICH thread made the call, not whether one happened at all.
    """
    scanner = _scanner()
    caller = threading.current_thread().name
    result = scanner._get_live_premium_cached(_Trade(), allow_blocking=False)
    assert result is None, "returned a premium it could only have blocked to obtain"
    time.sleep(0.4)
    on_caller = [c for c in scanner.data.blocking_calls if c[0] == caller]
    assert on_caller == [], (
        f"made {len(on_caller)} blocking call(s) on the scanner loop thread"
    )
    assert scanner.data.blocking_calls, "no refresh happened at all"


def test_the_hot_path_returns_promptly_even_with_a_slow_broker():
    """A 2s Fyers round-trip must not become a 2s stall on the event loop."""
    scanner = _scanner(latency=2.0)
    started = time.monotonic()
    scanner._get_live_premium_cached(_Trade(), allow_blocking=False)
    elapsed = time.monotonic() - started
    assert elapsed < 0.2, f"hot path took {elapsed:.2f}s — that is a loop stall"


def test_the_hot_path_dispatches_a_refresh_off_thread():
    scanner = _scanner(latency=0.1)
    trade = _Trade()
    scanner._get_live_premium_cached(trade, allow_blocking=False)
    time.sleep(0.5)
    assert scanner.data.blocking_calls, "no refresh was ever dispatched"
    assert scanner._premium_cache.get(trade.symbol_token), (
        "the off-thread refresh did not populate the cache the hot path reads"
    )


def test_the_refreshed_premium_is_served_from_cache_next_pass():
    scanner = _scanner(latency=0.1)
    trade = _Trade()
    scanner._get_live_premium_cached(trade, allow_blocking=False)
    time.sleep(0.5)
    before = len(scanner.data.blocking_calls)
    assert scanner._get_live_premium_cached(trade, allow_blocking=False) == 210.0
    assert len(scanner.data.blocking_calls) == before, "hit the broker again on a cache hit"


def test_one_contract_is_not_refreshed_concurrently():
    scanner = _scanner(latency=0.3)
    trade = _Trade()
    for _ in range(6):
        scanner._get_live_premium_cached(trade, allow_blocking=False)
    time.sleep(0.8)
    assert len(scanner.data.blocking_calls) == 1, (
        f"{len(scanner.data.blocking_calls)} concurrent refreshes for one contract"
    )


def test_a_failing_refresh_releases_its_slot():
    """Otherwise one exception blocks that contract from ever refreshing again."""
    scanner = _scanner()

    def boom(exchange, symbol, token):
        raise RuntimeError("fyers down")

    scanner.data.get_ltp = boom
    trade = _Trade()
    scanner._get_live_premium_cached(trade, allow_blocking=False)
    time.sleep(0.4)
    assert trade.symbol_token not in scanner._premium_refresh_inflight


def test_the_blocking_path_still_works_for_the_scan_cycle():
    """The 1s scan keeps the authoritative fetch; only the 100ms path is restricted."""
    scanner = _scanner()
    assert scanner._get_live_premium_cached(_Trade(), allow_blocking=True) == 210.0
    assert scanner.data.blocking_calls, "the authoritative path stopped fetching"


def test_the_hft_worker_asks_for_the_non_blocking_variant():
    """Pins the call site — the flag is worthless if the worker does not pass it."""
    import inspect

    src = inspect.getsource(Scanner._background_hft_worker)
    assert "_run_exit_tick()" in src, (
        "the 100ms worker is back to the blocking call"
    )


# ── the 1s scan cycle is on the same loop and had the same problem ────────────

def test_the_scan_cycle_does_not_block_the_loop_either():
    """The 100ms fix protected one caller and left the other on the same loop.

    _scan_cycle is `async def` and called self._update_active_trades() with
    allow_blocking defaulting to True, so the identical blocking Fyers/Kite chain still
    ran on the scanner event loop once a second. Today's log carries 39 "Error in
    periodic trades updater" TimeoutErrors clustered through live hours — that updater
    budgets 5s on _call_on_scanner_loop, so the loop was blocked past five seconds, 39
    times, while the market was open. A blocked loop is not scanning and not checking
    exits.

    Nothing is lost: _premium_refresh_worker does the authoritative fetch on a daemon
    thread and fills the cache both callers read.
    """
    import ast
    import inspect
    import textwrap

    from scanner import Scanner

    tree = ast.parse(textwrap.dedent(inspect.getsource(Scanner._scan_cycle)))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_run_exit_tick"
    ]
    assert calls, "_scan_cycle no longer updates active trades at all"
    from engine.exit_cycle import ExitCycleService
    tick = inspect.getsource(ExitCycleService.run_tick)
    assert "update_active_trades(allow_blocking=False)" in tick


def test_the_background_refresher_still_does_the_blocking_fetch():
    """Dropping the blocking call is only safe because this one keeps it."""
    import inspect

    from scanner import Scanner

    src = inspect.getsource(Scanner._premium_refresh_worker)
    assert "allow_blocking=True" in src, (
        "nothing does the authoritative premium fetch any more"
    )


# ── the flight recorder must not reintroduce the same stall ──────────────────
#
# 2026-08-30 gave TradeManager._record_trade_forensics its first caller, in
# update_trade() -- which runs on the scanner's asyncio loop, the same loop as the 1s
# scan cycle, the 100ms trade monitor, and every dashboard _call_on_scanner_loop. Its
# sqlite write takes db.write_lock, so calling it inline is a lock-contended commit on
# the hot path: exactly the shape this file exists to prevent, and whose visible symptom
# is a stream of "Error in periodic trades updater: TimeoutError" once
# _call_on_scanner_loop can no longer be serviced inside its 5s budget.
#
# Its sibling, Scanner._emit_forensic_event, already states the rule in its docstring:
# "DB writes are pushed off the event loop so a slow commit cannot stall the scan cycle."

class _ForensicTrade:
    id = "T-FORENSIC"
    is_ghost = False
    instrument = "NIFTY"
    timeframe = "5min"
    direction = "LONG"
    status = "OPEN"
    entry_price = 100.0
    current_price = 110.0
    lots = 1
    lot_size = 65
    pnl = 650.0
    peak_pnl = 650.0
    max_drawdown = 0.0
    trading_symbol = "NIFTY26SEP24500CE"
    lifecycle_id = "LC-FORENSIC"
    broker_order_id = ""
    exit_order_id = ""
    inst_type = "OPT"


def _manager_recording_forensics(monkeypatch, landed):
    import data.sqlite_db as sqlite_db
    from trading.trade_manager import TradeManager

    monkeypatch.setattr(
        sqlite_db.db,
        "append_signal_lifecycle_events",
        lambda events: landed.append(threading.current_thread().name) or 0,
    )
    manager = TradeManager.__new__(TradeManager)
    manager.mode = "REAL"
    return manager


def test_the_forensics_write_never_runs_on_the_callers_thread(monkeypatch):
    """update_trade runs on the scanner loop; a sqlite commit there is the stall."""
    landed = []
    manager = _manager_recording_forensics(monkeypatch, landed)
    caller = threading.current_thread().name

    manager._record_trade_forensics((_ForensicTrade(),))

    deadline = time.time() + 5.0
    while not landed and time.time() < deadline:
        time.sleep(0.02)

    assert landed, "the forensics write never happened -- dispatch is silently failing"
    assert landed[0] != caller, (
        f"the sqlite write ran on the caller's thread ({caller})"
    )


def test_the_forensics_write_is_dispatched_off_a_running_event_loop(monkeypatch):
    """The live shape: called from inside the scanner's loop."""
    import asyncio

    landed = []
    manager = _manager_recording_forensics(monkeypatch, landed)

    async def drive():
        manager._record_trade_forensics((_ForensicTrade(),))
        for _ in range(100):
            if landed:
                break
            await asyncio.sleep(0.02)
        return threading.current_thread().name

    loop_thread = asyncio.run(drive())

    assert landed, "nothing was written from inside a running loop"
    assert landed[0] != loop_thread, "the commit ran on the event loop itself"


def test_a_failed_dispatch_does_not_silently_drop_every_event(monkeypatch):
    """asyncio was not imported in this module when the caller was added, so the
    NameError fell through to a bare `except Exception: pass` and nothing was ever
    written -- a recorder that looks wired but records nothing."""
    import trading.trade_manager as trade_manager

    assert hasattr(trade_manager, "asyncio"), "asyncio is not importable in this module"
    assert hasattr(trade_manager, "threading")
