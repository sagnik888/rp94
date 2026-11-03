"""Independent executable checks of the six resilience report claims.

Only dependency boundaries are stubbed. No live broker or running application is
mutated. Failing assertions are retained as reproductions of unresolved claims.
"""
import asyncio
import importlib.util
import queue
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from config.settings import get_settings
from data.market_data import MarketDataProvider
from engine.async_queue import AsyncExecutionQueue, execution_queue
from engine.signal_processor import SignalProcessor
from scanner import Scanner
from trading.order_retry import BrokerOrderAmbiguous


def _support(filename):
    spec = importlib.util.spec_from_file_location(filename, Path(__file__).with_name(filename + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_entry_support = _support("test_entry_submission_guard_2026_08_25")
_Broker, _manager, _open = _entry_support._Broker, _entry_support._manager, _entry_support._open
_exit_support = _support("test_resilience_recovery_regressions_2026_09_05")
_ExitBroker = _exit_support._ExitBroker
_live_position, _manager_and_trade = _exit_support._live_position, _exit_support._manager_and_trade


class BoundaryReached(BaseException):
    """Stop after the path under test, before unrelated application work."""


async def _heartbeat_during(action):
    ticks = []

    async def heartbeat():
        await asyncio.sleep(0.01)
        ticks.append(time.perf_counter())

    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    started = time.perf_counter()
    action()
    await task
    return ticks[0] - started


def test_slow_funds_preflight_does_not_delay_scan_heartbeat():
    entered = threading.Event()
    finished = threading.Event()

    class SlowBroker(_Broker):
        def preflight_entry_margin(self, **kwargs):
            entered.set()
            time.sleep(0.25)
            finished.set()
            return {"ok": False, "limitation": "simulated funds outage"}

    manager = _manager(SlowBroker())
    delay = asyncio.run(_heartbeat_during(lambda: _open(manager)))
    assert entered.wait(1), "the real queue never executed the funds check"
    assert finished.wait(1)
    execution_queue.task_queue.join()
    print(f"slow funds / heartbeat: {delay * 1000:.1f} ms")
    assert delay < 0.15


def test_slow_protection_is_deferred_by_trade_heartbeat():
    manager, trade = _manager_and_trade(_ExitBroker([]))
    trade.pending_exit = False
    trade.execution_status = "OPEN"
    manager._sync_wake = threading.Event()
    manager._dirty = threading.Event()
    protection = Mock(side_effect=lambda *a: time.sleep(0.25))
    manager.ensure_broker_protection = protection
    delay = asyncio.run(_heartbeat_during(lambda: manager.update_trade(
        trade.id, 25000, real_premium=100, quote_verified=False,
        allow_broker_protection=False,
    )))
    print(f"deferred protection / heartbeat: {delay * 1000:.1f} ms")
    assert manager._sync_wake.is_set()
    protection.assert_not_called()
    assert delay < 0.15


def test_saturated_execution_queue_does_not_delay_scan_heartbeat(monkeypatch):
    # All broker workers blocked by an outage; no free queue capacity remains.
    saturated = object.__new__(AsyncExecutionQueue)
    saturated._state_lock = threading.Lock()
    saturated._accepting = True
    saturated.task_queue = queue.Queue(maxsize=1)
    saturated.task_queue.put(("already waiting", None, (), {}))
    monkeypatch.setattr(execution_queue, "submit", saturated.submit)
    manager = _manager(_Broker())
    delay = asyncio.run(_heartbeat_during(lambda: _open(manager)))
    print(f"saturated queue / heartbeat: {delay * 1000:.1f} ms")
    assert delay < 0.15, f"full broker queue blocked scan heartbeat for {delay:.3f}s"


@pytest.mark.parametrize("outcome", [False, True, TimeoutError("socket write failed")])
def test_watchdog_retries_four_stalled_passes(monkeypatch, outcome):
    import main
    import data.kite_auth

    clock = [100.0]
    callbacks = []
    attempts = []

    def resubscribe():
        attempts.append(clock[0])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def advance(seconds):
        clock[0] += seconds
        if len(attempts) >= 4:
            raise BoundaryReached()

    monkeypatch.setenv("RUNTIME_WATCHDOG_ENABLED", "1")
    monkeypatch.setenv("RUNTIME_WATCHDOG_INTERVAL_SECONDS", "15")
    monkeypatch.setattr(main, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=advance))
    monkeypatch.setattr(main, "threading", SimpleNamespace(Thread=lambda **kw: SimpleNamespace(
        start=lambda: callbacks.append(kw["target"]))))
    monkeypatch.setattr(main, "send_desktop_notification", lambda *a: None)
    monkeypatch.setattr(main, "_apply_runtime_resource_guard", lambda *a: (False, ""))
    monkeypatch.setattr(data.kite_auth, "get_kite_auth", lambda: SimpleNamespace(status=lambda: {}))
    scanner = SimpleNamespace(is_running=True, auto_mode=True, log_event=lambda *a: None,
        data=SimpleNamespace(get_source_health=lambda: {"kite_stream_stalled": True},
            resubscribe_kite_tokens=resubscribe, get_fyers_auth_status=lambda: {}))
    main.start_runtime_watchdog(scanner, SimpleNamespace(dashboard_host="localhost", dashboard_port=8500))
    with pytest.raises(BoundaryReached):
        callbacks[0]()
    assert attempts == [100, 115, 130, 145]


@pytest.mark.parametrize("failure", ["analyze", "after_analyze"])
def test_failed_intelligence_preserves_old_bundle_age(monkeypatch, failure):
    scanner = Scanner.__new__(Scanner)
    frame = pd.DataFrame({"open": [100.0]*40, "high": [102.0]*40,
        "low": [99.0]*40, "close": [101.0]*40, "volume": [100.0]*40},
        index=pd.date_range("2026-09-04 09:15", periods=40, freq="5min"))
    scanner.mode = "REAL"
    scanner.user_lots = {}
    scanner.capital_fut = scanner.capital_opt = 100000
    scanner.risk_fut_pct = 1
    scanner.scan_count = 1
    scanner.inst_pref = "OPT"
    scanner._cached_results = {}
    scanner._cached_result_metadata = {}
    scanner._chain_cache = {}
    scanner._candidate_process_cache = {}
    scanner._intel_cache = {}
    scanner.latest_regimes = {}
    scanner._last_vol_alert_time = {}
    scanner.data = SimpleNamespace(get_index_quote=lambda *a: {"ltp": 101, "previous_close": 100})
    scanner.candles = SimpleNamespace(update_latest_price=lambda *a: None,
        get_candles=lambda *a: frame, get_latest_price=lambda *a: 101)
    mtf = SimpleNamespace(results_1min=None, results_5min=None, results_15min=None)
    scanner.mtf = SimpleNamespace(update_data=lambda *a: None, process_instrument=lambda *a: mtf)
    for name, value in {
        "_get_option_chain_cached": (None, {}), "_get_depth_context": {},
        "_get_live_volatility_context": {}, "_previous_close_for": 100,
        "_days_to_expiry_for": 4, "_build_intelligence_contexts": {},
        "_instrument_iv_observations": [], "_live_regime_horizons": {},
        "_build_pre_candle_predictions": {}, "_get_timeframe_intel": {},
        "_record_bar_aligned_intelligence": None, "_mtf_has_forming_signal": False,
    }.items():
        setattr(scanner, name, Mock(return_value=value))
    scanner._prepare_live_signal_data = lambda name, tf, data: data
    scanner._live_analysis_window = lambda data, tf: data
    scanner._regime_history_ready = lambda data: True
    scanner._intel_analysis_ttl_seconds = lambda: 30
    scanner._intel_result_cacheable = lambda data: bool(data)
    old = {"aggregate": {"score_norm": 0.5}, "regime": {"regime": "TRENDING_UP"}}
    captured = time.time() - 300
    scanner._cache_timeframe_intel("NIFTY", "5min", old, at=captured)
    fresh = {"aggregate": {"score_norm": 0.6}, "regime": {"regime": "TRENDING_UP"}}
    scanner.intel = SimpleNamespace(analyze=Mock(
        side_effect=TimeoutError("analysis failed") if failure == "analyze" else None,
        return_value=fresh))
    scanner._record_instrument_iv = Mock(side_effect=RuntimeError("IV recording failed"))
    scanner.intel_memory = SimpleNamespace(record=Mock())
    scanner.signal_processor = SimpleNamespace(process_best_signal=Mock(side_effect=BoundaryReached))
    with pytest.raises(BoundaryReached):
        scanner._process_instrument("NIFTY", {"strike_interval": 50, "lot_size": 50})
    delivered = scanner.signal_processor.process_best_signal.call_args.args[2]
    assert time.time() - delivered["as_of"] >= 300
    assert old["as_of"] == captured


@pytest.mark.parametrize("fire_mode", ["INTRACANDLE", "CANDLE_CLOSE"])
def test_held_signal_is_graded_when_intelligence_returns(monkeypatch, fire_mode):
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime")
    import engine.signal_processor as processor_module
    from trading.trade_manager import IST

    now = [datetime(2026, 9, 4, 10, 5, 10)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return IST.localize(now[0]) if tz else now[0]

    monkeypatch.setattr(processor_module, "datetime", Clock)
    monkeypatch.setattr(type(get_settings()), "get_signal_fire_mode", lambda self: fire_mode)
    settings = get_settings()
    settings.own_timeframe_intel_fail_closed = True
    sig = SimpleNamespace(instrument="NIFTY", timestamp=datetime(2026, 9, 4, 10),
        signal_type="BUY", price=25000, trailing_stop=24950, adx_value=25)
    scanner = SimpleNamespace(mode="REAL", risk_manager=SimpleNamespace(can_trade=lambda *a: True),
        _last_signal_time={"NIFTY_5min": sig.timestamp - timedelta(seconds=1)},
        _vix_data={sig.timestamp.strftime("%Y-%m-%d %H:%M:00"): 15},
        _detect_volume_spike_entry=lambda *a, **kw: None,
        log_event=lambda *a: None, log_signal_decision_once=lambda *a: None,
        signals=SimpleNamespace(_grade_signal=Mock(side_effect=BoundaryReached)))
    processor = SignalProcessor.__new__(SignalProcessor)
    processor.scanner = scanner
    contexts = {}
    processor._build_timeframe_contexts = lambda *a: contexts
    processor._append_adaptive_dual_apex_candidates = lambda **kw: None
    mtf = SimpleNamespace(results_5min={"signals": [sig]}, results_15min=None, confluence_score=1)
    args = ("NIFTY", mtf, {}, 0.5, "TRENDING_UP", 1, 50, {}, 25000, 25000)
    assert processor.process_best_signal(*args) == []
    assert scanner._live_signal_recheck_eligible["NIFTY_5min"]
    contexts["5min"] = {"intel_result": {}, "intel_score": 0.5, "regime": "TRENDING_UP"}
    now[0] += timedelta(seconds=10)
    with pytest.raises(BoundaryReached):
        processor.process_best_signal(*args)


def _provider_and_future(age):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._ws_cache = {}
    provider._ltp_cache = {"NIFTY26SEPFUT": {"ltp": 25100, "time": time.time() - age}}
    provider.kite = SimpleNamespace(is_connected=False)
    provider.fyers = None
    provider.is_connected = False
    provider.smart_api = None
    scanner = Scanner.__new__(Scanner)
    scanner.data = provider
    scanner._candidate_ltp_cache = {}
    trade = SimpleNamespace(symbol_token="123", trading_symbol="NIFTY26SEPFUT")
    return scanner, trade


def test_futures_provider_fallback_cannot_make_hour_old_price_fresh():
    scanner, trade = _provider_and_future(3600)
    mark = scanner._get_live_fut_ltp_cached(trade)
    assert mark is None, f"hour-old provider quote returned {mark} and was cached as new"


def test_fast_futures_path_reads_a_fresh_websocket_contract_quote():
    scanner, trade = _provider_and_future(3600)
    scanner.data._ws_cache["123"] = {"ltp": 25200, "time": time.time()}
    assert scanner._get_live_fut_ltp_cached(trade, allow_blocking=False) == 25200


@pytest.mark.parametrize("contract_price", [25100, None])
def test_missing_spot_checks_fresh_futures_mark_before_skipping(contract_price):
    scanner = Scanner.__new__(Scanner)
    scanner._reset_daily_counters = lambda: None
    scanner.candles = SimpleNamespace(get_latest_price=lambda *a: None)
    trade = SimpleNamespace(inst_type="FUT", instrument="NIFTY", status="OPEN", pending_exit=False,
        execution_status="OPEN", entry_spot=25000, entry_price=25050)
    scanner.trades = SimpleNamespace(open_trades={"T1": trade})
    scanner._get_live_fut_ltp_cached = lambda *a, **kw: contract_price
    scanner._get_trade_regime = Mock(side_effect=BoundaryReached)
    if contract_price:
        with pytest.raises(BoundaryReached):
            scanner._update_active_trades(allow_blocking=False)
        assert trade.current_price == contract_price
    else:
        scanner._update_active_trades(allow_blocking=False)
        scanner._get_trade_regime.assert_not_called()


def test_reconnected_ambiguous_recovery_exit_is_adopted_not_duplicated():
    class LostAckBroker(_ExitBroker):
        readable = False

        def inspect_order_by_tag(self, tag):
            return super().inspect_order_by_tag(tag) if self.readable else {"state": "UNKNOWN"}

        def close_order(self, *args, **kwargs):
            self.exits.append((args, kwargs))
            self.book.append({"tag": kwargs.get("tag"), "orderid": "RECOVERY-LIVE"})
            if len(self.exits) == 1:
                raise BrokerOrderAmbiguous("exchange accepted; acknowledgement lost")
            return "DUPLICATE-EXIT"

    broker = LostAckBroker([])
    manager, trade = _manager_and_trade(broker)
    manager._dirty = threading.Event()
    assert manager.emergency_flatten_trade(trade, "SIMULATED_EOD") is False
    assert len(broker.exits) == 1
    broker.readable = True
    manager.reconcile_broker_positions(_live_position())
    assert len(broker.exits) == 1, "reconnection submitted a second exit while RECOVERY_EXIT was live"
    assert trade.exit_order_id == "RECOVERY-LIVE"
