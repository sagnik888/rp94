from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from config.runtime_groups import ScannerRuntimeSettings
from engine.analysis_schedule import analysis_due, quote_generation
from engine.call_guard import CallCircuitBreaker, CircuitBusyError
from engine.decision_evaluation import EvaluationThresholds, evaluate_paired_replays


def test_typed_runtime_groups_read_legacy_flat_fields():
    flat = SimpleNamespace(
        live_index_timeout_seconds="4.5",
        event_driven_scan_enabled=True,
        intracandle_analysis_min_interval_seconds="2",
        external_call_timeout_seconds="3",
        external_circuit_breaker_failures="4",
        external_circuit_breaker_recovery_seconds="12",
        external_call_max_inflight_per_route="2",
    )
    grouped = ScannerRuntimeSettings.from_flat(flat)
    assert grouped.live_scan.index_deadline_seconds == 4.5
    assert grouped.live_scan.intracandle_min_interval_seconds == 2.0
    assert grouped.external_calls.failure_threshold == 4
    assert grouped.external_calls.max_inflight_per_route == 2

    flat.intracandle_analysis_min_interval_seconds = 0
    assert ScannerRuntimeSettings.from_flat(flat).live_scan.intracandle_min_interval_seconds == 0


def test_intracandle_quotes_are_coalesced_but_explicit_events_are_not():
    assert quote_generation(10.1, 1.0) == quote_generation(10.9, 1.0)
    assert quote_generation(10.9, 1.0) != quote_generation(11.0, 1.0)
    assert analysis_due(
        event_driven=True,
        cycle_is_warmup=False,
        initial_setup_completed=True,
        has_pending_signals=False,
        signature=("new-feature",),
        previous_signature=("old-feature",),
        requested_reason="feature event",
    ) == (True, "feature event")
    assert analysis_due(
        event_driven=True,
        cycle_is_warmup=False,
        initial_setup_completed=True,
        has_pending_signals=False,
        signature=("same-feature", "new-quote"),
        previous_signature=("same-feature", "old-quote"),
        previous_analysis_at=10.0,
        intracandle_min_interval_seconds=2.0,
        now_monotonic=11.0,
    ) == (False, "intracandle analysis throttled")


def test_timed_out_external_work_keeps_its_bulkhead_lease():
    release = threading.Event()
    breaker = CallCircuitBreaker(failure_threshold=5, recovery_seconds=1.0, max_inflight=1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        with pytest.raises(TimeoutError):
            breaker.call(executor, release.wait, timeout_seconds=0.05, label="hung route")
        assert breaker.snapshot()["timed_out_inflight"] == 1
        with pytest.raises(CircuitBusyError, match="saturated"):
            breaker.call(executor, lambda: 1, timeout_seconds=0.05, label="hung route")
        release.set()
        deadline = time.monotonic() + 1.0
        while breaker.snapshot()["inflight"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert breaker.snapshot()["inflight"] == 0


def _trade(identity, pnl, day, index="NIFTY"):
    return {
        "id": identity,
        "instrument": f"{index} 20000 CE",
        "pnl": pnl,
        "entry_timestamp": day * 86400,
        "charges": 1.0,
    }


def test_paired_evaluation_blocks_concentrated_single_index_candidate():
    baseline = {"trades": [_trade("a", 10, 1), _trade("b", -5, 2)]}
    candidate = {
        "trades": [_trade("a", 30, 1), _trade("b", 2, 2)],
        "summary": {"network_attempts": 0, "rejects": {"missing candle": 2}},
    }
    result = evaluate_paired_replays(
        baseline,
        candidate,
        starting_capital=1000,
        thresholds=EvaluationThresholds(
            max_drawdown_pct=20,
            max_single_day_profit_concentration=0.60,
            min_positive_unseen_period_ratio=0.5,
            min_index_profit_factor=1.0,
            min_pair_coverage=1.0,
        ),
    )
    assert result["paired_delta_net"] == 27
    assert result["gates"]["single_day_concentration_acceptable"] is False
    assert result["promotable"] is False


def test_paired_evaluation_promotes_balanced_causal_candidate():
    baseline = {
        "trades": [
            _trade("a", 1, 1, "NIFTY"),
            _trade("b", 1, 2, "BANKNIFTY"),
            _trade("c", 1, 3, "NIFTY"),
            _trade("d", 1, 4, "BANKNIFTY"),
        ]
    }
    candidate = {
        "trades": [
            _trade("a", 10, 1, "NIFTY"),
            _trade("b", 10, 2, "BANKNIFTY"),
            _trade("c", 10, 3, "NIFTY"),
            _trade("d", 10, 4, "BANKNIFTY"),
        ],
        "summary": {"network_attempts": 0, "rejects": {}},
    }
    result = evaluate_paired_replays(
        baseline,
        candidate,
        starting_capital=1000,
        thresholds=EvaluationThresholds(
            max_drawdown_pct=20,
            max_single_day_profit_concentration=0.35,
            min_positive_unseen_period_ratio=0.5,
            min_index_profit_factor=1.0,
            min_pair_coverage=1.0,
        ),
    )
    assert result["promotable"] is True
    assert all(result["gates"].values())
