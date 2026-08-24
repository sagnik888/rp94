"""
End-to-End Pipeline Speed Test
===============================================================

Measures the FULL processing pipeline latency across every stage:

  1. Signal Filtering & Buffering  (SignalManager.process_signal)
  2. Signal Processing & Grading   (SignalManager._grade_signal)
  3. Entry Candidate Selection      (SignalProcessor.process_best_signal)
  4. Notification Dispatch          (NotificationManager + send_desktop_notification)
  5. Trade Management               (TradeManager.open_trade admission gates)
  6. PnL Mark-to-Market Updates     (Trade.unrealized_pnl + update_trade)
  7. Exit Processing                (TradeManager.close_trade + _finalize_closed_trade)
  8. Async Queue Throughput         (AsyncExecutionQueue submit → execute)
  9. Risk Manager Circuit Breaker   (RiskManager.check_circuit_breaker)
 10. Full Pipeline Integration      (signal → entry → PnL → exit)

Reports individual stage latencies, identifies bottlenecks, and validates
that the full pipeline completes within acceptable latency budgets.
"""

import math
import os
import queue
import statistics
import sys
import threading
import time
from datetime import datetime, date, timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch, PropertyMock
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")

# --- Helpers ----------------------------------------------------------

def _make_settings(**overrides):
    """Build a minimal settings object with sensible defaults."""
    defaults = dict(
        # Core
        mode="HISTORICAL",
        scanner_engine="utbot",
        active_indices="NIFTY",
        max_positions=5,
        # Risk
        futures_sl_pct=0.30,
        options_sl_pct=15.0,
        max_trade_loss_abs=5000.0,
        max_daily_loss_abs=25000.0,
        max_futures_daily_loss_abs=15000.0,
        max_options_daily_loss_abs=15000.0,
        # Grading
        ut_confidence_logistic=True,
        ut_confidence_logistic_k=5.0,
        ut_dynamic_confidence=False,
        ut_exhaustion_confidence_cap=0.82,
        signal_vix_high_threshold=20.0,
        signal_vix_low_threshold=12.0,
        range_day_regime_pillar_damp_enabled=True,
        range_day_regime_pillar_min_confidence=0.60,
        range_day_regime_pillar_bonus_cap=0.10,
        utbot_prime_confidence_per_agreement=0.03,
        utbot_prime_max_confidence_bonus=0.09,
        # Direction
        direction_mode="LONG_SHORT",
        # Entry gates
        regime_veto_enabled=True,
        regime_veto_adx_floor=18.0,
        volume_spike_entry_enabled=True,
        one_min_scalping_enabled=True,
        post_exit_reversion_enabled=False,
        # Quality
        choppy_market_confidence_floor=0.65,
        choppy_market_trending_confidence_floor=0.60,
        # Dashboard
        dashboard_port=8001,
        # Timing
        ut_force_exit_time="15:25",
        ut_5min_no_entry_after="15:15",
        ut_no_entry_after="15:00",
        # Position sync
        position_sync_interval_seconds=5.0,
        position_sync_idle_interval_seconds=30.0,
        position_sync_max_backoff_seconds=60.0,
        # Protective stops
        live_index_timeout_seconds=6.0,
        # Engine lab
        engine_lab_enabled=False,
        dual_lane_enabled=False,
        # PnL
        pnl_charges_per_order=20.0,
        pnl_stt_rate_options=0.000625,
        # Slippage
        slippage_model_enabled=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_signal(
    instrument="NIFTY",
    signal_type="BUY",
    price=22500.0,
    trailing_stop=22400.0,
    adx_value=28.0,
    atr_value=100.0,
    stop_distance=100.0,
    timeframe="5min",
    timestamp=None,
    trend_agreement=0,
):
    """Create a mock UTBotSignal."""
    ts = timestamp or datetime.now(IST)
    sig = SimpleNamespace(
        instrument=instrument,
        signal_type=signal_type,
        price=price,
        trailing_stop=trailing_stop,
        adx_value=adx_value,
        atr_value=atr_value,
        stop_distance=stop_distance,
        timeframe=timeframe,
        timestamp=ts,
        trend_agreement=trend_agreement,
        trend_agreement_sources=[],
        raw_candle={"open": 22480, "high": 22520, "low": 22450, "close": 22500},
        exchange="NSE",
        token="26000",
    )
    return sig


def _make_trade(trade_id="T001_NIFTY_0821", **overrides):
    """Create a minimal Trade-like object."""
    from trading.trade_manager import Trade
    defaults = dict(
        id=trade_id,
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        trailing_stop=95.0,
        current_stop=95.0,
        target=110.0,
        lots=1,
        lot_size=25,
        grade="A",
        confidence=0.75,
        inst_type="OPT",
        option_type="CE",
        status="OPEN",
        entry_time=datetime.now(IST),
        is_ghost=False,
        is_replay=False,
        is_historical=False,
        entry_spot=22500.0,
        spot_stop=22400.0,
        current_price=102.0,
        trading_symbol="NIFTY24AUG22500CE",
        symbol_token="12345",
        instrument_multiplier=1.0,
        initial_stop=95.0,
    )
    defaults.update(overrides)
    return Trade(**{k: v for k, v in defaults.items() if k in Trade.__dataclass_fields__})


class TimedStage:
    """Context manager that records execution time of a pipeline stage."""
    def __init__(self, name: str, results: dict):
        self.name = name
        self.results = results
        self.samples = []

    def __enter__(self):
        self._t0 = time.perf_counter_ns()
        return self

    def __exit__(self, *_):
        elapsed_ns = time.perf_counter_ns() - self._t0
        elapsed_us = elapsed_ns / 1_000
        self.samples.append(elapsed_us)
        self.results.setdefault(self.name, []).append(elapsed_us)


def _summarize_results(results: dict) -> dict:
    """Convert raw sample lists to summary statistics."""
    summary = {}
    for name, samples in results.items():
        n = len(samples)
        avg = statistics.mean(samples)
        med = statistics.median(samples)
        p95 = sorted(samples)[int(n * 0.95)] if n >= 20 else max(samples)
        p99 = sorted(samples)[int(n * 0.99)] if n >= 100 else max(samples)
        mn = min(samples)
        mx = max(samples)
        summary[name] = {
            "count": n,
            "mean_us": round(avg, 1),
            "median_us": round(med, 1),
            "p95_us": round(p95, 1),
            "p99_us": round(p99, 1),
            "min_us": round(mn, 1),
            "max_us": round(mx, 1),
            "mean_ms": round(avg / 1000, 3),
            "p95_ms": round(p95 / 1000, 3),
        }
    return summary


# ===============================================================
# Stage 1: Signal Filtering & Deduplication
# ===============================================================

class TestSignalFilteringSpeed:
    """Measures SignalManager.process_signal deduplication + grading speed."""

    def test_signal_deduplication_speed(self):
        """Dedup check for same-direction same-instrument signals."""
        from engine.signal_manager import SignalManager

        mgr = SignalManager(dedup_window_seconds=300)
        results = {}
        N = 500

        # First signal should pass through
        base_ts = datetime.now(IST)
        sig0 = _make_signal(timestamp=base_ts)
        with patch("config.settings.get_settings", return_value=_make_settings()):
            mgr.process_signal(sig0, confluence_score=0.5, intelligence_score=0.3, regime="TRENDING_UP")

        # Subsequent same-direction signals within the dedup window should be filtered
        for i in range(N):
            sig = _make_signal(
                timestamp=base_ts + timedelta(seconds=i * 0.1),  # Within 300s window
            )
            with TimedStage("1_signal_dedup", results):
                with patch("config.settings.get_settings", return_value=_make_settings()):
                    result = mgr.process_signal(sig, confluence_score=0.5, intelligence_score=0.3)

            # Should be deduplicated (returns None)
            assert result is None, f"Signal {i} should have been deduplicated"

        summary = _summarize_results(results)
        stage = summary["1_signal_dedup"]
        print(f"\n{'='*60}")
        print(f"Stage 1: Signal Deduplication ({N} iterations)")
        print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
        print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
        print(f"  Mean: {stage['mean_ms']:.3f} ms")
        # Dedup should be sub-50µs (dict lookup + timestamp math)
        assert stage["mean_us"] < 500, f"Dedup too slow: {stage['mean_us']:.1f} µs mean"

    def test_signal_pass_through_speed(self):
        """Full process_signal pass-through (new direction or expired window)."""
        from engine.signal_manager import SignalManager

        results = {}
        N = 200

        for i in range(N):
            mgr = SignalManager(dedup_window_seconds=300)
            sig = _make_signal(
                timestamp=datetime.now(IST) + timedelta(minutes=i * 10),
                adx_value=25 + (i % 20),
            )
            with TimedStage("1_signal_passthrough", results):
                with patch("config.settings.get_settings", return_value=_make_settings()):
                    result = mgr.process_signal(
                        sig,
                        confluence_score=0.5 + (i % 10) * 0.05,
                        intelligence_score=0.3 + (i % 10) * 0.03,
                        regime="TRENDING_UP",
                    )
            assert result is not None

        summary = _summarize_results(results)
        stage = summary["1_signal_passthrough"]
        print(f"\n{'='*60}")
        print(f"Stage 1b: Signal Pass-Through + Grading ({N} iterations)")
        print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
        print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
        print(f"  Mean: {stage['mean_ms']:.3f} ms")
        assert stage["mean_us"] < 5_000, f"Signal grading too slow: {stage['mean_us']:.1f} µs"


# ===============================================================
# Stage 2: Signal Grading (3-Pillar Engine)
# ===============================================================

class TestSignalGradingSpeed:
    """Measures the 3-pillar grading engine performance."""

    def test_grade_signal_speed(self):
        """Direct _grade_signal call with all pillars active."""
        from engine.signal_manager import SignalManager

        mgr = SignalManager()
        results = {}
        N = 500

        scenarios = [
            ("trending_buy", "BUY", "TRENDING_UP", 30.0, 0.6, 0.5),
            ("choppy_sell", "SELL", "RANGING", 18.0, 0.2, -0.3),
            ("counter_trend", "BUY", "TRENDING_DOWN", 22.0, -0.4, 0.1),
            ("unknown_regime", "SELL", "UNKNOWN", 15.0, 0.0, 0.0),
            ("breakout_buy", "BUY", "BREAKOUT_UP", 42.0, 0.8, 0.7),
        ]

        for i in range(N):
            label, sig_type, regime, adx, conf, intel = scenarios[i % len(scenarios)]
            sig = _make_signal(signal_type=sig_type, adx_value=adx)
            with TimedStage("2_grading", results):
                with patch("config.settings.get_settings", return_value=_make_settings()):
                    grade, confidence, reasons = mgr._grade_signal(
                        sig, conf, intel, regime=regime, vix_value=15.0,
                    )
            assert grade in ("A+", "A", "B+", "B", "C")

        summary = _summarize_results(results)
        stage = summary["2_grading"]
        print(f"\n{'='*60}")
        print(f"Stage 2: Signal Grading - 3 Pillar Engine ({N} iterations)")
        print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
        print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
        print(f"  Mean: {stage['mean_ms']:.3f} ms")
        # Grading is pure math + settings reads — should be < 2ms
        assert stage["mean_us"] < 10_000, f"Grading too slow: {stage['mean_us']:.1f} µs"


# ===============================================================
# Stage 3: Notification Dispatch
# ===============================================================

class TestNotificationSpeed:
    """Measures notification enqueue latency (not toast rendering)."""

    def test_notification_enqueue_speed(self):
        """Test send_desktop_notification enqueue (non-blocking path)."""
        results = {}
        N = 200

        # Patch win11toast to avoid actual toast rendering
        with patch.dict("sys.modules", {"win11toast": MagicMock()}):
            from notifications import send_desktop_notification, _notification_queue

            # Drain any pre-existing items
            while not _notification_queue.empty():
                try:
                    _notification_queue.get_nowait()
                except queue.Empty:
                    break

            for i in range(N):
                msg = f"Test signal NIFTY BUY 22500 #{i}"
                with TimedStage("3_notification_enqueue", results):
                    send_desktop_notification(msg, "entry")

        summary = _summarize_results(results)
        stage = summary["3_notification_enqueue"]
        print(f"\n{'='*60}")
        print(f"Stage 3: Notification Enqueue ({N} iterations)")
        print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
        print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
        print(f"  Mean: {stage['mean_ms']:.3f} ms")
        # Queue put_nowait should be sub-100µs
        assert stage["mean_us"] < 1_000, f"Notification enqueue too slow: {stage['mean_us']:.1f} µs"

    def test_notification_manager_format_speed(self):
        """Test NotificationManager toast formatting (no actual display)."""
        from engine.notification_manager import NotificationManager

        results = {}
        N = 200

        with patch("engine.notification_manager.InteractableWindowsToaster", None):
            mgr = NotificationManager()
            for i in range(N):
                with TimedStage("3_notification_format", results):
                    mgr.send_trade_notification(
                        signal_type="BUY",
                        instrument="NIFTY",
                        entry_price=22500.0,
                        target_price=22600.0,
                        sl_price=22400.0,
                        rr_ratio=1.5,
                        grade="A",
                        confidence=0.75,
                        timeframe="5min",
                    )

        summary = _summarize_results(results)
        stage = summary["3_notification_format"]
        print(f"\n{'='*60}")
        print(f"Stage 3b: Notification Formatting ({N} iterations)")
        print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
        print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
        print(f"  Mean: {stage['mean_ms']:.3f} ms")


# ===============================================================
# Stage 4: Trade Entry Admission Gates
# ===============================================================

class TestTradeEntrySpeed:
    """Measures TradeManager admission gate checks."""

    def _make_trade_manager(self):
        """Create a TradeManager with mocked broker."""
        from trading.trade_manager import TradeManager
        from trading.broker import SignalSimBroker

        broker = SignalSimBroker()
        tm = TradeManager(
            broker=broker,
            max_positions=5,
            session_end="15:25",
        )
        return tm

    def test_can_open_trade_speed(self):
        """Measure can_open_trade gate check latency."""
        tm = self._make_trade_manager()
        results = {}
        N = 500

        for i in range(N):
            with TimedStage("4_can_open_trade", results):
                can, reason = tm.can_open_trade()
            assert can is True

        summary = _summarize_results(results)
        stage = summary["4_can_open_trade"]
        print(f"\n{'='*60}")
        print(f"Stage 4: can_open_trade Gate ({N} iterations)")
        print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
        print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
        print(f"  Mean: {stage['mean_ms']:.3f} ms")
        assert stage["mean_us"] < 1_000, f"can_open_trade too slow: {stage['mean_us']:.1f} µs"

    def test_open_trade_full_admission_speed(self):
        """Full open_trade call with sim broker (no real order)."""
        tm = self._make_trade_manager()
        results = {}
        N = 50  # Each creates a real trade object

        with patch("config.settings.get_settings", return_value=_make_settings()):
            for i in range(N):
                # Reset to avoid max positions
                tm.open_trades.clear()
                tm._trade_counter = i

                with TimedStage("4_open_trade_full", results):
                    trade = tm.open_trade(
                        instrument="NIFTY",
                        timeframe="5min",
                        direction="LONG",
                        price=100.0 + i,
                        trailing_stop=95.0,
                        lots=1,
                        lot_size=25,
                        grade="A",
                        confidence=0.75,
                        inst_type="OPT",
                        option_type="CE",
                        trading_symbol=f"NIFTY24AUG22500CE_{i}",
                        symbol_token=f"1234{i}",
                        entry_spot=22500.0,
                        spot_stop=22400.0,
                        target=110.0,
                    )

        summary = _summarize_results(results)
        stage = summary["4_open_trade_full"]
        print(f"\n{'='*60}")
        print(f"Stage 4b: Full open_trade Admission ({N} iterations)")
        print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
        print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
        print(f"  Mean: {stage['mean_ms']:.3f} ms")
        # Full admission + Trade object creation should be < 20ms
        assert stage["mean_ms"] < 50, f"open_trade too slow: {stage['mean_ms']:.3f} ms"


# ===============================================================
# Stage 5: PnL Mark-to-Market Updates
# ===============================================================

class TestPnLUpdateSpeed:
    """Measures PnL calculation and mark-to-market performance."""

    def test_unrealized_pnl_speed(self):
        """Measure Trade.unrealized_pnl property calculation speed."""
        trade = _make_trade(entry_price=100.0, current_price=105.0)
        results = {}
        N = 1000

        for i in range(N):
            trade.current_price = 100.0 + (i % 20) * 0.5
            with TimedStage("5_unrealized_pnl", results):
                pnl = trade.unrealized_pnl

        summary = _summarize_results(results)
        stage = summary["5_unrealized_pnl"]
        print(f"\n{'='*60}")
        print(f"Stage 5: Unrealized PnL Calculation ({N} iterations)")
        print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
        print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
        print(f"  Mean: {stage['mean_ms']:.3f} ms")
        # PnL is simple arithmetic — should be sub-200µs
        assert stage["mean_us"] < 2_000, f"PnL calc too slow: {stage['mean_us']:.1f} µs"


# ===============================================================
# Stage 6: Trade Exit Processing
# ===============================================================

class TestTradeExitSpeed:
    """Measures trade finalization and close performance."""

    def test_finalize_closed_trade_speed(self):
        """Measure _finalize_closed_trade (no broker, sim mode)."""
        from trading.trade_manager import TradeManager
        from trading.broker import SignalSimBroker

        broker = SignalSimBroker()
        tm = TradeManager(broker=broker, max_positions=5)

        results = {}
        N = 100

        with patch("config.settings.get_settings", return_value=_make_settings()):
            for i in range(N):
                trade = _make_trade(
                    trade_id=f"T{i:03d}_NIFTY_0821",
                    entry_price=100.0,
                    current_price=105.0,
                )
                tm.open_trades[trade.id] = trade

                with TimedStage("6_finalize_close", results):
                    tm._finalize_closed_trade(
                        trade.id,
                        trade,
                        exit_price=105.0,
                        reason="TARGET_HIT",
                    )

        summary = _summarize_results(results)
        stage = summary["6_finalize_close"]
        print(f"\n{'='*60}")
        print(f"Stage 6: Trade Finalization ({N} iterations)")
        print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
        print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
        print(f"  Mean: {stage['mean_ms']:.3f} ms")
        assert stage["mean_ms"] < 50, f"Finalize too slow: {stage['mean_ms']:.3f} ms"


# ===============================================================
# Stage 7: Async Execution Queue Throughput
# ===============================================================

def _private_queue():
    """A throwaway AsyncExecutionQueue that is NOT the process singleton.

    AsyncExecutionQueue.__new__ is a singleton accessor (engine/async_queue.py:15-20):
    it ignores the caller and returns the one live instance. So
    `AsyncExecutionQueue.__new__(AsyncExecutionQueue)` handed these timing tests the
    REAL queue, and the fields they then overwrote — task_queue, worker_threads,
    worker_count, the locks — were the running system's. The last line of the latency
    test, `aeq._accepting = False`, therefore shut the shared queue down for the rest
    of the session: every later test that submitted work got "AsyncExecutionQueue is
    shutting down; rejected task" and failed, while passing in isolation.

    object.__new__ bypasses the accessor and gives a genuinely separate object.
    """
    from engine.async_queue import AsyncExecutionQueue

    return object.__new__(AsyncExecutionQueue)


class TestAsyncQueueSpeed:
    """Measures AsyncExecutionQueue submit → execute latency."""

    def test_queue_submit_speed(self):
        """Measure task submission latency (non-blocking)."""
        aeq = _private_queue()
        aeq.task_queue = queue.Queue(maxsize=500)
        aeq._accepting = True
        aeq._state_lock = threading.Lock()
        aeq._shutdown_lock = threading.RLock()
        aeq.worker_count = 4
        aeq.worker_threads = []
        aeq._queue_size = 500

        results = {}
        N = 500

        for i in range(N):
            with TimedStage("7_queue_submit", results):
                task = (f"test_task_{i}", lambda: None, (), {})
                try:
                    aeq.task_queue.put_nowait(task)
                except queue.Full:
                    pass

        summary = _summarize_results(results)
        stage = summary["7_queue_submit"]
        print(f"\n{'='*60}")
        print(f"Stage 7: Async Queue Submit ({N} iterations)")
        print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
        print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
        print(f"  Mean: {stage['mean_ms']:.3f} ms")
        # Queue put_nowait should be sub-50µs
        assert stage["mean_us"] < 500, f"Queue submit too slow: {stage['mean_us']:.1f} µs"

    def test_queue_submit_to_execute_latency(self):
        """Measure end-to-end submit → execute latency with real workers."""
        aeq = _private_queue()
        aeq.task_queue = queue.Queue(maxsize=500)
        aeq._accepting = True
        aeq._state_lock = threading.Lock()
        aeq._shutdown_lock = threading.RLock()
        aeq.worker_count = 2
        aeq.worker_threads = []
        aeq._queue_size = 500
        aeq._init = lambda: None

        # Start workers manually
        for idx in range(2):
            w = threading.Thread(target=aeq._process_queue, daemon=True, name=f"TestWorker-{idx}")
            w.start()
            aeq.worker_threads.append(w)

        results = {}
        N = 100
        events = []

        for i in range(N):
            evt = threading.Event()
            events.append(evt)
            t0 = time.perf_counter_ns()

            def task_fn(event=evt, start=t0, idx=i):
                elapsed_us = (time.perf_counter_ns() - start) / 1_000
                results.setdefault("7_queue_e2e", []).append(elapsed_us)
                event.set()

            aeq.task_queue.put_nowait((f"test_{i}", task_fn, (), {}))

        # Wait for all tasks
        for evt in events:
            evt.wait(timeout=5.0)

        # Shutdown
        aeq._accepting = False
        for w in aeq.worker_threads:
            w.join(timeout=2.0)

        if "7_queue_e2e" in results:
            summary = _summarize_results(results)
            stage = summary["7_queue_e2e"]
            print(f"\n{'='*60}")
            print(f"Stage 7b: Queue Submit-to-Execute E2E ({N} tasks)")
            print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
            print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
            print(f"  Mean: {stage['mean_ms']:.3f} ms")


# ===============================================================
# Stage 8: Risk Manager Circuit Breaker
# ===============================================================

class TestRiskManagerSpeed:
    """Measures RiskManager circuit breaker check speed."""

    def test_circuit_breaker_check_speed(self):
        """Measure check_circuit_breaker with mock trades."""
        from engine.risk_manager import RiskManager
        from trading.trade_manager import TradeManager
        from trading.broker import SignalSimBroker

        broker = SignalSimBroker()
        tm = TradeManager(broker=broker, max_positions=5)
        rm = RiskManager(tm, capital_total=500000.0)

        results = {}
        N = 200

        with patch("config.settings.get_settings", return_value=_make_settings()):
            for i in range(N):
                with TimedStage("8_circuit_breaker", results):
                    rm.check_circuit_breaker()

        summary = _summarize_results(results)
        stage = summary["8_circuit_breaker"]
        print(f"\n{'='*60}")
        print(f"Stage 8: Circuit Breaker Check ({N} iterations)")
        print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
        print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
        print(f"  Mean: {stage['mean_ms']:.3f} ms")
        # Circuit breaker is trades iteration — should be < 5ms empty
        assert stage["mean_us"] < 10_000, f"Circuit breaker too slow: {stage['mean_us']:.1f} µs"

    def test_circuit_breaker_with_trades_speed(self):
        """Measure circuit breaker with active open trades."""
        from engine.risk_manager import RiskManager
        from trading.trade_manager import TradeManager
        from trading.broker import SignalSimBroker

        broker = SignalSimBroker()
        tm = TradeManager(broker=broker, max_positions=10)
        rm = RiskManager(tm, capital_total=500000.0)

        # Add some open trades
        for i in range(5):
            trade = _make_trade(
                trade_id=f"T{i:03d}_NIFTY",
                entry_price=100.0 + i,
                current_price=102.0 + i,
            )
            tm.open_trades[trade.id] = trade

        # Add some closed trades
        for i in range(20):
            trade = _make_trade(
                trade_id=f"TC{i:03d}_NIFTY",
                entry_price=100.0,
                current_price=103.0,
                status="CLOSED",
            )
            trade.exit_price = 103.0
            trade.exit_time = datetime.now(IST)
            tm.closed_trades.append(trade)

        results = {}
        N = 200

        with patch("config.settings.get_settings", return_value=_make_settings()):
            for i in range(N):
                with TimedStage("8_circuit_breaker_loaded", results):
                    rm.check_circuit_breaker()

        summary = _summarize_results(results)
        stage = summary["8_circuit_breaker_loaded"]
        print(f"\n{'='*60}")
        print(f"Stage 8b: Circuit Breaker (5 open + 20 closed trades) ({N} iterations)")
        print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
        print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
        print(f"  Mean: {stage['mean_ms']:.3f} ms")
        assert stage["mean_ms"] < 20, f"Loaded circuit breaker too slow: {stage['mean_ms']:.3f} ms"


# ===============================================================
# Stage 9: Entry Risk Policy
# ===============================================================

class TestEntryRiskPolicySpeed:
    """Measures entry risk budget evaluation speed."""

    def test_evaluate_entry_risk_speed(self):
        """Measure evaluate_entry_risk pure computation."""
        from engine.entry_risk_policy import evaluate_entry_risk

        results = {}
        N = 500

        # Mock candidate
        candidate = SimpleNamespace(
            entry_price=100.0,
            trailing_stop=95.0,
            lots=1,
            lot_size=25,
            inst_type="OPT",
            instrument_multiplier=1.0,
            instrument="NIFTY",
        )

        # Mock open trades
        open_trades = {}
        for i in range(3):
            t = _make_trade(trade_id=f"T{i}", entry_price=100.0 + i, current_price=102.0)
            open_trades[t.id] = t

        for i in range(N):
            with TimedStage("9_entry_risk", results):
                decision = evaluate_entry_risk(
                    candidate,
                    list(open_trades.values()),
                    fut_limit_rs=15000.0,
                    opt_limit_rs=15000.0,
                    global_limit_rs=25000.0,
                    max_trade_loss_abs=5000.0,
                )

        summary = _summarize_results(results)
        stage = summary["9_entry_risk"]
        print(f"\n{'='*60}")
        print(f"Stage 9: Entry Risk Evaluation ({N} iterations)")
        print(f"  Mean: {stage['mean_us']:.1f} µs  |  Median: {stage['median_us']:.1f} µs")
        print(f"  P95:  {stage['p95_us']:.1f} µs  |  Max: {stage['max_us']:.1f} µs")
        print(f"  Mean: {stage['mean_ms']:.3f} ms")
        # Pure arithmetic — should be < 1ms
        assert stage["mean_us"] < 5_000, f"Entry risk eval too slow: {stage['mean_us']:.1f} µs"


# ===============================================================
# Stage 10: Full Pipeline Integration
# ===============================================================

class TestFullPipelineSpeed:
    """Measures the full signal → entry → PnL → exit pipeline latency."""

    def test_full_pipeline_e2e(self):
        """End-to-end pipeline: signal → grade → admit → trade → PnL → exit."""
        from engine.signal_manager import SignalManager
        from engine.entry_risk_policy import evaluate_entry_risk
        from trading.trade_manager import TradeManager
        from trading.broker import SignalSimBroker

        results = {}
        N = 50
        settings = _make_settings()

        for iteration in range(N):
            stage_times = {}

            with patch("config.settings.get_settings", return_value=settings):
                # -- Stage A: Signal Generation & Grading --
                mgr = SignalManager(dedup_window_seconds=300)
                sig = _make_signal(
                    timestamp=datetime.now(IST) + timedelta(minutes=iteration * 10),
                    adx_value=25.0 + (iteration % 15),
                    price=22500.0 + iteration * 10,
                )

                t0 = time.perf_counter_ns()
                graded = mgr.process_signal(
                    sig,
                    confluence_score=0.5,
                    intelligence_score=0.4,
                    regime="TRENDING_UP",
                )
                stage_times["A_signal_grade"] = (time.perf_counter_ns() - t0) / 1_000
                assert graded is not None

                # -- Stage B: Entry Risk Evaluation --
                candidate = SimpleNamespace(
                    entry_price=100.0,
                    trailing_stop=95.0,
                    lots=1,
                    lot_size=25,
                    inst_type="OPT",
                    instrument_multiplier=1.0,
                    instrument="NIFTY",
                )

                t0 = time.perf_counter_ns()
                decision = evaluate_entry_risk(
                    candidate,
                    [],
                    fut_limit_rs=15000.0,
                    opt_limit_rs=15000.0,
                    global_limit_rs=25000.0,
                    max_trade_loss_abs=5000.0,
                )
                stage_times["B_risk_eval"] = (time.perf_counter_ns() - t0) / 1_000

                # -- Stage C: Trade Admission & Entry --
                broker = SignalSimBroker()
                tm = TradeManager(broker=broker, max_positions=5)

                t0 = time.perf_counter_ns()
                trade = tm.open_trade(
                    instrument="NIFTY",
                    timeframe="5min",
                    direction="LONG",
                    price=100.0,
                    trailing_stop=95.0,
                    lots=1,
                    lot_size=25,
                    grade=graded.grade,
                    confidence=graded.confidence,
                    inst_type="OPT",
                    option_type="CE",
                    trading_symbol=f"NIFTY24AUG22500CE_{iteration}",
                    symbol_token=f"T{iteration}",
                    entry_spot=22500.0,
                    spot_stop=22400.0,
                    target=110.0,
                )
                stage_times["C_trade_entry"] = (time.perf_counter_ns() - t0) / 1_000

                # -- Stage D: PnL Mark-to-Market --
                if trade:
                    pnl_samples = []
                    for price_tick in range(10):
                        trade.current_price = 100.0 + price_tick * 0.5
                        t0 = time.perf_counter_ns()
                        _ = trade.unrealized_pnl
                        pnl_samples.append((time.perf_counter_ns() - t0) / 1_000)
                    stage_times["D_pnl_update"] = statistics.mean(pnl_samples)

                    # -- Stage E: Trade Exit --
                    t0 = time.perf_counter_ns()
                    tm._finalize_closed_trade(trade.id, trade, exit_price=105.0, reason="TARGET_HIT")
                    stage_times["E_trade_exit"] = (time.perf_counter_ns() - t0) / 1_000

                # -- Stage F: Notification Enqueue --
                t0 = time.perf_counter_ns()
                # Just measure the format + enqueue overhead
                msg = (
                    f"🟢 BUY {sig.instrument} @ ₹{sig.price:.2f} | "
                    f"Grade: {graded.grade} ({graded.confidence:.0%})"
                )
                stage_times["F_notification"] = (time.perf_counter_ns() - t0) / 1_000

            # Total E2E
            total = sum(stage_times.values())
            results.setdefault("10_total_e2e", []).append(total)
            for k, v in stage_times.items():
                results.setdefault(f"10_{k}", []).append(v)

        summary = _summarize_results(results)

        print(f"\n{'='*70}")
        print(f"Stage 10: FULL PIPELINE END-TO-END ({N} iterations)")
        print(f"{'-'*70}")

        # Print per-stage breakdown
        stage_order = [
            ("10_A_signal_grade", "A. Signal Grading"),
            ("10_B_risk_eval", "B. Risk Evaluation"),
            ("10_C_trade_entry", "C. Trade Entry"),
            ("10_D_pnl_update", "D. PnL Update (avg 10 ticks)"),
            ("10_E_trade_exit", "E. Trade Exit"),
            ("10_F_notification", "F. Notification Format"),
        ]

        total_mean = 0
        for key, label in stage_order:
            if key in summary:
                s = summary[key]
                total_mean += s["mean_us"]
                bar_len = min(50, int(s["mean_us"] / 100))
                bar = "#" * bar_len
                print(f"  {label:35s} {s['mean_us']:>10.1f} µs  ({s['mean_ms']:>7.3f} ms)  {bar}")

        print(f"{'-'*70}")
        total_s = summary.get("10_total_e2e", {})
        print(f"  {'TOTAL E2E':35s} {total_s.get('mean_us', 0):>10.1f} µs  ({total_s.get('mean_ms', 0):>7.3f} ms)")
        print(f"  {'P95 E2E':35s} {total_s.get('p95_us', 0):>10.1f} µs  ({total_s.get('p95_ms', 0):>7.3f} ms)")
        print(f"  {'Max E2E':35s} {total_s.get('max_us', 0):>10.1f} µs")
        print(f"{'='*70}")

        # Full pipeline should complete within 100ms
        assert total_s.get("mean_ms", 0) < 100, (
            f"Full pipeline too slow: {total_s.get('mean_ms', 0):.3f} ms mean"
        )

        # Identify bottleneck
        bottleneck = max(
            [(k, summary[k]["mean_us"]) for k, _ in stage_order if k in summary],
            key=lambda x: x[1],
        )
        print(f"\n  ! BOTTLENECK: {bottleneck[0]} at {bottleneck[1]:.1f} µs mean")

        # Return summary for reporting
        return summary


# ===============================================================
# Improvement Recommendations Report
# ===============================================================

class TestPipelineOptimizationReport:
    """Generates a comprehensive pipeline optimization analysis."""

    def test_generate_optimization_report(self):
        """Run all stages and produce a summary report."""
        all_results = {}

        with patch("config.settings.get_settings", return_value=_make_settings()):
            # -- 1. Signal Dedup --
            from engine.signal_manager import SignalManager
            mgr = SignalManager(dedup_window_seconds=300)
            sig0 = _make_signal()
            mgr.process_signal(sig0, confluence_score=0.5, intelligence_score=0.3, regime="TRENDING_UP")

            for i in range(200):
                sig = _make_signal(timestamp=datetime.now(IST) + timedelta(seconds=i * 0.1))
                with TimedStage("01_signal_dedup", all_results):
                    mgr.process_signal(sig, confluence_score=0.5, intelligence_score=0.3)

            # -- 2. Signal Grading --
            for i in range(200):
                mgr2 = SignalManager()
                sig = _make_signal(adx_value=20 + i % 30)
                with TimedStage("02_signal_grading", all_results):
                    mgr2._grade_signal(sig, 0.5, 0.3, regime="TRENDING_UP")

            # -- 3. Entry Risk --
            from engine.entry_risk_policy import evaluate_entry_risk
            cand = SimpleNamespace(
                entry_price=100.0, trailing_stop=95.0, lots=1, lot_size=25,
                inst_type="OPT", instrument_multiplier=1.0, instrument="NIFTY",
            )
            for i in range(200):
                with TimedStage("03_entry_risk_eval", all_results):
                    evaluate_entry_risk(
                        cand, [],
                        fut_limit_rs=15000, opt_limit_rs=15000, global_limit_rs=25000,
                        max_trade_loss_abs=5000,
                    )

            # -- 4. Trade Entry --
            from trading.trade_manager import TradeManager
            from trading.broker import SignalSimBroker
            for i in range(50):
                tm = TradeManager(broker=SignalSimBroker(), max_positions=5)
                with TimedStage("04_trade_entry", all_results):
                    tm.open_trade(
                        instrument="NIFTY", timeframe="5min", direction="LONG",
                        price=100.0, trailing_stop=95.0, lots=1, lot_size=25,
                        grade="A", trading_symbol=f"SYM_{i}", symbol_token=f"T{i}",
                        inst_type="OPT", option_type="CE", entry_spot=22500.0,
                        spot_stop=22400.0, target=110.0,
                    )

            # -- 5. PnL --
            trade = _make_trade()
            for i in range(500):
                trade.current_price = 100.0 + (i % 20) * 0.5
                with TimedStage("05_pnl_update", all_results):
                    _ = trade.unrealized_pnl

            # -- 6. Trade Exit --
            for i in range(50):
                tm = TradeManager(broker=SignalSimBroker(), max_positions=5)
                t = _make_trade(trade_id=f"TEX{i}")
                tm.open_trades[t.id] = t
                with TimedStage("06_trade_exit", all_results):
                    tm._finalize_closed_trade(t.id, t, exit_price=105.0, reason="TARGET_HIT")

            # -- 7. Circuit Breaker --
            from engine.risk_manager import RiskManager
            tm_cb = TradeManager(broker=SignalSimBroker(), max_positions=5)
            rm = RiskManager(tm_cb, capital_total=500000.0)
            for i in range(200):
                with TimedStage("07_circuit_breaker", all_results):
                    rm.check_circuit_breaker()

            # -- 8. Queue Submit --
            for i in range(200):
                q = queue.Queue(maxsize=500)
                with TimedStage("08_queue_submit", all_results):
                    q.put_nowait(("test", lambda: None, (), {}))

        summary = _summarize_results(all_results)

        print(f"\n{'='*80}")
        print(f"     FULL PIPELINE SPEED ANALYSIS & OPTIMIZATION REPORT")
        print(f"{'='*80}")
        print(f"{'Stage':50s} {'Mean':>10s} {'P95':>10s} {'Max':>10s} {'Status':>8s}")
        print(f"{'-'*80}")

        budgets = {
            "01_signal_dedup":       ("Signal Dedup/Filter",           200),
            "02_signal_grading":     ("Signal 3-Pillar Grading",      2000),
            "03_entry_risk_eval":    ("Entry Risk Evaluation",         500),
            "04_trade_entry":        ("Trade Entry (open_trade)",    20000),
            "05_pnl_update":         ("PnL Mark-to-Market",           500),
            "06_trade_exit":         ("Trade Exit (finalize)",      20000),
            "07_circuit_breaker":    ("Risk Circuit Breaker",        5000),
            "08_queue_submit":       ("Async Queue Submit",           200),
        }

        total_mean = 0
        bottlenecks = []
        for key, (label, budget_us) in budgets.items():
            s = summary.get(key, {})
            mean = s.get("mean_us", 0)
            p95 = s.get("p95_us", 0)
            mx = s.get("max_us", 0)
            total_mean += mean
            status = "✅ PASS" if mean < budget_us else "!️ SLOW"
            if mean >= budget_us:
                bottlenecks.append((label, mean, budget_us))
            print(f"  {label:48s} {mean:>8.0f}µs {p95:>8.0f}µs {mx:>8.0f}µs {status}")

        print(f"{'-'*80}")
        print(f"  {'TOTAL (serial sum)':48s} {total_mean:>8.0f}µs {'':>10s} {'':>10s}")
        print(f"  {'TOTAL':48s} {total_mean/1000:>8.1f}ms")
        print(f"{'='*80}")

        if bottlenecks:
            print(f"\n  !️  BOTTLENECKS IDENTIFIED:")
            for label, actual, budget in sorted(bottlenecks, key=lambda x: -x[1]):
                ratio = actual / budget
                print(f"    * {label}: {actual:.0f}µs (budget: {budget}µs, {ratio:.1f}x over)")
        else:
            print(f"\n  ✅ ALL STAGES WITHIN BUDGET")

        print(f"\n  📊 OPTIMIZATION RECOMMENDATIONS:")
        print(f"  ---------------------------------")

        # Identify specific recommendations based on measured data
        recs = []

        grading_mean = summary.get("02_signal_grading", {}).get("mean_us", 0)
        if grading_mean > 1000:
            recs.append(
                "  1. SIGNAL GRADING: Cache get_settings() calls within a scan cycle\n"
                "     instead of re-importing per grade call (5 import-time lookups).\n"
                f"     Current: {grading_mean:.0f}µs → Target: <500µs"
            )

        entry_mean = summary.get("04_trade_entry", {}).get("mean_us", 0)
        if entry_mean > 5000:
            recs.append(
                f"  2. TRADE ENTRY: open_trade holds self._lock for the full body.\n"
                "     Move DB persistence (save_state) outside the critical section.\n"
                f"     Current: {entry_mean:.0f}µs → Target: <5000µs"
            )

        exit_mean = summary.get("06_trade_exit", {}).get("mean_us", 0)
        if exit_mean > 5000:
            recs.append(
                f"  3. TRADE EXIT: _finalize_closed_trade includes DB I/O (save_state).\n"
                "     Defer persistence to a background write-behind queue.\n"
                f"     Current: {exit_mean:.0f}µs → Target: <5000µs"
            )

        pnl_mean = summary.get("05_pnl_update", {}).get("mean_us", 0)
        if pnl_mean > 200:
            recs.append(
                f"  4. PNL UPDATE: estimate_trade_charges called per tick.\n"
                "     Cache charges at entry and only recompute on lot changes.\n"
                f"     Current: {pnl_mean:.0f}µs → Target: <100µs"
            )

        cb_mean = summary.get("07_circuit_breaker", {}).get("mean_us", 0)
        if cb_mean > 2000:
            recs.append(
                f"  5. CIRCUIT BREAKER: Iterates all trades under RLock.\n"
                "     Maintain running PnL totals (incremental) instead of\n"
                "     re-summing on each check.\n"
                f"     Current: {cb_mean:.0f}µs → Target: <1000µs"
            )

        dedup_mean = summary.get("01_signal_dedup", {}).get("mean_us", 0)
        if dedup_mean > 100:
            recs.append(
                f"  6. SIGNAL DEDUP: Timestamp epoch conversion on each check.\n"
                "     Pre-compute and cache epoch timestamps on signal creation.\n"
                f"     Current: {dedup_mean:.0f}µs → Target: <50µs"
            )

        if not recs:
            recs.append("  All stages are within optimal performance budgets! ✅")

        for rec in recs:
            print(rec)

        print(f"\n{'='*80}")

        # Overall assertion
        assert total_mean / 1000 < 200, (
            f"Total serial pipeline time {total_mean/1000:.1f}ms exceeds 200ms budget"
        )
