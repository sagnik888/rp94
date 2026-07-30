"""Shared AngelOne REST pacing for data and order-management calls."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any


class SharedApiRateLimiter:
    # Official SmartAPI limits are per client code.  The global 0.5s pacing
    # below is intentionally conservative, but it is insufficient for the
    # endpoints capped at 1 request/s (order book, positions, trade book).
    # Keep order-family accounting explicit even though global serialization is
    # currently stricter than the broker's 9 request/s combined order ceiling.
    ENDPOINT_MIN_INTERVALS = {
        "order_book": 1.0,
        "positions": 1.0,
        "trade_book": 1.0,
        "rms": 0.5,
        "order_family": 1.0 / 9.0,
        "individual_order": 0.1,
        "margin": 0.1,
    }

    def __init__(self, min_interval_seconds: float = 0.5):
        self.min_interval_seconds = max(0.05, float(min_interval_seconds))
        self._lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._quote_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._last_call = 0.0
        self._endpoint_last_call: dict[str, float] = {}
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

    def _acquire_endpoint_interval(self, endpoint: str, timeout: float) -> bool:
        """Reserve one endpoint-specific slot while the priority lane is held."""
        endpoint = str(endpoint or "").strip().lower()
        interval = float(self.ENDPOINT_MIN_INTERVALS.get(endpoint, 0.0) or 0.0)
        if interval <= 0:
            return True
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._state_lock:
                previous = float(self._endpoint_last_call.get(endpoint, 0.0) or 0.0)
            now = time.monotonic()
            wait = interval - (now - previous)
            if wait <= 0:
                with self._state_lock:
                    # The call lock serializes priority callers, nevertheless
                    # recheck to keep this correct if a future caller uses it
                    # from another lane.
                    latest = float(self._endpoint_last_call.get(endpoint, 0.0) or 0.0)
                    if time.monotonic() - latest >= interval:
                        self._endpoint_last_call[endpoint] = time.monotonic()
                        return True
                continue
            remaining = deadline - now
            if remaining <= 0 or wait > remaining:
                self.timeouts += 1
                return False
            time.sleep(min(wait, remaining))

    def begin_priority_call(self, timeout: float = 5.0, endpoint: str = "") -> bool:
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
        remaining = max(0.0, deadline - time.monotonic())
        if not self._acquire_endpoint_interval(endpoint, remaining):
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
                "endpoint_last_call": dict(self._endpoint_last_call),
            }


def get_shared_api_limiter(api: Any, min_interval_seconds: float = 0.5) -> SharedApiRateLimiter:
    limiter = getattr(api, "_ut_shared_rate_limiter", None)
    if limiter is None:
        limiter = SharedApiRateLimiter(min_interval_seconds=min_interval_seconds)
        setattr(api, "_ut_shared_rate_limiter", limiter)
    return limiter


class AngelHistoricalRateLimiter:
    """Non-blocking, per-client pacing for Angel's candle-history endpoint.

    The broker permits three requests per second and 180 per rolling minute.
    Historical refresh is deliberately best-effort: it never takes the order
    lane, so a pending stop, exit, or position reconciliation always wins.
    """

    def __init__(
        self,
        *,
        per_second: int = 3,
        per_minute: int = 180,
        per_hour: int = 5000,
        clock=time.monotonic,
    ) -> None:
        self.per_second = max(1, int(per_second))
        self.per_minute = max(1, int(per_minute))
        self.per_hour = max(1, int(per_hour))
        self._clock = clock
        self._guard = threading.Lock()
        self._inflight = threading.Lock()
        self._calls: deque[float] = deque()
        self.calls = 0
        self.skips_priority = 0
        self.skips_per_second = 0
        self.skips_per_minute = 0
        self.skips_per_hour = 0

    def try_begin(self, priority_limiter: SharedApiRateLimiter | None = None) -> bool:
        """Reserve a history request without queueing ahead of order work."""
        if not self._inflight.acquire(False):
            self.skips_priority += 1
            return False
        if priority_limiter is not None:
            state = priority_limiter.snapshot()
            if state.get("priority_waiters", 0) or state.get("active_priority_calls", 0):
                self.skips_priority += 1
                self._inflight.release()
                return False
        now = float(self._clock())
        with self._guard:
            while self._calls and now - self._calls[0] >= 60.0:
                # Keep one hour of timestamps for the published 5,000/hour
                # ceiling; dropping only at 60 seconds would make that cap
                # impossible to enforce.
                if now - self._calls[0] >= 3600.0:
                    self._calls.popleft()
                else:
                    break
            second_count = sum(1 for ts in self._calls if now - ts < 1.0)
            if second_count >= self.per_second:
                self.skips_per_second += 1
                self._inflight.release()
                return False
            minute_count = sum(1 for ts in self._calls if now - ts < 60.0)
            if minute_count >= self.per_minute:
                self.skips_per_minute += 1
                self._inflight.release()
                return False
            hour_count = sum(1 for ts in self._calls if now - ts < 3600.0)
            if hour_count >= self.per_hour:
                self.skips_per_hour += 1
                self._inflight.release()
                return False
            self._calls.append(now)
            self.calls += 1
        return True

    def end(self) -> None:
        if self._inflight.locked():
            self._inflight.release()

    def snapshot(self) -> dict:
        with self._guard:
            now = float(self._clock())
            return {
                "calls_last_minute": sum(1 for ts in self._calls if now - ts < 60.0),
                "calls_last_hour": sum(1 for ts in self._calls if now - ts < 3600.0),
                "per_second_limit": self.per_second,
                "per_minute_limit": self.per_minute,
                "per_hour_limit": self.per_hour,
                "calls": self.calls,
                "skips_priority": self.skips_priority,
                "skips_per_second": self.skips_per_second,
                "skips_per_minute": self.skips_per_minute,
                "skips_per_hour": self.skips_per_hour,
                "inflight": self._inflight.locked(),
            }


_HISTORY_LIMITERS: dict[str, AngelHistoricalRateLimiter] = {}
_HISTORY_LIMITERS_LOCK = threading.Lock()


def get_shared_history_limiter(client_code: str) -> AngelHistoricalRateLimiter:
    """Return the process-shared history limiter keyed by Angel client code."""
    key = str(client_code or "").strip().upper() or "__UNKNOWN_CLIENT__"
    with _HISTORY_LIMITERS_LOCK:
        limiter = _HISTORY_LIMITERS.get(key)
        if limiter is None:
            limiter = AngelHistoricalRateLimiter()
            _HISTORY_LIMITERS[key] = limiter
        return limiter


class AngelApiQuotaExceeded(RuntimeError):
    """A published rolling SmartAPI quota was exhausted before a REST call."""


class AngelClientQuotaRegistry:
    """Process-wide rolling REST quotas for one Angel client code.

    This intentionally survives SDK object replacement after reauthentication.
    It is process-local: the REAL account execution lease prevents another UT1
    execution process on the same host, while separate hosts must coordinate
    their broker usage operationally.
    """

    LIMITS = {
        # Place/modify/cancel are a combined Angel budget.
        "order_family": ((60.0, 500), (3600.0, 1000)),
        "market": ((60.0, 500), (3600.0, 5000)),
        "margin": ((60.0, 500), (3600.0, 5000)),
        "individual_order": ((60.0, 500), (3600.0, 5000)),
    }

    def __init__(self, *, clock=time.monotonic, limits=None) -> None:
        self._clock = clock
        self._limits = dict(limits or self.LIMITS)
        self._guard = threading.Lock()
        self._calls: dict[str, deque[float]] = {}
        self.rejections: dict[str, int] = {}

    def try_reserve(self, endpoint: str) -> bool:
        endpoint = str(endpoint or "").strip().lower()
        limits = self._limits.get(endpoint)
        if not limits:
            return True
        now = float(self._clock())
        longest = max(float(window) for window, _ in limits)
        with self._guard:
            calls = self._calls.setdefault(endpoint, deque())
            while calls and now - calls[0] >= longest:
                calls.popleft()
            for window, limit in limits:
                if sum(1 for ts in calls if now - ts < float(window)) >= int(limit):
                    self.rejections[endpoint] = self.rejections.get(endpoint, 0) + 1
                    return False
            calls.append(now)
            return True

    def snapshot(self) -> dict:
        now = float(self._clock())
        with self._guard:
            endpoints = {}
            for endpoint, limits in self._limits.items():
                calls = self._calls.get(endpoint, ())
                endpoints[endpoint] = {
                    f"calls_last_{int(window)}s": sum(
                        1 for ts in calls if now - ts < float(window)
                    )
                    for window, _ in limits
                }
            return {"limits": self._limits, "endpoints": endpoints, "rejections": dict(self.rejections)}


_CLIENT_QUOTAS: dict[str, AngelClientQuotaRegistry] = {}
_CLIENT_QUOTAS_LOCK = threading.Lock()


def get_shared_client_quota(client_code: str) -> AngelClientQuotaRegistry:
    """Get a process-wide quota registry stable across SmartConnect reauth."""
    key = str(client_code or "").strip().upper() or "__UNKNOWN_CLIENT__"
    with _CLIENT_QUOTAS_LOCK:
        quota = _CLIENT_QUOTAS.get(key)
        if quota is None:
            quota = AngelClientQuotaRegistry()
            _CLIENT_QUOTAS[key] = quota
        return quota
