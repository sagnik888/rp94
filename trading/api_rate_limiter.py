"""Shared AngelOne REST pacing for data and order-management calls."""

from __future__ import annotations

import threading
import time
from typing import Any


class SharedApiRateLimiter:
    def __init__(self, min_interval_seconds: float = 0.5):
        self.min_interval_seconds = max(0.05, float(min_interval_seconds))
        self._lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._quote_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._last_call = 0.0
        self._priority_waiters = 0
        self._active_priority_calls = 0
        self._active_quote_calls = 0
        self.calls = 0
        self.timeouts = 0
        self.quote_skips = 0

    def acquire(self, timeout: float = 0.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            acquired = self._lock.acquire(timeout=remaining) if timeout > 0 else self._lock.acquire(False)
            if not acquired:
                self.timeouts += 1
                return False
            wait = self.min_interval_seconds - (time.monotonic() - self._last_call)
            if wait <= 0:
                self._last_call = time.monotonic()
                self.calls += 1
                self._lock.release()
                return True
            self._lock.release()
            if timeout <= 0 or time.monotonic() + wait > deadline:
                self.timeouts += 1
                return False
            time.sleep(min(wait, max(0.0, deadline - time.monotonic())))

    def begin_priority_call(self, timeout: float = 5.0) -> bool:
        """Reserve the shared API for orders, stops, exits, or reconciliation."""
        timeout = max(0.0, float(timeout))
        deadline = time.monotonic() + timeout
        with self._state_lock:
            self._priority_waiters += 1
        try:
            acquired = self._call_lock.acquire(timeout=timeout) if timeout > 0 else self._call_lock.acquire(False)
        finally:
            with self._state_lock:
                self._priority_waiters = max(0, self._priority_waiters - 1)
        if not acquired:
            self.timeouts += 1
            return False

        remaining = max(0.0, deadline - time.monotonic())
        if not self.acquire(timeout=remaining):
            self._call_lock.release()
            return False
        with self._state_lock:
            self._active_priority_calls += 1
        return True

    def end_priority_call(self) -> None:
        with self._state_lock:
            self._active_priority_calls = max(0, self._active_priority_calls - 1)
        self._call_lock.release()

    def try_begin_best_effort_quote(self) -> bool:
        """Start one quote only when no broker-critical call is active or queued."""
        if not self._quote_lock.acquire(False):
            self.quote_skips += 1
            return False
        with self._state_lock:
            priority_busy = self._priority_waiters > 0 or self._active_priority_calls > 0
        if priority_busy or not self._call_lock.acquire(False):
            self.quote_skips += 1
            self._quote_lock.release()
            return False
        if not self.acquire(timeout=0.0):
            self.quote_skips += 1
            self._call_lock.release()
            self._quote_lock.release()
            return False
        with self._state_lock:
            self._active_quote_calls = 1
        return True

    def end_best_effort_quote(self) -> None:
        with self._state_lock:
            self._active_quote_calls = 0
        self._call_lock.release()
        self._quote_lock.release()

    def snapshot(self) -> dict:
        with self._state_lock:
            return {
                "priority_waiters": self._priority_waiters,
                "active_priority_calls": self._active_priority_calls,
                "active_quote_calls": self._active_quote_calls,
                "quote_skips": self.quote_skips,
            }


def get_shared_api_limiter(api: Any, min_interval_seconds: float = 0.5) -> SharedApiRateLimiter:
    limiter = getattr(api, "_ut_shared_rate_limiter", None)
    if limiter is None:
        limiter = SharedApiRateLimiter(min_interval_seconds=min_interval_seconds)
        setattr(api, "_ut_shared_rate_limiter", limiter)
    return limiter
