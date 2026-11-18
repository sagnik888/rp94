"""Failover Broker Manager: Primary Upstox with 30-second Angel One Fallback.

Routes all execution and reconciliation to primary Upstox broker.
If Upstox fails continuously for more than 30 seconds, automatically
fails over to Angel One SmartAPI until Upstox recovers.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from trading.broker import Broker
from trading.upstox_broker import UpstoxBroker
from trading.angelone_broker import AngelOneBroker


class FailoverBroker(Broker):
    """Dynamic failover wrapper: Primary Upstox, Fallback Angel One after 30s."""

    def __init__(
        self,
        primary: Optional[UpstoxBroker] = None,
        fallback: Optional[AngelOneBroker] = None,
        failover_threshold_seconds: float = 30.0,
    ):
        super().__init__()
        self.primary = primary or UpstoxBroker()
        self.fallback = fallback or AngelOneBroker()
        self.threshold_seconds = float(failover_threshold_seconds)

        self._lock = threading.RLock()
        self._last_failover_logged = 0.0
        self._last_recovery_logged = 0.0

    @property
    def is_failover_active(self) -> bool:
        """True if primary Upstox has failed for > 30 seconds."""
        if not self.primary.is_healthy(threshold_seconds=self.threshold_seconds):
            now = time.monotonic()
            if now - self._last_failover_logged > 30.0:
                logger.warning(
                    f"⚠️ [BROKER FAILOVER ACTIVE] Upstox primary failed for {self.primary.failure_duration():.1f}s (>30s). "
                    "Routing order execution to Angel One backup."
                )
                self._last_failover_logged = now
            return True
        else:
            if self._last_failover_logged > 0 and (time.monotonic() - self._last_recovery_logged > 30.0):
                logger.info("✅ [BROKER RECOVERED] Upstox primary restored. Returning to primary broker.")
                self._last_recovery_logged = time.monotonic()
                self._last_failover_logged = 0.0
            return False

    @property
    def active_broker(self) -> Broker:
        """Returns fallback AngelOne if primary down > 30s, else primary Upstox."""
        return self.fallback if self.is_failover_active else self.primary

    def get_failover_status(self) -> Dict[str, Any]:
        """Telemetry status of broker health and failover state."""
        failover = self.is_failover_active
        return {
            "primary": "UPSTOX",
            "fallback": "ANGELONE",
            "active_broker": "ANGELONE" if failover else "UPSTOX",
            "failover_active": failover,
            "upstox_healthy": self.primary.is_healthy(self.threshold_seconds),
            "upstox_failure_seconds": round(self.primary.failure_duration(), 1),
            "threshold_seconds": self.threshold_seconds,
        }

    # ── Forwarded Broker interface methods ───────────────────────────────────

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        return self.active_broker.place_order(*args, **kwargs)

    def place_order_async(self, *args: Any, **kwargs: Any) -> Any:
        return self.active_broker.place_order_async(*args, **kwargs)

    def close_order(self, *args: Any, **kwargs: Any) -> Any:
        return self.active_broker.close_order(*args, **kwargs)

    def get_positions(self) -> Optional[Dict]:
        return self.active_broker.get_positions()

    def get_order_status(self, order_id: str) -> Dict:
        # Check primary first, then fallback
        res = self.primary.get_order_status(order_id)
        if res.get("orderstatus") != "UNKNOWN":
            return res
        return self.fallback.get_order_status(order_id)

    def find_working_protective_stop(self, *args: Any, **kwargs: Any) -> Optional[str]:
        return self.active_broker.find_working_protective_stop(*args, **kwargs)

    def inspect_working_protective_stop(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.active_broker.inspect_working_protective_stop(*args, **kwargs)

    def inspect_recent_entry_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.active_broker.inspect_recent_entry_order(*args, **kwargs)

    def cancel_order(self, *args: Any, **kwargs: Any) -> bool:
        return self.active_broker.cancel_order(*args, **kwargs)

    def cancel_order_confirmed(self, *args: Any, **kwargs: Any) -> bool:
        return self.active_broker.cancel_order_confirmed(*args, **kwargs)

    def preflight_entry_margin(self) -> Dict[str, Any]:
        return self.active_broker.preflight_entry_margin()

    def place_protective_stop(self, *args: Any, **kwargs: Any) -> Optional[str]:
        return self.active_broker.place_protective_stop(*args, **kwargs)

    def modify_protective_stop(self, *args: Any, **kwargs: Any) -> bool:
        return self.active_broker.modify_protective_stop(*args, **kwargs)

    def acquire_execution_lease(self) -> bool:
        return getattr(self.primary, "acquire_execution_lease", lambda: True)()

    def execution_lease_established(self) -> bool:
        return getattr(self.primary, "execution_lease_established", lambda: True)()

    def release_execution_lease(self) -> None:
        return getattr(self.primary, "release_execution_lease", lambda: None)()

    def has_execution_lease(self) -> bool:
        return getattr(self.primary, "has_execution_lease", lambda: True)()

    def execution_lease_block_reason(self) -> str:
        return getattr(self.primary, "execution_lease_block_reason", lambda: "")()

    def set_execution_authority(self, provider: Any) -> None:
        if hasattr(self.primary, "set_execution_authority"):
            self.primary.set_execution_authority(provider)
        if hasattr(self.fallback, "set_execution_authority"):
            self.fallback.set_execution_authority(provider)
