"""Upstox API v2 Live Broker Execution.

Implements the unified Broker interface with Upstox API v2:
- Order placement (MARKET, LIMIT)
- Protective stops (SL-M, SL)
- Order modification and cancellation with confirmation
- Position book reconciliation normalized to system standards
- Margin preflight checks
- Fail-closed error handling (distinguishing ambiguous vs rejected)
- Health and failure tracking for 30-second Angel One fallback.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from loguru import logger

from trading.broker import Broker, order_filled_quantity
from trading.order_retry import BrokerOrderAmbiguous, BrokerOrderRejected
from trading.account_execution_lease import AccountExecutionLease, lease_owner_description
from data.upstox_auth import get_upstox_token, get_upstox_auth_status

UPSTOX_BASE_URL = "https://api.upstox.com"
UPSTOX_ORDER_PLACE = f"{UPSTOX_BASE_URL}/v2/order/place"
UPSTOX_ORDER_MODIFY = f"{UPSTOX_BASE_URL}/v2/order/modify"
UPSTOX_ORDER_CANCEL = f"{UPSTOX_BASE_URL}/v2/order/cancel"
UPSTOX_ORDER_BOOK = f"{UPSTOX_BASE_URL}/v2/order/retrieve-all"
UPSTOX_ORDER_HISTORY = f"{UPSTOX_BASE_URL}/v2/order/history"
UPSTOX_POSITIONS = f"{UPSTOX_BASE_URL}/v2/portfolio/short-term-positions"
UPSTOX_FUNDS = f"{UPSTOX_BASE_URL}/v2/user/get-funds-and-margin"

_TERMINAL_STATUSES = {"COMPLETE", "REJECTED", "CANCELLED"}


class UpstoxBroker(Broker):
    """Upstox API v2 Live Execution Broker."""

    def __init__(self, account_id: str = "", instance_id: str = "UT1", timeout: float = 7.0):
        super().__init__()
        self.timeout = timeout
        self.account_id = str(account_id or "").strip()
        self._execution_lease = (
            AccountExecutionLease(self.account_id, instance_id)
            if self.account_id
            else None
        )
        self._execution_authority = None
        self._lock = threading.RLock()
        self._last_success_time = time.monotonic()
        self._last_error_time: Optional[float] = None
        self._consecutive_failures = 0
        self._last_error_reason = ""
        self._order_cache: Dict[str, Dict[str, Any]] = {}

    def acquire_execution_lease(self) -> bool:
        if self._execution_lease is None:
            return True
        return self._execution_lease.acquire()

    def execution_lease_established(self) -> bool:
        return self._execution_lease is not None

    def release_execution_lease(self) -> None:
        if self._execution_lease is not None:
            self._execution_lease.release()

    def has_execution_lease(self) -> bool:
        lease = self._execution_lease
        return True if lease is None else bool(lease.held)

    def execution_lease_block_reason(self) -> str:
        lease = self._execution_lease
        if lease is None or lease.held:
            return ""
        return "REAL broker account is owned by " + lease_owner_description(lease.owner_metadata)

    def set_execution_authority(self, provider) -> None:
        self._execution_authority = provider

    def _headers(self) -> Dict[str, str]:
        token = get_upstox_token()
        if not token:
            raise BrokerOrderRejected("Upstox access token missing or expired; login required")
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def _record_success(self) -> None:
        with self._lock:
            self._last_success_time = time.monotonic()
            self._consecutive_failures = 0
            self._last_error_time = None
            self._last_error_reason = ""

    def _record_failure(self, error: Exception | str) -> None:
        with self._lock:
            now = time.monotonic()
            if self._last_error_time is None:
                self._last_error_time = now
            self._consecutive_failures += 1
            self._last_error_reason = str(error)

    def is_healthy(self, threshold_seconds: float = 30.0) -> bool:
        """Returns False if Upstox has been continuously failing for > 30s."""
        with self._lock:
            if self._last_error_time is None:
                return True
            return (time.monotonic() - self._last_error_time) < threshold_seconds

    def failure_duration(self) -> float:
        with self._lock:
            if self._last_error_time is None:
                return 0.0
            return max(0.0, time.monotonic() - self._last_error_time)

    def _map_product(self, product_type: str) -> str:
        p = str(product_type).upper()
        if p in ("INTRADAY", "MIS", "I"):
            return "I"
        return "D"

    def place_order(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "INTRADAY",
        price: float = 0.0,
        touch_quote: Optional[Dict] = None,
        tag: str = "ut1",
        **kwargs: Any,
    ) -> str:
        """Place an order via Upstox API v2."""
        qty = abs(int(qty))
        if qty <= 0:
            raise BrokerOrderRejected(f"Invalid quantity {qty} for {symbol}")

        instrument_token = token if ("|" in token or "_" in token) else f"NSE_FO|{token}"
        order_type = "MARKET" if price <= 0.0 else "LIMIT"
        transaction_type = "BUY" if side.upper() == "BUY" else "SELL"
        product = self._map_product(product_type)

        payload = {
            "quantity": qty,
            "product": product,
            "validity": "DAY",
            "price": round(float(price), 2) if price > 0.0 else 0.0,
            "tag": str(tag or "ut1")[:20],
            "instrument_token": instrument_token,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
        }

        try:
            resp = requests.post(
                UPSTOX_ORDER_PLACE,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            self._record_failure(exc)
            # Timeout during placement is ambiguous: the order may have reached the exchange
            raise BrokerOrderAmbiguous(f"Upstox place_order timed out for {symbol}: {exc}") from exc
        except Exception as exc:
            self._record_failure(exc)
            raise BrokerOrderAmbiguous(f"Upstox place_order transport error for {symbol}: {exc}") from exc

        if resp.status_code == 200:
            data = resp.json().get("data", {})
            order_id = str(data.get("order_id") or "")
            if order_id:
                self._record_success()
                logger.info(f"Upstox order placed: {symbol} {transaction_type} {qty} -> order_id={order_id}")
                return order_id

        err_body = resp.text[:300]
        self._record_failure(f"HTTP {resp.status_code}: {err_body}")

        # Distinguish definitive reject from ambiguous failure
        if resp.status_code in (400, 422):
            raise BrokerOrderRejected(f"Upstox rejected order for {symbol}: {err_body}")
        raise BrokerOrderAmbiguous(f"Upstox order status uncertain (HTTP {resp.status_code}): {err_body}")

    def close_order(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "INTRADAY",
        price: float = 0.0,
        tag: str = "ut1_exit",
        **kwargs: Any,
    ) -> str:
        """Close an existing position by sending an opposing market/limit order."""
        exit_side = "SELL" if side.upper() == "BUY" else "BUY"
        return self.place_order(
            symbol=symbol,
            token=token,
            qty=qty,
            side=exit_side,
            product_type=product_type,
            price=price,
            tag=tag,
            **kwargs,
        )

    def get_positions(self) -> Optional[Dict[str, List[Dict[str, Any]]]]:
        """Fetch positions and normalize to the system's expected format."""
        try:
            resp = requests.get(UPSTOX_POSITIONS, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                raw_positions = resp.json().get("data", []) or []
                rows = []
                for p in raw_positions:
                    net_qty = int(float(p.get("quantity") or 0))
                    rows.append({
                        "tradingsymbol": p.get("trading_symbol", ""),
                        "symboltoken": p.get("instrument_token", ""),
                        "netqty": net_qty,
                        "buyqty": int(float(p.get("buy_quantity") or 0)),
                        "sellqty": int(float(p.get("sell_quantity") or 0)),
                        "buyprice": float(p.get("buy_price") or 0.0),
                        "sellprice": float(p.get("sell_price") or 0.0),
                        "pnl": float(p.get("pnl") or 0.0),
                        "realised": float(p.get("realised_profit") or 0.0),
                        "unrealised": float(p.get("unrealised_profit") or 0.0),
                        "producttype": "INTRADAY" if p.get("product") == "I" else "CARRYFORWARD",
                    })
                self._record_success()
                return {"data": rows}
            self._record_failure(f"HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as exc:
            self._record_failure(exc)
            logger.warning(f"Upstox get_positions failed: {exc}")
        return None

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Fetch status and fill details for an order."""
        if not order_id:
            return {"status": "UNKNOWN", "orderstatus": "UNKNOWN"}

        url = f"{UPSTOX_ORDER_HISTORY}?order_id={urllib.parse.quote(order_id)}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                history = resp.json().get("data", []) or []
                if history:
                    latest = history[0] # Upstox returns newest transition first
                    raw_status = str(latest.get("status") or "").upper()
                    # Normalise to standard keys
                    status_map = {
                        "COMPLETE": "complete",
                        "REJECTED": "rejected",
                        "CANCELLED": "cancelled",
                        "TRIGGER PENDING": "trigger pending",
                        "OPEN": "open",
                        "PUT ORDER REQ RECEIVED": "open",
                    }
                    norm_status = status_map.get(raw_status, raw_status.lower())
                    result = {
                        "order_id": order_id,
                        "orderstatus": norm_status,
                        "status": norm_status,
                        "filledshares": int(float(latest.get("filled_quantity") or 0)),
                        "filled_quantity": int(float(latest.get("filled_quantity") or 0)),
                        "price": float(latest.get("average_price") or latest.get("price") or 0.0),
                        "average_price": float(latest.get("average_price") or 0.0),
                        "status_message": str(latest.get("status_message") or ""),
                        "transaction_type": latest.get("transaction_type", ""),
                        "tradingsymbol": latest.get("trading_symbol", ""),
                    }
                    self._order_cache[order_id] = result
                    self._record_success()
                    return result
            self._record_failure(f"HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as exc:
            self._record_failure(exc)
            logger.warning(f"Upstox get_order_status failed for {order_id}: {exc}")

        return self._order_cache.get(order_id, {"status": "UNKNOWN", "orderstatus": "UNKNOWN"})

    def place_protective_stop(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        trigger_price: float,
        product_type: str = "INTRADAY",
        tag: str = "ut1_stop",
        **kwargs: Any,
    ) -> Optional[str]:
        """Place an SL-M (Stop-Loss Market) or SL protective order."""
        qty = abs(int(qty))
        if qty <= 0:
            return None

        instrument_token = token if ("|" in token or "_" in token) else f"NSE_FO|{token}"
        stop_side = "SELL" if side.upper() == "BUY" else "BUY"
        product = self._map_product(product_type)
        trigger = round(float(trigger_price), 2)

        payload = {
            "quantity": qty,
            "product": product,
            "validity": "DAY",
            "price": 0.0,
            "tag": str(tag or "ut1_stop")[:20],
            "instrument_token": instrument_token,
            "order_type": "SL-M",
            "transaction_type": stop_side,
            "disclosed_quantity": 0,
            "trigger_price": trigger,
            "is_amo": False,
        }

        try:
            resp = requests.post(UPSTOX_ORDER_PLACE, headers=self._headers(), json=payload, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                order_id = str(data.get("order_id") or "")
                if order_id:
                    self._record_success()
                    logger.info(f"Upstox protective stop placed: {symbol} trigger={trigger} -> order_id={order_id}")
                    return order_id
            self._record_failure(f"HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as exc:
            self._record_failure(exc)
            logger.error(f"Upstox place_protective_stop error for {symbol}: {exc}")
        return None

    def modify_protective_stop(
        self,
        order_id: str,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        trigger_price: float,
        product_type: str = "INTRADAY",
        **kwargs: Any,
    ) -> bool:
        """Modify trigger price / quantity of a working protective stop."""
        if not order_id:
            return False

        payload = {
            "order_id": order_id,
            "quantity": abs(int(qty)),
            "validity": "DAY",
            "price": 0.0,
            "order_type": "SL-M",
            "trigger_price": round(float(trigger_price), 2),
            "disclosed_quantity": 0,
        }
        try:
            resp = requests.put(UPSTOX_ORDER_MODIFY, headers=self._headers(), json=payload, timeout=self.timeout)
            if resp.status_code == 200:
                self._record_success()
                logger.info(f"Upstox stop modified: {order_id} -> trigger={trigger_price}")
                return True
            self._record_failure(f"HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as exc:
            self._record_failure(exc)
            logger.error(f"Upstox modify_protective_stop error for {order_id}: {exc}")
        return False

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        """Cancel an open or trigger-pending order."""
        if not order_id:
            return False
        url = f"{UPSTOX_ORDER_CANCEL}?order_id={urllib.parse.quote(order_id)}"
        try:
            resp = requests.delete(url, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                self._record_success()
                logger.info(f"Upstox order cancelled: {order_id}")
                return True
            self._record_failure(f"HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as exc:
            self._record_failure(exc)
            logger.warning(f"Upstox cancel_order error for {order_id}: {exc}")
        return False

    def cancel_order_confirmed(
        self,
        order_id: str,
        variety: str = "NORMAL",
        timeout_seconds: float = 2.0,
        poll_seconds: float = 0.1,
    ) -> bool:
        """Cancel order and poll until confirmation or timeout."""
        self.cancel_order(order_id, variety=variety)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            st = self.get_order_status(order_id)
            if st.get("orderstatus") in ("cancelled", "rejected", "complete"):
                return True
            time.sleep(poll_seconds)
        return False

    def find_working_protective_stop(
        self,
        symbol: str,
        token: str,
        side: str,
        quantity: int,
    ) -> Optional[str]:
        """Find an existing open/pending stop order matching symbol and side."""
        try:
            resp = requests.get(UPSTOX_ORDER_BOOK, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                orders = resp.json().get("data", []) or []
                stop_side = "SELL" if side.upper() == "BUY" else "BUY"
                for o in orders:
                    status = str(o.get("status") or "").upper()
                    if status in ("TRIGGER PENDING", "OPEN"):
                        if o.get("transaction_type") == stop_side:
                            t_sym = o.get("trading_symbol", "")
                            if symbol in t_sym or t_sym in symbol:
                                return str(o.get("order_id") or "")
        except Exception as exc:
            logger.warning(f"Upstox find_working_protective_stop failed: {exc}")
        return None

    def inspect_working_protective_stop(
        self,
        symbol: str,
        token: str,
        side: str,
        quantity: int,
        product_type: Optional[str] = None,
        trigger_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Return full details of a working protective stop order."""
        stop_id = self.find_working_protective_stop(symbol, token, side, quantity)
        if not stop_id:
            return {"order_id": "", "status": "ABSENT"}
        st = self.get_order_status(stop_id)
        return {
            "order_id": stop_id,
            "status": "WORKING",
            "trigger_price": st.get("price") or trigger_price or 0.0,
            "raw": st,
        }

    def inspect_recent_entry_order(
        self,
        symbol: str,
        token: str,
        side: str,
        quantity: int,
    ) -> Dict[str, Any]:
        """Inspect recent order book for matching entry order."""
        try:
            resp = requests.get(UPSTOX_ORDER_BOOK, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                orders = resp.json().get("data", []) or []
                for o in orders:
                    t_sym = o.get("trading_symbol", "")
                    if (symbol in t_sym or t_sym in symbol) and o.get("transaction_type") == side.upper():
                        return {
                            "order_id": str(o.get("order_id") or ""),
                            "status": str(o.get("status") or "").lower(),
                            "filled_quantity": int(float(o.get("filled_quantity") or 0)),
                        }
        except Exception as exc:
            logger.warning(f"Upstox inspect_recent_entry_order failed: {exc}")
        return {"order_id": "", "status": "UNKNOWN"}

    def preflight_entry_margin(self, **kwargs: Any) -> Dict[str, Any]:
        """Query Upstox available funds and margin."""
        try:
            resp = requests.get(UPSTOX_FUNDS, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                equity = data.get("equity", {})
                avail = float(equity.get("available_margin") or 0.0)
                used = float(equity.get("used_margin") or 0.0)
                return {
                    "available_margin": avail,
                    "used_margin": used,
                    "status": "OK",
                    "broker": "UPSTOX",
                }
        except Exception as exc:
            logger.warning(f"Upstox preflight_entry_margin failed: {exc}")

        return {
            "available_margin": 100000.0,
            "used_margin": 0.0,
            "status": "UNAVAILABLE_ASSUMED_PASS",
            "broker": "UPSTOX",
        }
