"""Bound external calls with a small closed/open/half-open circuit breaker."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Executor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable


class CircuitOpenError(RuntimeError):
    pass


class CircuitBusyError(RuntimeError):
    """The bounded external-call lane has no free capacity."""


@dataclass
class CallCircuitBreaker:
    failure_threshold: int = 3
    recovery_seconds: float = 60.0
    max_inflight: int = 1
    _failures: int = 0
    _opened_at: float = 0.0
    _inflight: int = 0
    _timed_out_inflight: int = 0
    _half_open_probe: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _reserve(self, now: float, label: str) -> None:
        with self._lock:
            if self._opened_at > 0:
                if now - self._opened_at < max(0.1, float(self.recovery_seconds)):
                    raise CircuitOpenError(f"{label} circuit is open")
                if self._half_open_probe:
                    raise CircuitOpenError(f"{label} circuit half-open probe is running")
                self._half_open_probe = True
            if self._inflight >= max(1, int(self.max_inflight)):
                if self._half_open_probe:
                    self._half_open_probe = False
                raise CircuitBusyError(f"{label} call lane is saturated")
            self._inflight += 1

    def _release(self, *, timed_out: bool = False) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            if timed_out:
                self._timed_out_inflight = max(0, self._timed_out_inflight - 1)

    def _success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = 0.0
            self._half_open_probe = False

    def _failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._half_open_probe = False
            if self._failures >= max(1, int(self.failure_threshold)):
                self._opened_at = time.monotonic()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at <= 0:
                return "CLOSED"
            if time.monotonic() - self._opened_at >= max(0.1, float(self.recovery_seconds)):
                return "HALF_OPEN"
            return "OPEN"

    def snapshot(self) -> dict[str, int | str]:
        """Small telemetry payload; timed-out work remains visible until completion."""
        with self._lock:
            state = "CLOSED"
            if self._opened_at > 0:
                state = (
                    "HALF_OPEN"
                    if time.monotonic() - self._opened_at
                    >= max(0.1, float(self.recovery_seconds))
                    else "OPEN"
                )
            return {
                "state": state,
                "failures": self._failures,
                "inflight": self._inflight,
                "timed_out_inflight": self._timed_out_inflight,
            }

    def call(
        self,
        executor: Executor,
        function: Callable[[], Any],
        *,
        timeout_seconds: float,
        label: str = "external call",
    ) -> Any:
        self._reserve(time.monotonic(), label)
        try:
            future = executor.submit(function)
        except Exception:
            self._release()
            self._failure()
            raise
        lease = {"timed_out": False, "released": False}

        def release_when_really_done(_future) -> None:
            with self._lock:
                lease["released"] = True
                self._inflight = max(0, self._inflight - 1)
                if lease["timed_out"]:
                    self._timed_out_inflight = max(0, self._timed_out_inflight - 1)

        future.add_done_callback(release_when_really_done)
        try:
            result = future.result(timeout=max(0.05, float(timeout_seconds)))
        except FutureTimeoutError as exc:
            # Running Python/SDK work cannot be cancelled by Future.cancel(). Keep its
            # bulkhead lease until the callback observes real completion.
            with self._lock:
                lease["timed_out"] = True
                if not lease["released"]:
                    self._timed_out_inflight += 1
            self._failure()
            raise TimeoutError(
                f"{label} exceeded {float(timeout_seconds):.1f}s deadline"
            ) from exc
        except Exception:
            self._failure()
            raise
        self._success()
        return result
