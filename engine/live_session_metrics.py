"""In-memory evidence for one live scanner session."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * max(0.0, min(1.0, percentile))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


class LiveSessionMetrics:
    def __init__(self, max_samples: int = 100_000) -> None:
        self._lock = threading.Lock()
        self._started_monotonic = time.monotonic()
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._cycle_ms = deque(maxlen=max_samples)
        self._exit_ms = deque(maxlen=max_samples)
        self._analysis_runs = 0
        self._analysis_skips = 0
        self._skip_reasons: dict[str, int] = {}
        self._latest_timeout_count = 0
        self._latest_bulkheads: dict[str, Any] = {}

    def record_cycle(
        self,
        duration_ms: float,
        *,
        analysis_recomputed: bool,
        skip_reason: str = "",
        timeout_count: int = 0,
        bulkheads: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._cycle_ms.append(max(0.0, float(duration_ms)))
            if analysis_recomputed:
                self._analysis_runs += 1
            else:
                self._analysis_skips += 1
                reason = str(skip_reason or "unspecified")
                self._skip_reasons[reason] = self._skip_reasons.get(reason, 0) + 1
            self._latest_timeout_count = max(0, int(timeout_count or 0))
            self._latest_bulkheads = dict(bulkheads or {})

    def record_exit_tick(self, duration_ms: float) -> None:
        with self._lock:
            self._exit_ms.append(max(0.0, float(duration_ms)))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cycles = list(self._cycle_ms)
            exits = list(self._exit_ms)
            runs = self._analysis_runs
            skips = self._analysis_skips
            reasons = dict(self._skip_reasons)
            timeout_count = self._latest_timeout_count
            bulkheads = dict(self._latest_bulkheads)
        total = runs + skips
        duration_seconds = max(0.0, time.monotonic() - self._started_monotonic)
        saturated = sum(
            int((row or {}).get("inflight", 0) or 0)
            >= 1 and str((row or {}).get("state", "")) != "CLOSED"
            for row in bulkheads.values()
        )
        return {
            "started_at": self._started_at,
            "duration_seconds": round(duration_seconds, 3),
            "cycles": len(cycles),
            "cycle_ms": {
                "p50": _percentile(cycles, 0.50),
                "p95": _percentile(cycles, 0.95),
                "p99": _percentile(cycles, 0.99),
                "max": round(max(cycles), 3) if cycles else None,
            },
            "analysis": {
                "runs": runs,
                "skips": skips,
                "skip_ratio": round(skips / total, 4) if total else 0.0,
                "skip_reasons": reasons,
            },
            "exit_tick_ms": {
                "samples": len(exits),
                "p50": _percentile(exits, 0.50),
                "p95": _percentile(exits, 0.95),
                "p99": _percentile(exits, 0.99),
                "max": round(max(exits), 3) if exits else None,
            },
            "timeout_count": timeout_count,
            "bulkheads": bulkheads,
            "saturated_bulkheads": saturated,
            "full_session_evidence": duration_seconds >= 4 * 60 * 60 and len(cycles) >= 1_000,
        }
