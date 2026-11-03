"""Bounded ownership seam for live external market-data calls."""

from __future__ import annotations

import threading
from concurrent.futures import Executor
from typing import Any, Callable

from config.runtime_groups import ExternalCallSettings
from engine.call_guard import CallCircuitBreaker


class LiveMarketDataGateway:
    def __init__(self, executor: Executor, policy: ExternalCallSettings) -> None:
        self._executor = executor
        self._policy = policy
        self._breakers: dict[str, CallCircuitBreaker] = {}
        self._lock = threading.Lock()

    def _breaker(self, route: str) -> CallCircuitBreaker:
        key = str(route or "external")
        with self._lock:
            breaker = self._breakers.get(key)
            if breaker is None:
                breaker = CallCircuitBreaker(
                    failure_threshold=self._policy.failure_threshold,
                    recovery_seconds=self._policy.recovery_seconds,
                    max_inflight=self._policy.max_inflight_per_route,
                )
                self._breakers[key] = breaker
            return breaker

    def call(
        self,
        route: str,
        function: Callable[[], Any],
        *,
        label: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        return self._breaker(route).call(
            self._executor,
            function,
            timeout_seconds=(
                self._policy.deadline_seconds
                if timeout_seconds is None
                else max(0.05, float(timeout_seconds))
            ),
            label=label or route,
        )

    def snapshot(self) -> dict[str, dict[str, int | str]]:
        with self._lock:
            items = tuple(self._breakers.items())
        return {route: breaker.snapshot() for route, breaker in items}
