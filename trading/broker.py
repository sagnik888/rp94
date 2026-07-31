"""
Broker Interface — Abstracting order execution
═══════════════════════════════════════════════════════════════
"""

import time
import threading
import logging
import uuid
import re
from collections import deque
from typing import Any, Dict, Optional
from loguru import logger
from trading.api_rate_limiter import get_shared_api_limiter
from trading.account_execution_lease import AccountExecutionLease, lease_owner_description
from trading.order_retry import BrokerOrderAmbiguous
from trading.secure_logging import install_smartapi_log_redaction
try:
    from SmartApi import SmartConnect
    install_smartapi_log_redaction()
    try:
        import logzero
        logzero.logger.setLevel(logging.CRITICAL)
    except Exception:
        pass
    HAS_SMARTAPI = True
except ImportError:
    SmartConnect = None
    HAS_SMARTAPI = False


from concurrent.futures import ThreadPoolExecutor

class Broker:
    """Base Broker Interface"""
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=5)

    def place_order(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "CARRYFORWARD",
        price: float = 0.0,
        touch_quote: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        raise NotImplementedError

    def place_order_async(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "CARRYFORWARD",
        price: float = 0.0,
        callback=None,
        touch_quote: Optional[Dict[str, Any]] = None,
    ):
        """Fire-and-forget async order placement"""
        def task():
            try:
                res = self.place_order(symbol, token, qty, side, product_type, price, touch_quote=touch_quote)
                if callback: callback(res)
                return res
            except Exception as e:
                logger.error(f"Async Order Task Error: {e}")
                return None
        return self._executor.submit(task)

    def close_order(self, symbol: str, token: str, qty: int, side: str, product_type: str = "CARRYFORWARD", price: float = 0.0) -> Optional[str]:
        raise NotImplementedError

    def get_positions(self) -> Optional[Dict]:
        """Fetch real-time position book from broker"""
        raise NotImplementedError

    def get_order_status(self, order_id: str) -> Optional[Dict]:
        return None

    def find_working_protective_stop(
        self, symbol: str, token: str, side: str, quantity: int = 0
    ) -> str:
        """Return the order id of an existing working exchange-side STOPLOSS for this
        contract, or "" if none. Overridden by real brokers; keeps protective-stop
        placement idempotent across retries and concurrent callers."""
        return ""

    def inspect_working_protective_stop(
        self,
        symbol: str,
        token: str,
        side: str,
        quantity: int = 0,
        product_type: str = "CARRYFORWARD",
        trigger_price: float = 0.0,
    ) -> Dict[str, Any]:
        """Return a tri-state protective-order inspection.

        ``UNKNOWN`` is deliberately different from ``NONE``.  A caller must not
        place another stop when the order book could not be inspected because an
        existing exchange order may merely be invisible locally.
        """
        return {
            "state": "UNKNOWN",
            "order_id": "",
            "reason": "broker does not support protective-order inspection",
            "matches": [],
        }

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        return False

    def cancel_order_confirmed(
        self,
        order_id: str,
        variety: str = "NORMAL",
        timeout_seconds: float = 2.0,
        poll_seconds: float = 0.10,
    ) -> Optional[bool]:
        """Cancel and prove a terminal non-fill state.

        Returns ``True`` only for a broker-confirmed safe terminal state,
        ``False`` when the order filled, and ``None`` when its final state is
        unknown.  A successful cancel API acknowledgement alone is not proof.
        """
        if not order_id:
            return True
        if not self.cancel_order(order_id, variety):
            return False
        getter = getattr(self, "get_order_status", None)
        if not callable(getter):
            return None
        deadline = time.monotonic() + max(0.0, float(timeout_seconds or 0.0))
        safe_terminal = {"cancelled", "canceled", "rejected", "expired", "lapsed"}
        filled_terminal = {"complete", "completed", "filled"}
        while True:
            row = getter(str(order_id))
            if isinstance(row, dict):
                status = str(
                    row.get("orderstatus")
                    or row.get("status")
                    or row.get("orderStatus")
                    or ""
                ).strip().lower().replace("_", " ")
                if status in safe_terminal:
                    return True
                if status in filled_terminal:
                    return False
            if time.monotonic() >= deadline:
                return None
            time.sleep(max(0.01, float(poll_seconds or 0.10)))

    def preflight_entry_margin(self, **kwargs) -> Dict[str, Any]:
        """Optional live-entry funds gate. Concrete real brokers override it."""
        return {
            "ok": True,
            "reservation_id": "",
            "basis": "UNSUPPORTED_SIMULATION",
            "limitation": "broker does not require live margin preflight",
        }

    def release_entry_margin_reservation(self, reservation_id: str) -> None:
        return None

    def place_protective_stop(
        self, symbol: str, token: str, qty: int, side: str, trigger_price: float,
        product_type: str = "CARRYFORWARD",
    ) -> Optional[str]:
        return None

    def modify_protective_stop(
        self, order_id: str, symbol: str, token: str, qty: int, side: str,
        trigger_price: float, product_type: str = "CARRYFORWARD",
    ) -> bool:
        return False

class SignalSimBroker(Broker):
    """Simulation Broker for Scanned Signals"""
    def __init__(self):
        super().__init__()

    def place_order(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "CARRYFORWARD",
        price: float = 0.0,
        touch_quote: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        logger.debug(f"SIGNAL SIMULATION: {side} {qty} {symbol} (Real Data Analysed) | Price: {price}")
        return "SIM_ORD_" + symbol

    def close_order(self, symbol: str, token: str, qty: int, side: str, product_type: str = "CARRYFORWARD", price: float = 0.0) -> Optional[str]:
        logger.debug(f"SIGNAL SIMULATION CLOSE: {side} {qty} {symbol} | Price: {price}")
        return "SIM_EXIT_" + symbol

    def get_positions(self) -> Optional[Dict]:
        return {"data": []}

    def get_order_status(self, order_id: str) -> Optional[Dict]:
        return {"orderid": order_id, "status": "complete", "orderstatus": "complete"}

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        return True

    def cancel_order_confirmed(
        self,
        order_id: str,
        variety: str = "NORMAL",
        timeout_seconds: float = 2.0,
        poll_seconds: float = 0.10,
    ) -> Optional[bool]:
        return True

    def inspect_working_protective_stop(
        self,
        symbol: str,
        token: str,
        side: str,
        quantity: int = 0,
        product_type: str = "CARRYFORWARD",
        trigger_price: float = 0.0,
    ) -> Dict[str, Any]:
        return {"state": "NONE", "order_id": "", "reason": "simulation", "matches": []}

    def place_protective_stop(
        self, symbol: str, token: str, qty: int, side: str, trigger_price: float,
        product_type: str = "CARRYFORWARD",
    ) -> Optional[str]:
        return f"SIM_SL_{symbol}"

    def modify_protective_stop(
        self, order_id: str, symbol: str, token: str, qty: int, side: str,
        trigger_price: float, product_type: str = "CARRYFORWARD",
    ) -> bool:
        return True

class SmartApiBroker(Broker):
    """AngelOne SmartAPI Broker"""
    requires_contract_master_validation = True
    requires_protective_stop_metadata_validation = True
    requires_entry_margin_preflight = True

    def __init__(
        self,
        smart_api: SmartConnect,
        account_id: str = "",
        instance_id: str = "",
    ):
        super().__init__()
        install_smartapi_log_redaction()
        self.api = smart_api
        self._order_lock = threading.Lock()
        self._position_backoff_until = 0.0
        self._position_backoff_failures = 0
        self._api_limiter = get_shared_api_limiter(self.api)
        self._entry_order_times = deque()
        self._entry_kill_latched_until = 0.0
        self._margin_lock = threading.RLock()
        self._funds_cache: Dict[str, Any] = {}
        self._margin_requirement_cache: Dict[tuple, Dict[str, Any]] = {}
        self._margin_reservations: Dict[str, Dict[str, Any]] = {}
        self._execution_lease = (
            AccountExecutionLease(account_id, instance_id)
            if str(account_id or "").strip()
            else None
        )

    def replace_api(self, smart_api: SmartConnect) -> None:
        """Atomically rebind execution to a freshly authenticated SDK session."""
        with self._order_lock:
            self.api = smart_api
            self._api_limiter = get_shared_api_limiter(self.api)
        with self._margin_lock:
            self._funds_cache.clear()
            self._margin_requirement_cache.clear()
            self._margin_reservations.clear()

    def acquire_execution_lease(self) -> bool:
        lease = self._execution_lease
        return True if lease is None else bool(lease.acquire())

    def release_execution_lease(self) -> None:
        lease = self._execution_lease
        if lease is not None:
            lease.release()

    def has_execution_lease(self) -> bool:
        lease = self._execution_lease
        return True if lease is None else bool(lease.held)

    def execution_lease_block_reason(self) -> str:
        lease = self._execution_lease
        if lease is None or lease.held:
            return ""
        return (
            "REAL broker account is owned by "
            + lease_owner_description(lease.owner_metadata)
        )

    def _require_account_execution_lease(self, action: str) -> None:
        if self.has_execution_lease():
            return
        # Claim it on demand before refusing. The lease was previously acquired ONLY at
        # startup in main.py, and only when trading_mode was already REAL — so a process
        # that reached REAL through a runtime mode switch never asked for it and had every
        # order blocked by "owned by another UT1 process" while the OS lock sat FREE and
        # the named owner was a pid that had exited the previous day (the message reads
        # stale metadata out of the lock file).
        #
        # This cannot let two processes execute at once: the underlying acquire is an
        # exclusive non-blocking OS file lock, so a genuine live holder still refuses us
        # here and we fail closed exactly as before.
        if self.acquire_execution_lease():
            logger.warning(
                f"Acquired the REAL account execution lease on demand before {action}; "
                "it had not been claimed by this process."
            )
            return
        reason = self.execution_lease_block_reason()
        raise PermissionError(f"{action} blocked: {reason}")

    @staticmethod
    def _exchange_for_symbol(symbol: str) -> str:
        sym = str(symbol or "").upper()
        return "BFO" if "SENSEX" in sym or "BANKEX" in sym else "NFO"

    @staticmethod
    def _tick_price(value: float) -> float:
        return round(max(0.05, round(float(value) / 0.05) * 0.05), 2)

    @staticmethod
    def _positive_float(value: Any) -> float:
        try:
            number = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return number if number > 0 else 0.0

    @staticmethod
    def _finite_float(value: Any) -> Optional[float]:
        try:
            normalized = value.replace(",", "") if isinstance(value, str) else value
            number = float(normalized)
        except (TypeError, ValueError):
            return None
        if number != number or number in {float("inf"), float("-inf")}:
            return None
        return number

    @staticmethod
    def _nested_numeric(payload: Any, preferred_keys) -> Optional[float]:
        preferred = {
            re.sub(r"[^a-z0-9]", "", str(key).lower()) for key in preferred_keys
        }
        queue_items = [payload]
        while queue_items:
            current = queue_items.pop(0)
            if isinstance(current, dict):
                for key, value in current.items():
                    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                    if normalized in preferred:
                        number = SmartApiBroker._finite_float(value)
                        if number is not None:
                            return number
                queue_items.extend(current.values())
            elif isinstance(current, (list, tuple)):
                queue_items.extend(current)
        return None

    def _cleanup_margin_reservations(self, now: float) -> None:
        expired = [
            reservation_id
            for reservation_id, row in self._margin_reservations.items()
            if float(row.get("expires_at", 0.0) or 0.0) <= now
        ]
        for reservation_id in expired:
            self._margin_reservations.pop(reservation_id, None)

    def _available_funds(self, cache_seconds: float) -> tuple[Optional[float], str]:
        now = time.monotonic()
        cached_at = float(self._funds_cache.get("fetched_at", 0.0) or 0.0)
        if now - cached_at <= max(0.0, float(cache_seconds or 0.0)):
            return self._funds_cache.get("available"), str(
                self._funds_cache.get("basis") or "RMS_CACHE"
            )
        if not callable(getattr(self.api, "rmsLimit", None)):
            return None, "RMS_API_UNAVAILABLE"
        try:
            response = self._api_call(
                "entry funds preflight",
                lambda: getattr(self.api, "rmsLimit")(),
                timeout=2.0,
            )
        except Exception as exc:
            logger.error(f"Entry funds preflight failed: {exc}")
            return None, f"RMS_API_ERROR:{exc}"
        if not isinstance(response, dict) or response.get("status") is False or response.get("errorcode"):
            return None, f"RMS_API_REJECTED:{response}"
        # Angel RMS payloads vary by SDK/API revision. availablecash is the
        # preferred conservative field; net is used only when it is absent.
        available = self._nested_numeric(response, ("availablecash",))
        field = "availablecash"
        if available is None:
            available = self._nested_numeric(response, ("net",))
            field = "net"
        if available is None:
            available = self._nested_numeric(
                response, ("availablelimitmargin", "availablemargin")
            )
            field = "availablelimitmargin"
        if available is None or available < 0:
            return None, "RMS_AVAILABLE_FUNDS_MISSING"
        self._funds_cache = {
            "available": float(available),
            "basis": f"RMS_{field.upper()}",
            "fetched_at": now,
        }
        return float(available), f"RMS_{field.upper()}"

    def _exact_or_estimated_margin(
        self,
        *,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str,
        price: float,
        inst_type: str,
        cache_seconds: float,
    ) -> tuple[Optional[float], str, str]:
        now = time.monotonic()
        cache_key = (
            str(symbol), str(token), int(qty), str(side).upper(),
            str(product_type).upper(), round(float(price or 0.0), 2),
        )
        cached = self._margin_requirement_cache.get(cache_key)
        if cached and now - float(cached.get("fetched_at", 0.0) or 0.0) <= max(
            0.0, float(cache_seconds or 0.0)
        ):
            return (
                float(cached["required"]),
                str(cached["basis"]),
                str(cached.get("limitation") or ""),
            )

        limitation = ""
        if callable(getattr(self.api, "getMarginApi", None)):
            payload = {
                "positions": [{
                    "exchange": self._exchange_for_symbol(symbol),
                    "qty": int(qty),
                    "price": float(price or 0.0),
                    "productType": str(product_type or "CARRYFORWARD").upper(),
                    "token": str(token),
                    "tradeType": str(side or "").upper(),
                }]
            }
            try:
                response = self._api_call(
                    "exact entry margin preflight",
                    lambda: getattr(self.api, "getMarginApi")(payload),
                    timeout=2.0,
                )
                if (
                    isinstance(response, dict)
                    and response.get("status") is not False
                    and not response.get("errorcode")
                ):
                    required = self._nested_numeric(
                        response,
                        (
                            "totalMarginRequired",
                            "requiredMargin",
                            "marginRequired",
                            "totalMargin",
                        ),
                    )
                    if required is not None and required > 0:
                        result = {
                            "required": float(required),
                            "basis": "SMARTAPI_EXACT_MARGIN",
                            "limitation": "",
                            "fetched_at": now,
                        }
                        self._margin_requirement_cache[cache_key] = result
                        return float(required), result["basis"], ""
                limitation = "exact SmartAPI margin response was unavailable/invalid"
            except Exception as exc:
                limitation = f"exact SmartAPI margin call unavailable: {exc}"
        else:
            limitation = "exact SmartAPI margin method unavailable"

        # Conservative fallback. For long options this is the full premium
        # payable. For futures/short legs, full notional intentionally
        # overestimates broker SPAN/exposure rather than allowing an unsafe entry.
        estimate_price = float(price or 0.0)
        if estimate_price <= 0 or int(qty or 0) <= 0:
            return None, "ESTIMATE_UNAVAILABLE", limitation
        required = estimate_price * int(qty)
        basis = (
            "OPTION_PREMIUM_ESTIMATE"
            if str(inst_type or "").upper() == "OPT" and str(side).upper() == "BUY"
            else "FULL_NOTIONAL_ESTIMATE"
        )
        result = {
            "required": float(required),
            "basis": basis,
            "limitation": limitation,
            "fetched_at": now,
        }
        self._margin_requirement_cache[cache_key] = result
        return float(required), basis, limitation

    def preflight_entry_margin(
        self,
        *,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str,
        price: float,
        inst_type: str,
        buffer_pct: float = 10.0,
        cache_seconds: float = 2.0,
    ) -> Dict[str, Any]:
        """Fail-closed available-margin gate with short-lived reservations."""
        with self._margin_lock:
            now = time.monotonic()
            self._cleanup_margin_reservations(now)
            required, requirement_basis, limitation = self._exact_or_estimated_margin(
                symbol=symbol,
                token=token,
                qty=qty,
                side=side,
                product_type=product_type,
                price=price,
                inst_type=inst_type,
                cache_seconds=cache_seconds,
            )
            available, funds_basis = self._available_funds(cache_seconds)
            if required is None or available is None:
                return {
                    "ok": False,
                    "reservation_id": "",
                    "available": available,
                    "required": required,
                    "required_with_buffer": None,
                    "basis": f"{requirement_basis}+{funds_basis}",
                    "limitation": limitation,
                    "reason": "available funds or required margin could not be verified",
                }
            required_with_buffer = float(required) * (
                1.0 + max(0.0, float(buffer_pct or 0.0)) / 100.0
            )
            reserved = sum(
                float(row.get("amount", 0.0) or 0.0)
                for row in self._margin_reservations.values()
            )
            effective_available = max(0.0, float(available) - reserved)
            if effective_available + 1e-9 < required_with_buffer:
                return {
                    "ok": False,
                    "reservation_id": "",
                    "available": float(available),
                    "effective_available": effective_available,
                    "reserved": reserved,
                    "required": float(required),
                    "required_with_buffer": required_with_buffer,
                    "basis": f"{requirement_basis}+{funds_basis}",
                    "limitation": limitation,
                    "reason": "insufficient available funds after margin buffer",
                }
            reservation_id = uuid.uuid4().hex
            self._margin_reservations[reservation_id] = {
                "amount": required_with_buffer,
                "symbol": str(symbol),
                "expires_at": now + 120.0,
            }
            return {
                "ok": True,
                "reservation_id": reservation_id,
                "available": float(available),
                "effective_available": effective_available,
                "reserved": reserved,
                "required": float(required),
                "required_with_buffer": required_with_buffer,
                "basis": f"{requirement_basis}+{funds_basis}",
                "limitation": limitation,
                "reason": "margin preflight passed",
            }

    def release_entry_margin_reservation(self, reservation_id: str) -> None:
        if not reservation_id:
            return
        with self._margin_lock:
            self._margin_reservations.pop(str(reservation_id), None)
            # A submitted/rejected order may have changed funds; force the next
            # entry batch to refresh RMS rather than trusting pre-order cash.
            self._funds_cache.clear()

    def _ltp_anchor_limit(self, price: float, side: str, is_option: bool, settings) -> float:
        if price <= 0:
            return 0.0
        if is_option:
            buffer_pct = (
                float(getattr(settings, "option_limit_buffer_pct", 0.40) or 0.40) / 100.0
                if settings
                else 0.004
            )
            min_ticks = int(getattr(settings, "option_limit_buffer_min_ticks", 2) or 2) if settings else 2
            buffer_amt = max(price * buffer_pct, min_ticks * 0.05)
        else:
            buffer_bps = (
                float(getattr(settings, "fut_limit_buffer_bps", 5.0) or 5.0) / 10000.0
                if settings
                else 0.0005
            )
            buffer_amt = price * buffer_bps
        if str(side).upper() == "BUY":
            return self._tick_price(price + buffer_amt)
        return self._tick_price(max(0.05, price - buffer_amt))

    def _touch_quote_usable(
        self,
        touch_quote: Optional[Dict[str, Any]],
        settings,
    ) -> tuple[bool, str, Dict[str, float]]:
        if not isinstance(touch_quote, dict):
            return False, "missing", {}
        bid = self._positive_float(touch_quote.get("bid"))
        ask = self._positive_float(touch_quote.get("ask"))
        values = {"bid": bid, "ask": ask}
        if bid <= 0 or ask <= 0:
            return False, "missing_bid_ask", values
        if ask < bid:
            return False, "crossed_book", values
        age = self._positive_float(touch_quote.get("age_seconds"))
        max_age = (
            float(getattr(settings, "execution_touch_quote_max_age_seconds", 1.5) or 1.5)
            if settings
            else 1.5
        )
        values["age_seconds"] = age
        if age > max_age:
            return False, "stale", values
        spread = max(0.0, ask - bid)
        mid = (ask + bid) / 2.0
        spread_pct = (spread / mid * 100.0) if mid > 0 else 999.0
        spread_ticks = spread / 0.05
        values.update({"spread": spread, "spread_pct": spread_pct, "spread_ticks": spread_ticks})
        max_spread_pct = (
            float(getattr(settings, "option_touch_quote_max_spread_pct", 1.0) or 1.0)
            if settings
            else 1.0
        )
        max_spread_ticks = (
            int(getattr(settings, "option_touch_quote_max_spread_ticks", 40) or 40)
            if settings
            else 40
        )
        if spread_pct > max_spread_pct or spread_ticks > max_spread_ticks:
            return False, "wide_spread", values
        return True, "ok", values

    def _touch_anchor_limit(self, side: str, values: Dict[str, float], settings) -> float:
        extra_ticks = int(getattr(settings, "option_touch_limit_extra_ticks", 1) or 1) if settings else 1
        extra = max(0, extra_ticks) * 0.05
        if str(side).upper() == "BUY":
            return self._tick_price(values.get("ask", 0.0) + extra)
        return self._tick_price(max(0.05, values.get("bid", 0.0) - extra))

    def _api_call(self, context: str, fn, timeout: float = 5.0):
        # The API object and its limiter are one atomic session pair. Holding
        # the rebinding lock from limiter begin through end prevents reauth from
        # pairing an old begin with a new limiter's end or executing a method on
        # a stale SmartConnect instance.
        with self._order_lock:
            limiter = self._api_limiter
            if not limiter.begin_priority_call(timeout=timeout):
                raise TimeoutError(
                    f"Shared AngelOne REST limiter timed out before {context}"
                )
            try:
                return fn()
            finally:
                limiter.end_priority_call()

    def _entry_rate_allowed(self) -> bool:
        from config.settings import get_settings

        settings = get_settings()
        now = time.monotonic()
        if now < self._entry_kill_latched_until:
            return False
        window = max(1.0, float(getattr(settings, "entry_order_rate_window_seconds", 60.0) or 60.0))
        limit = max(1, int(getattr(settings, "max_entry_orders_per_minute", 6) or 6))
        while self._entry_order_times and now - self._entry_order_times[0] >= window:
            self._entry_order_times.popleft()
        if len(self._entry_order_times) >= limit:
            latch = max(10.0, float(getattr(settings, "entry_order_kill_latch_seconds", 300.0) or 300.0))
            self._entry_kill_latched_until = now + latch
            logger.critical(
                f"ENTRY ORDER RATE KILL-SWITCH: {len(self._entry_order_times)} orders "
                f"inside {window:.0f}s; entries blocked for {latch:.0f}s."
            )
            return False
        self._entry_order_times.append(now)
        return True

    def _position_should_backoff(self, payload) -> bool:
        text = str(payload or "").lower()
        markers = (
            "exceeding access rate",
            "access rate",
            "rate limit",
            "too many requests",
            "read timed out",
            "connect timeout",
            "connection timed out",
            "timed out",
            "timeout",
        )
        return any(marker in text for marker in markers)

    def _mark_position_backoff(self, payload) -> None:
        failures = int(getattr(self, "_position_backoff_failures", 0) or 0) + 1
        delay = min(60.0, 12.0 * (2 ** max(0, failures - 1)))
        self._position_backoff_failures = failures
        self._position_backoff_until = time.time() + delay
        logger.warning(f"Broker position REST backoff {delay:.1f}s after failure: {payload}")

    def place_order(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "CARRYFORWARD",
        price: float = 0.0,
        touch_quote: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        try:
            self._require_account_execution_lease("entry order")
            if not self._entry_rate_allowed():
                return None
            # ══ Dynamic Exchange Resolution ══
            # SENSEX Options/Futures live on BFO (BSE), NIFTY/BANKNIFTY on NFO (NSE)
            exchange = self._exchange_for_symbol(symbol)

            # Use a MARKETABLE LIMIT wherever a reference price exists. The safe
            # default remains signal/LTP anchored. If guarded touch-quote execution
            # is enabled and the book is fresh/tight, option limits anchor to
            # ask+ticks (BUY) or bid-ticks (SELL). Shadow mode logs both limits.
            is_option = "CE" in symbol.upper() or "PE" in symbol.upper()
            try:
                from config.settings import get_settings
                settings = get_settings()
            except Exception:
                settings = None
            fut_protection = (
                bool(getattr(settings, "fut_entry_protection_limit_enabled", True))
                if settings is not None
                else True
            )
            order_type = "LIMIT" if (price > 0.0 and (is_option or fut_protection)) else "MARKET"

            limit_price = price
            if order_type == "LIMIT":
                ltp_limit = self._ltp_anchor_limit(float(price or 0.0), side, is_option, settings)
                limit_price = ltp_limit
                anchor = "ltp"
                touch_limit = 0.0
                touch_reason = "disabled"
                if is_option:
                    touch_ok, touch_reason, touch_values = self._touch_quote_usable(touch_quote, settings)
                    if touch_ok:
                        touch_limit = self._touch_anchor_limit(side, touch_values, settings)
                    use_touch = bool(getattr(settings, "execution_use_touch_quote", False)) if settings else False
                    shadow = bool(getattr(settings, "execution_touch_quote_shadow_enabled", True)) if settings else True
                    strict = bool(getattr(settings, "execution_touch_quote_strict", False)) if settings else False
                    if shadow or use_touch:
                        logger.info(
                            f"TOUCH EXECUTION SHADOW: {side} {symbol} "
                            f"ref={float(price or 0.0):.2f} ltp_limit={ltp_limit:.2f} "
                            f"bid={touch_values.get('bid', 0.0):.2f} "
                            f"ask={touch_values.get('ask', 0.0):.2f} "
                            f"touch_limit={touch_limit:.2f} usable={touch_ok} reason={touch_reason}"
                        )
                    if use_touch:
                        if touch_ok and touch_limit > 0:
                            limit_price = touch_limit
                            anchor = "touch"
                        elif strict:
                            logger.warning(
                                f"Touch-quote execution blocked {side} {symbol}: {touch_reason}"
                            )
                            return None
                self._last_execution_preview = {
                    "symbol": symbol,
                    "side": side,
                    "reference_price": float(price or 0.0),
                    "ltp_limit_price": ltp_limit,
                    "touch_limit_price": touch_limit,
                    "limit_price": limit_price,
                    "anchor": anchor,
                    "touch_reason": touch_reason,
                }

            params = {
                "variety": "NORMAL",
                "tradingsymbol": symbol,
                "symboltoken": token,
                "transactiontype": side, # BUY or SELL
                "exchange": exchange,
                "ordertype": order_type,
                "producttype": product_type,
                "duration": "DAY",
                "quantity": str(qty)
            }
            if order_type == "LIMIT":
                params["price"] = f"{limit_price:.2f}"

            logger.info(f"PLACING REAL ORDER: {side} {qty} {symbol} on {exchange} | Type: {order_type} | Price: {limit_price:.2f}")

            try:
                response = self._api_call(
                    "place entry order",
                    lambda: (
                        getattr(self.api, "placeOrderFullResponse")(params)
                        if callable(getattr(self.api, "placeOrderFullResponse", None))
                        else self.api.placeOrder(params)
                    ),
                )
            except Exception as api_err:
                logger.error(f"Broker API error during placeOrder: {api_err}")
                raise BrokerOrderAmbiguous(
                    f"Broker acknowledgement ambiguous after placeOrder exception: {api_err}"
                ) from api_err

            if isinstance(response, dict):
                if response.get("status") is False or response.get("errorcode"):
                    logger.error(f"Broker rejected order: {response}")
                    return None
                data = response.get("data", response)
                if isinstance(data, dict):
                    order_id = str(
                        data.get("orderid")
                        or data.get("uniqueorderid")
                        or ""
                    )
                else:
                    order_id = str(data or "")
            else:
                order_id = str(response or "")

            if not order_id or order_id == "None":
                raise BrokerOrderAmbiguous(
                    f"Broker acknowledgement ambiguous; no order id returned: {response}"
                )

            logger.success(f"REAL ORDER PLACED: {order_id} ({exchange})")
            return order_id
        except Exception as e:
            if isinstance(e, BrokerOrderAmbiguous):
                raise
            logger.error(f"Order placement failed on {exchange if 'exchange' in locals() else 'Unknown'}: {e}")
            return None

    def close_order(self, symbol: str, token: str, qty: int, side: str, product_type: str = "CARRYFORWARD", price: float = 0.0) -> Optional[str]:
        """Place a risk-reducing MARKET exit outside the entry-rate kill-switch."""
        self._require_account_execution_lease("exit order")
        params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": side.upper(),
            "exchange": self._exchange_for_symbol(symbol),
            "ordertype": "MARKET",
            "producttype": product_type,
            "duration": "DAY",
            "quantity": str(qty),
        }
        try:
            response = self._api_call(
                "place exit order",
                lambda: (
                    getattr(self.api, "placeOrderFullResponse")(params)
                    if callable(getattr(self.api, "placeOrderFullResponse", None))
                    else self.api.placeOrder(params)
                ),
            )
            if isinstance(response, dict):
                if response.get("status") is False or response.get("errorcode"):
                    return None
                data = response.get("data", response)
                order_id = str(data.get("orderid") or data.get("uniqueorderid") or "") if isinstance(data, dict) else str(data or "")
            else:
                order_id = str(response or "")
            # A successful response without an exchange id may still have reached
            # the market. Retrying it could create a duplicate market exit.
            if not order_id or order_id == "None":
                raise BrokerOrderAmbiguous(
                    f"Broker exit acknowledgement ambiguous; no order id returned: {response}"
                )
            return order_id
        except Exception as exc:
            logger.error(f"Exit order placement failed for {symbol}: {exc}")
            raise BrokerOrderAmbiguous(f"Broker exit acknowledgement ambiguous: {exc}") from exc

    def get_order_status(self, order_id: str) -> Optional[Dict]:
        if not order_id:
            return None
        try:
            response = self._api_call(
                "fetch order book", lambda: self.api.orderBook(), timeout=2.0
            )
            rows = response.get("data", []) if isinstance(response, dict) else []
            for row in rows or []:
                if str(row.get("orderid") or "") == str(order_id) or str(row.get("uniqueorderid") or "") == str(order_id):
                    return dict(row)
            return None
        except Exception as exc:
            logger.warning(f"Order-status poll failed for {order_id}: {exc}")
            return None

    def find_working_protective_stop(
        self, symbol: str, token: str, side: str, quantity: int = 0
    ) -> str:
        """Return the order id of an existing working exchange-side STOPLOSS for this
        contract, or "" if none. Keeps protective-stop placement idempotent so a lost
        acknowledgement or a concurrent caller can never create a duplicate SL-M."""
        result = self.inspect_working_protective_stop(
            symbol,
            token,
            side,
            quantity=quantity,
            product_type="",
        )
        return str(result.get("order_id") or "") if result.get("state") == "FOUND" else ""

    def inspect_working_protective_stop(
        self,
        symbol: str,
        token: str,
        side: str,
        quantity: int = 0,
        product_type: str = "CARRYFORWARD",
        trigger_price: float = 0.0,
    ) -> Dict[str, Any]:
        """Inspect the order book without collapsing failure into "no stop"."""
        if not symbol:
            return {
                "state": "UNKNOWN",
                "order_id": "",
                "reason": "protective-order symbol is empty",
                "matches": [],
            }
        try:
            response = self._api_call(
                "scan order book for stop",
                lambda: self.api.orderBook(),
                timeout=2.0,
            )
        except Exception as exc:
            logger.warning(f"Protective-stop order-book scan failed for {symbol}: {exc}")
            return {
                "state": "UNKNOWN",
                "order_id": "",
                "reason": f"order-book scan failed: {exc}",
                "matches": [],
            }
        if (
            not isinstance(response, dict)
            or response.get("status") is False
            or response.get("errorcode")
            or not isinstance(response.get("data", []), list)
        ):
            return {
                "state": "UNKNOWN",
                "order_id": "",
                "reason": f"order-book response is invalid/rejected: {response}",
                "matches": [],
            }
        rows = response.get("data", [])
        terminal = {"complete", "completed", "filled", "cancelled", "canceled", "rejected", "expired", "lapsed"}
        want_side = str(side or "").upper()
        want_qty = max(0, int(quantity or 0))
        want_product = str(product_type or "").upper()
        want_trigger = (
            self._tick_price(float(trigger_price or 0.0))
            if float(trigger_price or 0.0) > 0
            else 0.0
        )
        exact_matches = []
        stale_trigger_matches = []
        conflicts = []
        unknown_matches = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("tradingsymbol") or "") != str(symbol):
                continue
            variety = str(row.get("variety") or "").upper()
            ordertype = str(row.get("ordertype") or "").upper()
            if "STOPLOSS" not in variety and "STOPLOSS" not in ordertype:
                continue
            status = str(row.get("orderstatus") or row.get("status") or "").strip().lower().replace("_", " ")
            if status in terminal:
                continue
            if not status:
                unknown_matches.append(dict(row))
                continue
            row_token = str(row.get("symboltoken") or "")
            row_side = str(row.get("transactiontype") or "").upper()
            row_product = str(row.get("producttype") or "").upper()
            try:
                row_qty = int(float(row.get("quantity") or row.get("unfilledshares") or 0))
            except (TypeError, ValueError):
                row_qty = 0
            row_trigger = self._finite_float(
                row.get("triggerprice")
                if row.get("triggerprice") is not None
                else row.get("triggerPrice")
            )
            oid = str(row.get("orderid") or row.get("uniqueorderid") or "")
            base_metadata_ok = bool(oid)
            base_metadata_ok = base_metadata_ok and (not token or row_token == str(token))
            base_metadata_ok = base_metadata_ok and (not want_side or row_side == want_side)
            base_metadata_ok = base_metadata_ok and (not want_qty or row_qty == want_qty)
            base_metadata_ok = base_metadata_ok and (not want_product or row_product == want_product)
            trigger_ok = (
                not want_trigger
                or (
                    row_trigger is not None
                    and abs(float(row_trigger) - want_trigger) <= 0.011
                )
            )
            if base_metadata_ok and trigger_ok:
                exact_matches.append(dict(row))
            elif base_metadata_ok:
                stale_trigger_matches.append(dict(row))
            else:
                conflicts.append(dict(row))

        if (
            len(exact_matches) == 1
            and not stale_trigger_matches
            and not conflicts
            and not unknown_matches
        ):
            row = exact_matches[0]
            return {
                "state": "FOUND",
                "order_id": str(row.get("orderid") or row.get("uniqueorderid") or ""),
                "reason": "exact working protective stop found",
                "matches": exact_matches,
            }
        if (
            len(exact_matches) > 1
            or len(stale_trigger_matches) > 1
            or (exact_matches and (stale_trigger_matches or conflicts or unknown_matches))
            or (stale_trigger_matches and (conflicts or unknown_matches))
        ):
            return {
                "state": "AMBIGUOUS",
                "order_id": "",
                "reason": "multiple or conflicting working protective stops found",
                "matches": exact_matches + stale_trigger_matches + conflicts + unknown_matches,
            }
        if stale_trigger_matches:
            row = stale_trigger_matches[0]
            return {
                "state": "FOUND_STALE_TRIGGER",
                "order_id": str(row.get("orderid") or row.get("uniqueorderid") or ""),
                "trigger_price": self._finite_float(
                    row.get("triggerprice")
                    if row.get("triggerprice") is not None
                    else row.get("triggerPrice")
                ),
                "reason": "working protective stop has stale trigger price",
                "matches": stale_trigger_matches,
            }
        if unknown_matches:
            return {
                "state": "UNKNOWN",
                "order_id": "",
                "reason": "working protective-order metadata has no status",
                "matches": unknown_matches,
            }
        if conflicts:
            return {
                "state": "CONFLICT",
                "order_id": "",
                "reason": "working stop exists but contract/side/quantity/product metadata differs",
                "matches": conflicts,
            }
        return {
            "state": "NONE",
            "order_id": "",
            "reason": "no working protective stop found",
            "matches": [],
        }

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        if not order_id:
            return True
        self._require_account_execution_lease("order cancellation")
        try:
            response = self._api_call(
                "cancel order",
                lambda: self.api.cancelOrder(str(order_id), str(variety or "NORMAL").upper()),
            )
            return not isinstance(response, dict) or (
                response.get("status") is not False and not response.get("errorcode")
            )
        except Exception as exc:
            logger.error(f"Order cancellation failed for {order_id}: {exc}")
            return False

    def place_protective_stop(
        self, symbol: str, token: str, qty: int, side: str, trigger_price: float,
        product_type: str = "CARRYFORWARD",
    ) -> Optional[str]:
        self._require_account_execution_lease("protective stop")
        trigger = self._tick_price(trigger_price)
        params = {
            "variety": "STOPLOSS",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": side.upper(),
            "exchange": self._exchange_for_symbol(symbol),
            "ordertype": "STOPLOSS_MARKET",
            "producttype": product_type,
            "duration": "DAY",
            "triggerprice": str(trigger),
            "quantity": str(qty),
        }
        try:
            response = self._api_call(
                "place protective stop",
                lambda: (
                    getattr(self.api, "placeOrderFullResponse")(params)
                    if callable(getattr(self.api, "placeOrderFullResponse", None))
                    else self.api.placeOrder(params)
                ),
            )
        except Exception as exc:
            # Audit fix: a STOPLOSS_MARKET can reach and be accepted by the exchange
            # even when the acknowledgement is lost (timeout / 429 / socket drop). Do
            # NOT return None here — that is indistinguishable from a clean reject, so
            # the caller re-places and TWO SL-M orders rest at the exchange; when both
            # trigger, the second opens a naked reversed leg. Signal ambiguity exactly
            # like place_order so the caller reconciles instead of blindly re-placing.
            logger.error(f"Protective stop placement failed for {symbol}: {exc}")
            raise BrokerOrderAmbiguous(
                f"Protective stop acknowledgement ambiguous after exception: {exc}"
            ) from exc
        if isinstance(response, dict):
            if response.get("status") is False or response.get("errorcode"):
                logger.error(f"Broker rejected protective stop: {response}")
                return None
            data = response.get("data", response)
            order_id = (
                str(data.get("orderid") or data.get("uniqueorderid") or "")
                if isinstance(data, dict)
                else str(data or "")
            )
        else:
            order_id = str(response or "")
        if not order_id or order_id == "None":
            raise BrokerOrderAmbiguous(
                f"Protective stop acknowledgement ambiguous; no order id returned: {response}"
            )
        return order_id

    def modify_protective_stop(
        self, order_id: str, symbol: str, token: str, qty: int, side: str,
        trigger_price: float, product_type: str = "CARRYFORWARD",
    ) -> bool:
        self._require_account_execution_lease("protective stop modification")
        trigger = self._tick_price(trigger_price)
        params = {
            "variety": "STOPLOSS",
            "orderid": str(order_id),
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": side.upper(),
            "exchange": self._exchange_for_symbol(symbol),
            "ordertype": "STOPLOSS_MARKET",
            "producttype": product_type,
            "duration": "DAY",
            "triggerprice": str(trigger),
            "quantity": str(qty),
        }
        try:
            response = self._api_call(
                "modify protective stop",
                lambda: self.api.modifyOrder(params),
            )
            return not isinstance(response, dict) or (
                response.get("status") is not False and not response.get("errorcode")
            )
        except Exception as exc:
            logger.error(f"Protective stop modification failed for {symbol}: {exc}")
            return False

    def get_positions(self) -> Optional[Dict]:
        remaining = float(getattr(self, "_position_backoff_until", 0.0) or 0.0) - time.time()
        if remaining > 0:
            logger.debug(f"Skipping broker positions; backoff active for {remaining:.1f}s")
            return None
        try:
            response = self._api_call(
                "fetch positions", lambda: self.api.position(), timeout=2.0
            )
            if isinstance(response, dict):
                if response.get("status") is False or response.get("errorcode"):
                    if self._position_should_backoff(response):
                        self._mark_position_backoff(response)
                    logger.error(f"Broker returned error for positions: {response}")
                    return None
            self._position_backoff_failures = 0
            self._position_backoff_until = 0.0
            return response
        except Exception as e:
            if self._position_should_backoff(e):
                self._mark_position_backoff(e)
            logger.error(f"Failed to fetch position book: {e}")
            return None
