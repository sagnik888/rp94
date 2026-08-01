"""Zerodha Kite Connect execution, behind the shared Broker interface.

Kite reports failures as typed exceptions rather than the error dicts AngelOne
returned, so the whole "which envelope is this?" problem disappears. What replaces it
is a classification that matters more, and gets its own table below: some Kite
exceptions prove the order never reached the exchange, and some prove nothing at all.

The invariant carried over from the AngelOne incident (2026-07-31): we may never
conclude "not placed" from a call we could not complete. A lost acknowledgement that we
treat as a clean failure becomes a duplicate live order on the retry.

Rows returned to the rest of the system are normalised to the AngelOne-ish field names
(`orderstatus`, `orderid`, `netqty`, `tradingsymbol`, `transactiontype`) that
trade_manager and the Broker base class already read, so this is a drop-in.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from loguru import logger

from trading.account_execution_lease import AccountExecutionLease, lease_owner_description
from trading.broker import Broker
from trading.order_retry import BrokerOrderAmbiguous, BrokerOrderRejected

try:
    from kiteconnect import KiteConnect
    from kiteconnect.exceptions import (
        DataException,
        GeneralException,
        InputException,
        KiteException,
        NetworkException,
        OrderException,
        PermissionException,
        TokenException,
    )
    HAS_KITE = True
except ImportError:  # pragma: no cover - exercised only on a machine without the SDK
    KiteConnect = None
    HAS_KITE = False

    class KiteException(Exception): pass
    class DataException(KiteException): pass
    class GeneralException(KiteException): pass
    class InputException(KiteException): pass
    class NetworkException(KiteException): pass
    class OrderException(KiteException): pass
    class PermissionException(KiteException): pass
    class TokenException(KiteException): pass


# ── exception classification ─────────────────────────────────────────────────
# The distinction that protects live money:
#
#   AMBIGUOUS  the call did not complete, so the order MAY be resting at the exchange.
#              Never retried — a duplicate entry is worse than a missed one.
#   TERMINAL   Kite refused the request outright; retrying cannot change the outcome
#              (dead session, malformed order, permission denied).
#   RETRYABLE  the exchange/RMS rejected it for a condition that can clear on its own,
#              e.g. margin. The order is definitively not live, so a retry is safe.
_AMBIGUOUS_EXCEPTIONS = (NetworkException, DataException, GeneralException)
_TERMINAL_EXCEPTIONS = (TokenException, PermissionException, InputException)

# Broker-neutral product names used across this system -> Kite products.
_PRODUCT_MAP = {
    "CARRYFORWARD": "NRML",
    "NRML": "NRML",
    "INTRADAY": "MIS",
    "MIS": "MIS",
    "DELIVERY": "CNC",
    "CNC": "CNC",
}

# Kite order states that mean the order is still working at the exchange.
_WORKING_STATUSES = {"open", "trigger pending", "open pending", "validation pending",
                     "put order req received", "modify pending"}


def classify_kite_exception(exc: Exception) -> str:
    """AMBIGUOUS / TERMINAL / RETRYABLE for one Kite SDK exception."""
    if isinstance(exc, _AMBIGUOUS_EXCEPTIONS):
        return "AMBIGUOUS"
    if isinstance(exc, _TERMINAL_EXCEPTIONS):
        return "TERMINAL"
    if isinstance(exc, OrderException):
        return "RETRYABLE"
    # An unrecognised failure is treated as ambiguous on purpose: assuming the order
    # did not reach the market is the assumption that can duplicate a live position.
    return "AMBIGUOUS"


class KiteBroker(Broker):
    """Live execution through Zerodha Kite Connect."""

    requires_contract_master_validation = True
    requires_protective_stop_metadata_validation = True
    requires_entry_margin_preflight = True

    def __init__(self, kite=None, account_id: str = "", instance_id: str = "", auth=None):
        super().__init__()
        self.api = kite
        self._auth = auth           # KiteAuth, so a dead token can be invalidated
        self._order_lock = threading.Lock()
        self._last_call_at = 0.0
        self._min_call_interval = 0.34      # Kite allows ~3 order req/s; stay under it
        self._execution_lease = (
            AccountExecutionLease(account_id, instance_id)
            if str(account_id or "").strip()
            else None
        )

    # ── session ──────────────────────────────────────────────────────────────
    def replace_api(self, kite) -> None:
        """Rebind to a freshly authenticated client after a daily re-login."""
        with self._order_lock:
            self.api = kite

    def _require_api(self, action: str):
        if self.api is None:
            raise BrokerOrderRejected(
                f"{action} blocked: no authenticated Kite session. "
                "Complete the daily login at /api/kite_login.",
                terminal=True,
            )
        return self.api

    def _api_call(self, context: str, fn, timeout: float = 5.0):
        with self._order_lock:
            gap = time.monotonic() - self._last_call_at
            if gap < self._min_call_interval:
                time.sleep(self._min_call_interval - gap)
            try:
                return fn()
            except TokenException as exc:
                # The daily token died mid-session. Tell the auth layer so the
                # watchdog surfaces a re-login instead of every call failing blindly.
                if self._auth is not None:
                    self._auth.invalidate(f"{context}: {exc}")
                raise
            finally:
                self._last_call_at = time.monotonic()

    # ── execution lease (unchanged semantics from the AngelOne broker) ────────
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
        return "REAL broker account is owned by " + lease_owner_description(lease.owner_metadata)

    def _require_account_execution_lease(self, action: str) -> None:
        if self.has_execution_lease():
            return
        if self.acquire_execution_lease():
            logger.warning(
                f"Acquired the REAL account execution lease on demand before {action}."
            )
            return
        raise PermissionError(f"{action} blocked: {self.execution_lease_block_reason()}")

    # ── mapping helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _exchange_for_symbol(symbol: str) -> str:
        sym = str(symbol or "").upper()
        return "BFO" if "SENSEX" in sym or "BANKEX" in sym else "NFO"

    @staticmethod
    def _product(product_type: str) -> str:
        return _PRODUCT_MAP.get(str(product_type or "").upper(), "NRML")

    @staticmethod
    def _tick_price(value: float) -> float:
        return round(max(0.05, round(float(value) / 0.05) * 0.05), 2)

    @staticmethod
    def _entry_limit_confidence_threshold() -> float:
        try:
            from config.settings import get_settings

            return float(getattr(get_settings(), "entry_limit_confidence_threshold", 95.0) or 95.0)
        except Exception:
            return 95.0

    @staticmethod
    def _entry_limit_ttl_minutes() -> int:
        try:
            from config.settings import get_settings

            return max(0, int(getattr(get_settings(), "kite_entry_limit_ttl_minutes", 0) or 0))
        except Exception:
            return 0

    @staticmethod
    def order_tag(trade_id: str) -> str:
        """A <=20 char Kite tag that maps an order back to its trade deterministically.

        Kite echoes the tag on the order book, on trades, and on every postback. That
        turns order->trade matching from a heuristic (symbol + side + quantity, which
        is ambiguous the moment two trades sit on one strike) into an exact lookup —
        the precise ambiguity that stranded two trades on 2026-07-31.

        Trade ids look like T0005_MIDCPNIFTY_07311451 (25 chars), so the instrument is
        dropped: the counter and timestamp are what make it unique.
        """
        raw = str(trade_id or "").strip()
        if not raw:
            return ""
        parts = raw.split("_")
        if len(parts) >= 3:
            candidate = f"{parts[0]}_{parts[-1]}"      # T0005_07311451
        else:
            candidate = raw
        return candidate[:20]

    @staticmethod
    def _normalise_order(row: Dict[str, Any]) -> Dict[str, Any]:
        """Kite order row -> the field names the rest of this system reads."""
        row = dict(row or {})
        row.setdefault("orderid", row.get("order_id", ""))
        row.setdefault("orderstatus", str(row.get("status", "")).lower())
        row.setdefault("transactiontype", row.get("transaction_type", ""))
        row.setdefault("ordertype", row.get("order_type", ""))
        row.setdefault("producttype", row.get("product", ""))
        row.setdefault("filledshares", row.get("filled_quantity", 0))
        row.setdefault("averageprice", row.get("average_price", 0.0))
        row.setdefault("triggerprice", row.get("trigger_price", 0.0))
        row.setdefault("text", row.get("status_message", "") or "")
        return row

    def _raise_for(self, action: str, symbol: str, exc: Exception):
        """Translate a Kite exception into this system's order-failure contract."""
        kind = classify_kite_exception(exc)
        detail = f"{type(exc).__name__}: {exc}"
        if kind == "AMBIGUOUS":
            raise BrokerOrderAmbiguous(
                f"{action} for {symbol} did not complete, so the order may be live at "
                f"the exchange ({detail}). Reconcile before any further action."
            ) from exc
        logger.error(f"Kite refused {action} for {symbol}: {detail}")
        raise BrokerOrderRejected(detail, terminal=(kind == "TERMINAL")) from exc

    # ── entry ────────────────────────────────────────────────────────────────
    def place_order(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "CARRYFORWARD",
        price: float = 0.0,
        touch_quote: Optional[Dict[str, Any]] = None,
        trade_id: str = "",
        confidence: float = 0.0,
    ) -> Optional[str]:
        try:
            self._require_account_execution_lease("entry order")
        except PermissionError as exc:
            raise BrokerOrderRejected(str(exc), terminal=True) from exc
        api = self._require_api("entry order")

        exchange = self._exchange_for_symbol(symbol)
        # Take the market unless the signal is strong enough to justify waiting for a
        # price. On 2026-07-31 every entry went out as a blind LTP mark-up limit
        # against an empty book — a limit is only meaningful when the conviction is
        # high enough that missing the fill beats paying the spread.
        threshold = self._entry_limit_confidence_threshold()
        use_limit = float(price or 0.0) > 0 and float(confidence or 0.0) >= threshold
        order_type = "LIMIT" if use_limit else "MARKET"
        params: Dict[str, Any] = {
            "variety": "regular",
            "exchange": exchange,
            "tradingsymbol": str(symbol),
            "transaction_type": str(side).upper(),
            "quantity": int(qty),
            "product": self._product(product_type),
            "order_type": order_type,
            "validity": "DAY",
        }
        if order_type == "MARKET":
            # Unbounded market fills in a thin option book are the cost of this policy;
            # Zerodha's automatic protection band is what makes it acceptable.
            params["market_protection"] = -1
        if order_type == "LIMIT":
            params["price"] = self._tick_price(price)
            # Backstop only — see kite_entry_limit_ttl_minutes. The cancel/replace
            # path handles staleness in ~12s; this exists so a limit left resting by a
            # dead process expires server-side instead of filling hours later.
            ttl = self._entry_limit_ttl_minutes()
            if ttl > 0:
                params["validity"] = "TTL"
                params["validity_ttl"] = ttl
        tag = self.order_tag(trade_id)
        if tag:
            params["tag"] = tag

        logger.info(
            f"PLACING REAL ORDER: {side} {qty} {symbol} on {exchange} | "
            f"Type: {order_type} | Price: {params.get('price', 0.0):.2f}"
        )
        try:
            order_id = self._api_call("place entry order", lambda: api.place_order(**params))
        except Exception as exc:
            self._raise_for("entry order", symbol, exc)

        order_id = str(order_id or "")
        if not order_id or order_id == "None":
            # Kite returns an id on success, so an empty one is unexplained. Ask the
            # order book whether anything actually reached the exchange; only a
            # completed scan that finds nothing may be called a clean failure.
            resting = self.inspect_recent_entry_order(symbol, token, side, qty)
            state = str(resting.get("state") or "UNKNOWN").upper()
            if state == "ABSENT":
                raise BrokerOrderRejected(
                    "Kite returned no order id and the order book confirms nothing was placed",
                    terminal=False,
                )
            if state == "FOUND":
                found = str(resting.get("order_id") or "")
                logger.warning(f"Recovered entry order id {found} for {symbol} from the order book.")
                return found or None
            raise BrokerOrderAmbiguous(
                f"Kite returned no order id for {symbol} and the order-book scan was "
                f"inconclusive ({resting.get('reason') or 'unknown'})"
            )

        logger.success(f"REAL ORDER PLACED: {order_id} ({exchange})")
        return order_id

    # ── exit ─────────────────────────────────────────────────────────────────
    def close_order(
        self, symbol: str, token: str, qty: int, side: str,
        product_type: str = "CARRYFORWARD", price: float = 0.0,
    ) -> Optional[str]:
        self._require_account_execution_lease("exit order")
        api = self._require_api("exit order")
        try:
            order_id = self._api_call(
                "place exit order",
                lambda: api.place_order(
                    variety="regular",
                    exchange=self._exchange_for_symbol(symbol),
                    tradingsymbol=str(symbol),
                    transaction_type=str(side).upper(),
                    quantity=int(qty),
                    product=self._product(product_type),
                    order_type="MARKET",
                    validity="DAY",
                    # Exits are MARKET orders, so in a thin option book they can fill
                    # arbitrarily far from the touch. -1 asks Zerodha to apply its
                    # automatic protection band, capping that slippage. AngelOne had
                    # no equivalent; every exit was unbounded.
                    market_protection=-1,
                ),
            )
        except Exception as exc:
            # An exit that failed is live risk. Say so loudly, and keep the ambiguous
            # case ambiguous so nothing double-exits the position.
            kind = classify_kite_exception(exc)
            logger.critical(f"Kite refused EXIT for {symbol} [{kind}]: {type(exc).__name__}: {exc}")
            if kind == "AMBIGUOUS":
                raise BrokerOrderAmbiguous(
                    f"Exit for {symbol} did not complete; it may be live ({exc})"
                ) from exc
            return None
        order_id = str(order_id or "")
        if not order_id:
            raise BrokerOrderAmbiguous(f"Kite exit for {symbol} returned no order id")
        return order_id

    # ── protective stop ──────────────────────────────────────────────────────
    def place_protective_stop(
        self, symbol: str, token: str, qty: int, side: str, trigger_price: float,
        product_type: str = "CARRYFORWARD",
    ) -> Optional[str]:
        self._require_account_execution_lease("protective stop")
        api = self._require_api("protective stop")
        trigger = self._tick_price(trigger_price)
        try:
            order_id = self._api_call(
                "place protective stop",
                lambda: api.place_order(
                    variety="regular",
                    exchange=self._exchange_for_symbol(symbol),
                    tradingsymbol=str(symbol),
                    transaction_type=str(side).upper(),
                    quantity=int(qty),
                    product=self._product(product_type),
                    order_type="SL-M",
                    trigger_price=trigger,
                    validity="DAY",
                ),
            )
        except Exception as exc:
            if classify_kite_exception(exc) == "AMBIGUOUS":
                # Same rule as the entry: a second SL-M on the same position means one
                # of them fires against a flat book and opens a naked reversed leg.
                raise BrokerOrderAmbiguous(
                    f"Protective stop for {symbol} did not complete; it may be resting ({exc})"
                ) from exc
            logger.error(f"Kite rejected protective stop for {symbol}: {exc}")
            return None
        return str(order_id or "") or None

    # ── GTT: protection that outlives this process ───────────────────────────
    # A regular SL-M lives in the order book, so it dies with the session and cannot be
    # placed at all while the app is down. A GTT trigger sits on Zerodha's servers and
    # fires whether or not this machine is running. That is the direct answer to the
    # 2026-07-31 failure mode: positions left unprotected because the process could not
    # act — "emergency flatten unavailable" looped while real risk sat open.
    def place_gtt_stop(
        self, symbol: str, qty: int, side: str, trigger_price: float,
        last_price: float, product_type: str = "CARRYFORWARD",
    ) -> Optional[str]:
        """Create a single-leg GTT stop. Returns the trigger id, or None."""
        api = self._require_api("GTT stop")
        trigger = self._tick_price(trigger_price)
        exchange = self._exchange_for_symbol(symbol)
        # The GTT leg is a LIMIT order at the trigger. Zerodha rejects MARKET legs, and
        # a limit AT the trigger can miss in a gap — so this complements, and does not
        # replace, in-session stop management.
        orders = [{
            "exchange": exchange,
            "tradingsymbol": str(symbol),
            "transaction_type": str(side).upper(),
            "quantity": int(qty),
            "order_type": "LIMIT",
            "product": self._product(product_type),
            "price": trigger,
        }]
        try:
            result = self._api_call(
                "place GTT stop",
                lambda: api.place_gtt(
                    trigger_type=api.GTT_TYPE_SINGLE,
                    tradingsymbol=str(symbol),
                    exchange=exchange,
                    trigger_values=[trigger],
                    last_price=float(last_price or trigger),
                    orders=orders,
                ),
            )
        except Exception as exc:
            logger.error(f"Kite refused GTT stop for {symbol}: {type(exc).__name__}: {exc}")
            return None
        trigger_id = str((result or {}).get("trigger_id") or "")
        if trigger_id:
            logger.success(
                f"GTT stop armed for {symbol} at {trigger:.2f} (id {trigger_id}); "
                "it survives a restart or an outage of this process."
            )
        return trigger_id or None

    def cancel_gtt_stop(self, trigger_id: str) -> bool:
        if not trigger_id:
            return True
        api = self.api
        if api is None:
            return False
        try:
            self._api_call("delete GTT", lambda: api.delete_gtt(str(trigger_id)))
            return True
        except Exception as exc:
            # False on refusal, same polarity rule as cancel_order: a caller that reads
            # True will flatten on top of a still-armed trigger.
            logger.error(f"Kite refused GTT deletion for {trigger_id}: {exc}")
            return False

    def find_gtt_stop(self, symbol: str) -> Dict[str, Any]:
        """Tri-state lookup of an existing GTT for this contract."""
        api = self.api
        if api is None:
            return {"state": "UNKNOWN", "trigger_id": "", "reason": "no session"}
        try:
            triggers = self._api_call("list GTTs", lambda: api.get_gtts(), timeout=3.0)
        except Exception as exc:
            # Never NONE from a failed scan — that would authorise a duplicate trigger.
            return {"state": "UNKNOWN", "trigger_id": "", "reason": f"GTT scan failed: {exc}"}
        wanted = str(symbol or "").upper()
        for row in triggers or []:
            condition = (row or {}).get("condition") or {}
            if str(condition.get("tradingsymbol") or "").upper() != wanted:
                continue
            if str((row or {}).get("status") or "").lower() != "active":
                continue
            return {
                "state": "FOUND",
                "trigger_id": str(row.get("id") or ""),
                "reason": "active GTT found",
                "trigger_values": condition.get("trigger_values") or [],
            }
        return {"state": "NONE", "trigger_id": "", "reason": "no active GTT"}

    def modify_protective_stop(
        self, order_id: str, symbol: str, token: str, qty: int, side: str,
        trigger_price: float, product_type: str = "CARRYFORWARD",
    ) -> bool:
        api = self._require_api("protective stop modify")
        try:
            self._api_call(
                "modify protective stop",
                lambda: api.modify_order(
                    variety="regular",
                    order_id=str(order_id),
                    quantity=int(qty),
                    order_type="SL-M",
                    trigger_price=self._tick_price(trigger_price),
                ),
            )
            return True
        except Exception as exc:
            # Must be False on refusal: a True here records a trigger the exchange
            # never accepted, and every later tightening check trusts that value.
            logger.error(f"Kite refused protective-stop modify for {symbol}: {exc}")
            return False

    # ── cancellation ─────────────────────────────────────────────────────────
    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        if not order_id:
            return True
        self._require_account_execution_lease("order cancellation")
        api = self._require_api("order cancellation")
        try:
            self._api_call(
                "cancel order",
                lambda: api.cancel_order(variety="regular", order_id=str(order_id)),
            )
            return True
        except Exception as exc:
            # False on refusal. emergency_flatten_trade() reads a True as proof the
            # SL-M is gone and market-exits on top of it.
            logger.error(f"Kite refused cancellation of {order_id}: {exc}")
            return False

    # ── reads ────────────────────────────────────────────────────────────────
    def _order_book(self) -> Optional[List[Dict[str, Any]]]:
        api = self.api
        if api is None:
            return None
        try:
            rows = self._api_call("fetch order book", lambda: api.orders(), timeout=2.0)
        except Exception as exc:
            logger.warning(f"Kite order-book read failed: {exc}")
            return None
        if not isinstance(rows, list):
            return None
        return [self._normalise_order(row) for row in rows]

    def get_order_status(self, order_id: str) -> Optional[Dict]:
        if not order_id:
            return None
        for row in self._order_book() or []:
            if str(row.get("orderid") or "") == str(order_id):
                return row
        return None

    def get_positions(self) -> Optional[Dict]:
        """Normalised to {"data": [...]} with netqty, as the reconciler expects."""
        api = self.api
        if api is None:
            return None
        try:
            book = self._api_call("fetch positions", lambda: api.positions(), timeout=2.0)
        except Exception as exc:
            logger.error(f"Kite positions read failed: {exc}")
            return None
        net = (book or {}).get("net", []) if isinstance(book, dict) else []
        rows = []
        for row in net or []:
            row = dict(row)
            row.setdefault("netqty", row.get("quantity", 0))
            # The fill-price sync in trade_manager reads the AngelOne field names.
            # Kite calls these buy_price/sell_price, so without this mapping the
            # lookup silently returns nothing and the local entry price is never
            # reconciled against the broker's actual average.
            row.setdefault("buyavgprice", row.get("buy_price", 0.0))
            row.setdefault("sellavgprice", row.get("sell_price", 0.0))
            row.setdefault("producttype", row.get("product", ""))
            rows.append(row)
        return {"data": rows}

    def inspect_recent_entry_order(
        self, symbol: str, token: str = "", side: str = "", quantity: int = 0,
    ) -> Dict[str, Any]:
        """Tri-state. ABSENT only ever comes from a scan that actually completed."""
        rows = self._order_book()
        if rows is None:
            return {
                "state": "UNKNOWN", "order_id": "", "matches": [],
                "reason": "order book could not be read",
            }
        wanted_symbol = str(symbol or "").upper()
        wanted_side = str(side or "").upper()
        matches = [
            row for row in rows
            if str(row.get("tradingsymbol") or "").upper() == wanted_symbol
            and (not wanted_side or str(row.get("transactiontype") or "").upper() == wanted_side)
            and str(row.get("ordertype") or "").upper() not in {"SL", "SL-M"}
        ]
        if not matches:
            return {"state": "ABSENT", "order_id": "", "reason": "no such order", "matches": []}
        # Prefer a live/filled order over a rejected one when several exist.
        for row in matches:
            status = str(row.get("orderstatus") or "").lower()
            if status in _WORKING_STATUSES or status == "complete":
                return {
                    "state": "FOUND", "order_id": str(row.get("orderid") or ""),
                    "reason": f"order is {status}", "matches": matches,
                }
        return {
            "state": "ABSENT", "order_id": "",
            "reason": "only terminal non-fill orders found", "matches": matches,
        }

    def inspect_working_protective_stop(
        self, symbol: str, token: str, side: str, quantity: int = 0,
        product_type: str = "CARRYFORWARD", trigger_price: float = 0.0,
    ) -> Dict[str, Any]:
        rows = self._order_book()
        if rows is None:
            return {
                "state": "UNKNOWN", "order_id": "", "matches": [],
                "reason": "order book could not be read",
            }
        wanted_symbol = str(symbol or "").upper()
        wanted_side = str(side or "").upper()
        matches = [
            row for row in rows
            if str(row.get("tradingsymbol") or "").upper() == wanted_symbol
            and str(row.get("ordertype") or "").upper() in {"SL", "SL-M"}
            and str(row.get("orderstatus") or "").lower() in _WORKING_STATUSES
            and (not wanted_side or str(row.get("transactiontype") or "").upper() == wanted_side)
        ]
        if not matches:
            return {"state": "NONE", "order_id": "", "reason": "no working stop", "matches": []}
        return {
            "state": "FOUND", "order_id": str(matches[0].get("orderid") or ""),
            "reason": "working protective stop found", "matches": matches,
        }

    def find_working_protective_stop(
        self, symbol: str, token: str, side: str, quantity: int = 0
    ) -> str:
        result = self.inspect_working_protective_stop(symbol, token, side, quantity=quantity)
        return str(result.get("order_id") or "") if result.get("state") == "FOUND" else ""

    # ── fills: fact instead of inference ─────────────────────────────────────
    def get_fill(self, order_id: str) -> Dict[str, Any]:
        """Actual fill for an order: quantity, average price, exchange timestamp.

        Replaces inferring a fill from the order book. `order_trades` returns the real
        executions, so entry price and slippage stop being estimates — which is what
        made the 2026-07-31 rows carry a phantom -80 P&L on trades that never filled.
        """
        if not order_id:
            return {"state": "UNKNOWN", "reason": "no order id"}
        api = self.api
        if api is None:
            return {"state": "UNKNOWN", "reason": "no session"}
        try:
            fills = self._api_call(
                "order trades", lambda: api.order_trades(str(order_id)), timeout=3.0
            )
        except Exception as exc:
            # A failed lookup is UNKNOWN, never "not filled" — concluding no-fill from a
            # scan we could not complete is what strands or duplicates a live position.
            return {"state": "UNKNOWN", "reason": f"fill lookup failed: {exc}"}
        rows = [row for row in (fills or []) if row]
        if not rows:
            return {"state": "NO_FILL", "quantity": 0, "average_price": 0.0,
                    "reason": "no executions recorded"}
        total_qty, notional = 0, 0.0
        for row in rows:
            try:
                qty = float(row.get("quantity") or 0.0)
                price = float(row.get("average_price") or row.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            total_qty += int(qty)
            notional += qty * price
        return {
            "state": "FILLED",
            "quantity": total_qty,
            "average_price": round(notional / total_qty, 2) if total_qty else 0.0,
            "fills": len(rows),
            "last_fill_at": str(rows[-1].get("fill_timestamp") or rows[-1].get("exchange_timestamp") or ""),
            "reason": "",
        }

    def get_order_lifecycle(self, order_id: str) -> Dict[str, Any]:
        """Every state an order passed through, newest last.

        This is what settles an ambiguous acknowledgement definitively. The order-book
        scan can only answer "is it there now"; the history says whether it was ever
        accepted, and why it was rejected when it was.
        """
        if not order_id:
            return {"state": "UNKNOWN", "reason": "no order id", "transitions": []}
        api = self.api
        if api is None:
            return {"state": "UNKNOWN", "reason": "no session", "transitions": []}
        try:
            history = self._api_call(
                "order history", lambda: api.order_history(str(order_id)), timeout=3.0
            )
        except Exception as exc:
            return {"state": "UNKNOWN", "reason": f"history lookup failed: {exc}",
                    "transitions": []}
        rows = [row for row in (history or []) if row]
        if not rows:
            return {"state": "UNKNOWN", "reason": "no history returned", "transitions": []}
        transitions = [
            {
                "status": str(row.get("status") or "").upper(),
                "filled": row.get("filled_quantity"),
                "average_price": row.get("average_price"),
                "message": str(row.get("status_message") or ""),
                "at": str(row.get("order_timestamp") or ""),
            }
            for row in rows
        ]
        final = transitions[-1]["status"]
        rejection = next(
            (t["message"] for t in reversed(transitions) if t["status"] == "REJECTED" and t["message"]),
            "",
        )
        return {
            "state": final or "UNKNOWN",
            "reason": rejection,
            "transitions": transitions,
        }

    # ── margin ───────────────────────────────────────────────────────────────
    def available_funds(self) -> Optional[float]:
        """Free cash, or None when Kite will not answer.

        /user/margins answers "UNKNOWN_REQUEST Request not registered" outside market
        hours — Zerodha's RMS limits are not published until the session initialises.
        That is a normal condition, not a broken integration, so it returns None for
        the caller to interpret rather than raising.
        """
        api = self.api
        if api is None:
            return None
        try:
            funds = self._api_call("fetch margins", lambda: api.margins(), timeout=2.0)
        except Exception as exc:
            logger.debug(f"Kite margins unavailable: {exc}")
            return None
        equity = (funds or {}).get("equity") or {}
        if not equity and isinstance(funds, dict) and "available" in funds:
            equity = funds
        available = (equity.get("available") or {})
        for key in ("live_balance", "cash", "opening_balance"):
            try:
                value = float(available.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
            if value:
                return value
        try:
            return float(equity.get("net") or 0.0)
        except (TypeError, ValueError):
            return None

    def required_entry_margin(
        self, symbol: str, qty: int, side: str, product_type: str = "CARRYFORWARD",
    ) -> Optional[float]:
        """Exact margin Kite will charge for this order, via /margins/orders."""
        api = self.api
        if api is None:
            return None
        payload = [{
            "exchange": self._exchange_for_symbol(symbol),
            "tradingsymbol": str(symbol),
            "transaction_type": str(side).upper(),
            "variety": "regular",
            "product": self._product(product_type),
            "order_type": "MARKET",
            "quantity": int(qty),
        }]
        try:
            rows = self._api_call(
                "order margins", lambda: api.order_margins(payload), timeout=3.0
            )
        except Exception as exc:
            logger.warning(f"Kite order-margin preflight failed for {symbol}: {exc}")
            return None
        if not rows:
            return None
        try:
            return float((rows[0] or {}).get("total") or 0.0)
        except (TypeError, ValueError, IndexError):
            return None

    def preflight_entry_margin(self, **kwargs) -> Dict[str, Any]:
        """Compare the exact required margin against free cash, where both are known.

        Uses /margins/orders for the requirement rather than deriving it: it is the
        number Kite actually charges, premium and charges included. Free cash comes
        from /user/margins, which is frequently unavailable outside market hours — so a
        missing balance degrades to a stated limitation instead of blocking entries on
        a Zerodha endpoint quirk.
        """
        if self.api is None:
            return {"ok": False, "reservation_id": "", "basis": "NO_SESSION",
                    "limitation": "no authenticated Kite session"}

        symbol = str(kwargs.get("symbol") or "")
        qty = int(kwargs.get("qty") or kwargs.get("quantity") or 0)
        side = str(kwargs.get("side") or "BUY")
        product_type = str(kwargs.get("product_type") or "CARRYFORWARD")

        required = (
            self.required_entry_margin(symbol, qty, side, product_type)
            if symbol and qty > 0 else None
        )
        available = self.available_funds()

        if required is None:
            return {"ok": True, "reservation_id": "", "basis": "MARGIN_UNVERIFIED",
                    "available": available, "required": None,
                    "limitation": "Kite did not return an order margin; entry not verified"}
        if available is None:
            return {"ok": True, "reservation_id": "", "basis": "REQUIREMENT_ONLY",
                    "available": None, "required": required,
                    "limitation": "free cash unavailable (RMS limits are not published "
                                  "outside market hours); requirement known but unchecked"}
        return {
            "ok": bool(available >= required),
            "reservation_id": "",
            "basis": "KITE_ORDER_MARGIN",
            "available": available,
            "required": required,
            "limitation": "" if available >= required else (
                f"insufficient funds: {required:,.2f} required, {available:,.2f} available"
            ),
        }
