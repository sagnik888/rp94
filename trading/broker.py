"""
Broker Interface — Abstracting order execution
═══════════════════════════════════════════════════════════════

This module holds the broker CONTRACT and the simulation broker. Live execution is
Zerodha Kite, in trading/kite_broker.py.

The AngelOne implementation that used to live here was removed on 2026-08-01, after the
live-money invariants it carried were re-proved against KiteBroker: SL-M ambiguity on a
lost acknowledgement, the entry-order rate kill-switch, the position-book REST backoff,
the tri-state order-book scans, and the cross-process execution lease. Three of those
existed ONLY here and had to be built into KiteBroker before this could go — which is
the reason the deletion waited for the port rather than leading it.

The tri-state defaults below are the contract's most important part: a broker that
cannot inspect its order book must answer UNKNOWN, never NONE or ABSENT. Concluding
"no order is resting" from a scan that failed is what re-places a live order.
"""

import time
from typing import Any, Dict, Optional
from loguru import logger

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

    def inspect_recent_entry_order(
        self,
        symbol: str,
        token: str = "",
        side: str = "",
        quantity: int = 0,
    ) -> Dict[str, Any]:
        """Did an ENTRY order for this contract actually reach the exchange?

        Tri-state, and the distinction is the whole point:
          FOUND   - an order exists; adopt its id, never place another
          ABSENT  - the order book was read successfully and contains nothing, so the
                    placement definitively failed and the trade can be closed out cleanly
          UNKNOWN - we could not read the order book; the caller MUST stay ambiguous

        Only a successful scan may return ABSENT. A failed scan says UNKNOWN so a lost
        acknowledgement can never be mistaken for "not placed".
        """
        return {
            "state": "UNKNOWN",
            "order_id": "",
            "reason": "broker does not support entry-order inspection",
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
