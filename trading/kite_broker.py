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

import hashlib
import re
import threading
import time
from collections import deque
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
        self._entry_order_times: deque = deque()
        # Cash this process has committed to orders Kite's own margin figure has not yet
        # deducted. Guarded by its own lock: preflight runs on the entry workers, and two
        # of them reading the same free cash is the exact over-commit this prevents.
        self._margin_lock = threading.Lock()
        self._margin_reservations: Dict[str, Dict[str, Any]] = {}
        self._margin_reservation_ttl_seconds = 90.0
        self._entry_kill_latched_until = 0.0
        # Entries are placed from background threads (one per trade), so the window
        # bookkeeping below is genuinely concurrent. Without this lock two threads can
        # both read len()==limit-1, both pass, and both place — overshooting the cap the
        # kill-switch exists to enforce.
        self._entry_rate_lock = threading.Lock()
        self._position_backoff_until = 0.0
        self._position_backoff_failures = 0
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
        # `timeout` is advisory and deliberately NOT applied to the SDK client.
        # KiteConnect.timeout is a single mutable attribute on a client this process
        # SHARES with data/kite_market_data.py, which guards it with a different lock.
        # Setting it here per-call raced the data path: a quote in flight could inherit
        # this call's 2s budget instead of the client default, and the restore could put
        # back a value another thread had just changed. The SDK default (7s) applies to
        # every call; bound a slow broker at a higher level, not by mutating shared state.
        api = self.api
        previous = None
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

    # ── runaway-entry kill switch ────────────────────────────────────────────
    def _entry_rate_allowed(self) -> bool:
        """Latch entries off once too many fire inside the configured window.

        Nothing else in the system bounds how many live entry orders a signal loop
        can send, so this is the only backstop against a runaway. It counts entries
        only — risk-reducing exits and protective stops are never gated by it,
        because blocking those would turn a runaway into an unprotected position.
        """
        from config.settings import get_settings

        settings = get_settings()
        window = max(1.0, float(getattr(settings, "entry_order_rate_window_seconds", 60.0) or 60.0))
        limit = max(1, int(getattr(settings, "max_entry_orders_per_minute", 6) or 6))
        latch = max(10.0, float(getattr(settings, "entry_order_kill_latch_seconds", 300.0) or 300.0))
        # Read-decide-record must be atomic, or concurrent entry threads each see room
        # under the cap and collectively exceed it.
        with self._entry_rate_lock:
            now = time.monotonic()
            if now < self._entry_kill_latched_until:
                return False
            while self._entry_order_times and now - self._entry_order_times[0] >= window:
                self._entry_order_times.popleft()
            if len(self._entry_order_times) >= limit:
                self._entry_kill_latched_until = now + latch
                logger.critical(
                    f"ENTRY ORDER RATE KILL-SWITCH: {len(self._entry_order_times)} orders "
                    f"inside {window:.0f}s; entries blocked for {latch:.0f}s."
                )
                return False
            self._entry_order_times.append(now)
            return True

    def _release_entry_reservation(self) -> None:
        """Give back the slot taken by an order that provably never reached the exchange.

        Entries retry through execute_order_with_retry — 7 attempts over ~36s, all inside
        the 60s window. Counting each ATTEMPT means a single RMS refusal on one trade
        consumes the whole default budget of 6 and latches every entry off for 300s,
        without a single order having reached the market. The cap is on live orders, so
        only a definitive non-placement releases; an ambiguous one keeps its slot,
        because that order may well be resting.
        """
        with self._entry_rate_lock:
            try:
                self._entry_order_times.pop()
            except IndexError:
                # Racing releases on the last element would otherwise raise IndexError
                # out of place_order's except-block, replacing the classified
                # BrokerOrderRejected/BrokerOrderAmbiguous the caller depends on.
                pass

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
        """The conviction floor for a LIMIT entry, normalised to the 0-1 scale.

        Every other confidence setting in this system is a fraction (0.5, 0.62, 0.7,
        0.72 ...) and `trade.confidence` is a fraction too — it is printed as
        `{confidence:.0%}` throughout the engine. This one shipped as 95.0, so
        `confidence >= threshold` was comparing at most 1.0 against 95.0 and was ALWAYS
        false: the LIMIT path was unreachable and every entry went out as MARKET,
        however strong the signal.

        A value above 1 is read as a percentage so an existing 95.0 in .env or in a
        persisted settings file keeps meaning "95%" rather than silently disabling the
        gate again.
        """
        try:
            from config.settings import get_settings

            raw = float(getattr(get_settings(), "entry_limit_confidence_threshold", 0.95) or 0.95)
        except Exception:
            return 0.95
        if raw <= 0:
            return 0.95
        return raw / 100.0 if raw > 1.0 else raw

    @staticmethod
    def _entry_limit_ttl_minutes() -> int:
        try:
            from config.settings import get_settings

            return max(0, int(getattr(get_settings(), "kite_entry_limit_ttl_minutes", 0) or 0))
        except Exception:
            return 0

    @staticmethod
    def order_tag(trade_id: str, leg: str = "ENTRY", attempt: int = 0) -> str:
        """A <=20 char Kite tag that maps ONE order leg back to one trade exactly.

        Kite echoes the tag on the order book, on trades, and on every postback. That
        turns order->trade matching from a heuristic (symbol + side + quantity, which
        is ambiguous the moment two trades sit on one strike) into an exact lookup —
        the precise ambiguity that stranded two trades on 2026-07-31.

        Trade ids look like T0005_MIDCPNIFTY_07311451 (25 chars), so the instrument is
        dropped: the counter and timestamp are what make it unique.

        The ENTRY/attempt-0 form is deliberately byte-identical to the single-argument
        version this replaced, so a tagged order already resting at the exchange stays
        findable across this change. Every other leg gets a distinct U1-prefixed tag —
        without that, an exit and its entry would collide on one string and the lookup
        would return AMBIGUOUS exactly when it is needed most. The attempt code keeps a
        replaced entry distinct from the one it replaced, so a cancel/replace race
        cannot make two live orders share a tag.
        """
        raw = str(trade_id or "").strip()
        if not raw:
            return ""
        leg_code = {
            "ENTRY": "E", "EXIT": "X", "PROTECTIVE": "S", "STOP": "S",
        }.get(str(leg or "").strip().upper())
        if not leg_code:
            raise ValueError(f"unsupported order-tag leg: {leg!r}")
        parts = raw.split("_")
        if leg_code == "E" and int(attempt or 0) == 0:
            candidate = f"{parts[0]}_{parts[-1]}" if len(parts) >= 3 else raw
            return candidate[:20]
        compact = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
        if not compact or len(compact) > 15:
            compact = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:15].upper()
        attempt_code = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"[max(0, int(attempt or 0)) % 36]
        return f"U1{leg_code}{attempt_code}{compact[:14]}"[:20]

    def inspect_order_by_tag(self, tag: str) -> Dict[str, Any]:
        """Find an order by its EXACT echoed tag. The point of tagging at all.

        Tags were already being written on entry orders and read by nothing, so an
        unacknowledged submission still had to be treated as unknowable — which is what
        every latch and withhold in trade_manager works around. This closes that loop:
        after a lost acknowledgement, ask the order book whether the tag exists.

        Tri-state on purpose, and ABSENT is never inferred from a failed read: an order
        book we could not fetch is UNKNOWN. Concluding "the order was never placed" from
        a scan that errored is precisely how a live position gets duplicated.
        """
        wanted = str(tag or "").strip()
        if not wanted:
            return {"state": "UNKNOWN", "order_id": "", "matches": [],
                    "reason": "order tag is empty"}
        rows = self._order_book()
        if rows is None:
            return {"state": "UNKNOWN", "order_id": "", "matches": [],
                    "reason": "order book could not be read"}
        matches = [row for row in rows if str(row.get("tag") or "").strip() == wanted]
        if not matches:
            return {"state": "ABSENT", "order_id": "", "matches": [],
                    "reason": "exact order tag not found"}
        if len(matches) != 1:
            return {"state": "AMBIGUOUS", "order_id": "", "matches": matches,
                    "reason": f"{len(matches)} orders share exact tag {wanted}"}
        row = matches[0]
        return {
            "state": "FOUND",
            "order_id": str(row.get("orderid") or ""),
            "status": str(row.get("orderstatus") or ""),
            "order": row,
            "matches": matches,
            "reason": "exact order tag found",
        }

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
        self._escalate_if_ip_blocked(detail)
        raise BrokerOrderRejected(detail, terminal=(kind == "TERMINAL")) from exc

    # Markers for the one refusal that is not about the order at all. Two shapes: an
    # address Kite rejected, and an app with an EMPTY allow-list. The second is the
    # nastier one — an empty list denies every order rather than allowing all, so
    # "clear the list" looks like a fix for a dynamic connection and is the opposite.
    _IP_BLOCK_MARKERS = (
        "is not allowed to place orders",
        "allowed ips",
        "no ips configured",
    )

    def _escalate_if_ip_blocked(self, detail: str) -> None:
        """Shout once when the broker is refusing EVERY order on account configuration.

        An IP-permission refusal is not an order problem: nothing about the symbol, size
        or price can change it, and it will refuse every order until a human edits the
        Kite developer console. It was logged at ERROR next to ordinary per-order
        refusals, so on 2026-08-04 three dead signals read as three unrelated failures
        rather than one blanket block, and the cause was only findable by grepping logs.

        Latched on the ADDRESS rather than a bare flag, so a changed egress IP -- the
        usual way this shows up -- escalates again instead of being swallowed by the latch.
        """
        text = str(detail or "").lower()
        if not any(marker in text for marker in self._IP_BLOCK_MARKERS):
            return
        match = re.search(r"IP \(([^)]+)\)", str(detail))
        refused = match.group(1) if match else ""
        # The "no IPs configured" variant names no address at all, so the operator is
        # told to add an IP without being told WHICH. Look up what this machine actually
        # presents, so the alert carries the value to paste into the console.
        own = ""
        if not refused:
            try:
                from data.kite_auth import current_egress_ip

                own = current_egress_ip("v4")
            except Exception as exc:
                logger.debug(f"Egress lookup for the IP-block alert failed: {exc}")
        key = refused or own or "unknown"
        if getattr(self, "_ip_block_alerted", "") == key:
            return
        self._ip_block_alerted = key
        if refused:
            logger.critical(
                f"ORDERS BLOCKED AT THE BROKER: Kite is refusing EVERY order because "
                f"this machine's outbound address {refused} is not whitelisted for the "
                "app. No order can reach the exchange until it is added on the Kite "
                "developer console. Check the address family: a whitelisted IPv4 does "
                "NOT cover an IPv6 egress, and the address above is what Kite saw."
            )
        else:
            logger.critical(
                "ORDERS BLOCKED AT THE BROKER: the Kite app has NO allowed IPs "
                "configured, and an empty list DENIES every order rather than allowing "
                f"all. Add {own or 'this machine public IPv4'} on the Kite developer "
                "console. Keep kite_registered_ip in step with it so the readiness gate "
                "warns when this connection's address drifts."
            )

    # ── entry ────────────────────────────────────────────────────────────────
    def place_order(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "CARRYFORWARD",
        price: float = 0.0,
        # Accepted and deliberately IGNORED. trade_manager still computes and passes a
        # touch quote, which fed AngelOne's bid/ask-anchored limit policy; that policy
        # was retired here in favour of confidence-gated MARKET/LIMIT. Kept in the
        # signature so the existing call site keeps working — the caller-side plumbing
        # (and the option_*/execution_touch_* settings behind it) is now dead and wants
        # its own removal pass.
        touch_quote: Optional[Dict[str, Any]] = None,
        trade_id: str = "",
        confidence: float = 0.0,
    ) -> Optional[str]:
        try:
            self._require_account_execution_lease("entry order")
        except PermissionError as exc:
            raise BrokerOrderRejected(str(exc), terminal=True) from exc
        api = self._require_api("entry order")
        # Checked after the session, so a dead-token failure does not consume the
        # entry budget for an order that was never going to be sent.
        if not self._entry_rate_allowed():
            return None

        try:
            return self._submit_entry(
                api, symbol, token, qty, side, product_type, price, trade_id, confidence,
            )
        except (BrokerOrderRejected, BrokerOrderAmbiguous):
            raise
        except Exception:
            # Anything unclassified between taking the slot and submitting means no
            # order went out, so the reservation must go back — otherwise a repeated
            # local failure latches the kill-switch with nothing at the exchange.
            self._release_entry_reservation()
            raise

    def _submit_entry(
        self, api, symbol: str, token: str, qty: int, side: str, product_type: str,
        price: float, trade_id: str, confidence: float,
    ) -> Optional[str]:
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
            if classify_kite_exception(exc) != "AMBIGUOUS":
                # Refused outright: nothing is live, so this attempt must not count
                # against the runaway-entry budget it never used.
                self._release_entry_reservation()
            self._raise_for("entry order", symbol, exc)

        order_id = str(order_id or "")
        if not order_id or order_id == "None":
            # Kite returns an id on success, so an empty one is unexplained. Ask the
            # order book whether anything actually reached the exchange; only a
            # completed scan that finds nothing may be called a clean failure.
            resting = self.inspect_recent_entry_order(symbol, token, side, qty)
            state = str(resting.get("state") or "UNKNOWN").upper()
            if state == "ABSENT":
                self._release_entry_reservation()
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
        # Optional so simulation brokers and older call sites keep working. When set, an
        # exit whose acknowledgement is lost stops being unknowable — inspect_order_by_tag
        # can say whether it reached the exchange, instead of the caller having to latch
        # and wait for a human.
        tag: str = "",
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
                    **({"tag": str(tag)[:20]} if tag else {}),
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
        product_type: str = "CARRYFORWARD", tag: str = "",
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
                    **({"tag": str(tag)[:20]} if tag else {}),
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
        order_id = str(order_id or "")
        if not order_id or order_id == "None":
            # Kite returns an id on success, so an empty one is unexplained — NOT a
            # refusal. Returning None here is audit BLOCKER-1 (2026-07-24) verbatim:
            # the caller cannot tell it from a clean reject, re-places, and two SL-M
            # orders rest at the exchange. When both trigger, the second fires against
            # a flat book and opens a naked reversed leg.
            raise BrokerOrderAmbiguous(
                f"Protective stop for {symbol} returned no order id; it may be resting "
                "at the exchange. Reconcile before placing another."
            )
        return order_id

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
        # A GTT is a real resting order on the account, so it needs the same lease as
        # every other write. Without this a process that does NOT own the account could
        # arm a trigger on top of the stop the true holder is managing, and both fire.
        self._require_account_execution_lease("GTT stop")
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
            # A GTT is a resting order on Zerodha's servers, so a call that did not
            # complete may still have created one. Reporting that as a clean refusal
            # leaves gtt_trigger_id empty and the next cycle arms a SECOND trigger:
            # one fires and exits, the other fires against a flat book and opens a
            # naked reversed leg. Ask the server what actually exists before deciding.
            if classify_kite_exception(exc) == "AMBIGUOUS":
                existing = self.find_gtt_stop(symbol)
                if str(existing.get("state") or "") == "FOUND":
                    found_id = str(existing.get("trigger_id") or "")
                    logger.warning(
                        f"Recovered GTT {found_id} for {symbol} after a lost "
                        "acknowledgement; not arming a second trigger."
                    )
                    return found_id or None
                if str(existing.get("state") or "") == "UNKNOWN":
                    raise BrokerOrderAmbiguous(
                        f"GTT for {symbol} did not complete and the trigger list could "
                        f"not be read ({exc}); it may be armed."
                    ) from exc
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
        # The last account write without the lease, and the worst polarity of the set:
        # this STRIPS protection rather than adding or rewriting it.
        try:
            self._require_account_execution_lease("GTT cancellation")
        except PermissionError as exc:
            logger.error(f"GTT cancellation refused for {trigger_id}: {exc}")
            return False
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
        # Same lease as every other account write. SmartApiBroker required it here and
        # KiteBroker did not, so a second instance that did NOT own the account could
        # rewrite the trigger and quantity of the stop the true holder was managing —
        # and the holder's local protective_trigger would keep its own stale value, so
        # it would never notice or correct the divergence.
        self._require_account_execution_lease("protective stop modification")
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

    @staticmethod
    def _position_should_backoff(payload) -> bool:
        text = str(payload or "").lower()
        markers = (
            "too many requests",
            "exceeding access rate",
            "access rate",
            "rate limit",
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

    def get_positions(self) -> Optional[Dict]:
        """Normalised to {"data": [...]} with netqty, as the reconciler expects."""
        api = self.api
        if api is None:
            return None
        # Without this cooldown a throttled positions endpoint is re-hit on every
        # reconcile tick, which is what keeps the throttle alive.
        remaining = float(getattr(self, "_position_backoff_until", 0.0) or 0.0) - time.time()
        if remaining > 0:
            logger.debug(f"Skipping broker positions; backoff active for {remaining:.1f}s")
            return None
        try:
            book = self._api_call("fetch positions", lambda: api.positions(), timeout=2.0)
        except Exception as exc:
            if self._position_should_backoff(exc):
                self._mark_position_backoff(exc)
            logger.error(f"Kite positions read failed: {exc}")
            return None
        self._position_backoff_failures = 0
        self._position_backoff_until = 0.0
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

    @staticmethod
    def _quantity_could_match(row: Dict[str, Any], wanted_qty: int) -> bool:
        """False ONLY when the row's size is known and differs.

        Asymmetric on purpose. Adopting a differently-sized order books this trade
        against a stranger's position and leaves the difference unprotected — but
        answering "no match" from a size we could not read is the worse error, because
        it reaches the caller as ABSENT and re-places a possibly-live order. An
        unparseable size therefore stays adoptable.
        """
        if wanted_qty <= 0:
            return True
        raw = row.get("quantity")
        if raw is None or raw == "":
            return True
        try:
            return int(float(raw)) == wanted_qty
        except (TypeError, ValueError):
            return True

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
        if not symbol:
            # Cannot identify the contract, so nothing can be concluded. Falling through
            # would match no rows and answer ABSENT — "definitively not placed" — from a
            # scan that never had a chance to look.
            return {
                "state": "UNKNOWN", "order_id": "", "matches": [],
                "reason": "entry symbol is empty",
            }
        wanted_symbol = str(symbol or "").upper()
        wanted_side = str(side or "").upper()
        wanted_qty = max(0, int(quantity or 0))
        matches = [
            row for row in rows
            if str(row.get("tradingsymbol") or "").upper() == wanted_symbol
            and (not wanted_side or str(row.get("transactiontype") or "").upper() == wanted_side)
            and str(row.get("ordertype") or "").upper() not in {"SL", "SL-M"}
            and self._quantity_could_match(row, wanted_qty)
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
        if not symbol:
            return {
                "state": "UNKNOWN", "order_id": "", "matches": [],
                "reason": "protective-order symbol is empty",
            }
        wanted_symbol = str(symbol or "").upper()
        wanted_side = str(side or "").upper()
        wanted_qty = max(0, int(quantity or 0))
        wanted_product = self._product(product_type) if product_type else ""
        working = [
            row for row in rows
            if str(row.get("tradingsymbol") or "").upper() == wanted_symbol
            and str(row.get("ordertype") or "").upper() in {"SL", "SL-M"}
            and str(row.get("orderstatus") or "").lower() in _WORKING_STATUSES
            and (not wanted_side or str(row.get("transactiontype") or "").upper() == wanted_side)
        ]
        if not working:
            return {"state": "NONE", "order_id": "", "reason": "no working stop", "matches": []}

        # Split on metadata rather than taking working[0]. A caller that cancels the
        # first match and then acts as if the contract is clear will cancel ONE of two
        # resting stops and exit over the other — the duplicate-SL-M hazard this whole
        # subsystem exists to prevent. The AngelOne broker made these distinctions and
        # they were lost when it was deleted; T2 in trade_manager depends on them.
        wanted_token = str(token or "").strip()
        wanted_trigger = self._tick_price(trigger_price) if float(trigger_price or 0.0) > 0 else 0.0
        exact, conflicts, stale_trigger = [], [], []
        for row in working:
            # One rule for "is this row my size", shared with the entry scan. The
            # inline version here read a missing quantity as 0 and so as a CONFLICT,
            # while an unparseable string counted as a match — two contradictory
            # answers to the same question in one file.
            ok = self._quantity_could_match(row, wanted_qty)
            if ok and wanted_product:
                row_product = str(row.get("producttype") or "").upper()
                ok = not row_product or row_product == wanted_product
            if ok and wanted_token:
                row_token = str(row.get("instrument_token") or row.get("symboltoken") or "").strip()
                ok = not row_token or row_token == wanted_token
            if not ok:
                conflicts.append(row)
                continue
            # The stop is the right one but armed at the wrong level. That is neither a
            # clean FOUND (adopting it silently accepts a trigger the caller did not ask
            # for) nor a CONFLICT (it IS this position's stop, and modifying it is the
            # correct repair). Callers that pass no trigger_price never see this state.
            if wanted_trigger:
                row_trigger = row.get("triggerprice")
                try:
                    matches_level = abs(float(row_trigger) - wanted_trigger) <= 0.051
                except (TypeError, ValueError):
                    matches_level = True        # unreadable level is not proof of drift
                if not matches_level:
                    stale_trigger.append(row)
                    continue
            exact.append(row)

        if (
            len(exact) > 1
            or len(stale_trigger) > 1
            or (exact and (conflicts or stale_trigger))
            or (stale_trigger and conflicts)
        ):
            return {
                "state": "AMBIGUOUS", "order_id": "", "matches": working,
                "reason": f"{len(working)} working protective stops for this contract",
            }
        if stale_trigger:
            row = stale_trigger[0]
            return {
                "state": "FOUND_STALE_TRIGGER",
                "order_id": str(row.get("orderid") or ""),
                "trigger_price": row.get("triggerprice"),
                "reason": "this position's stop is armed at a different trigger",
                "matches": stale_trigger,
            }
        if exact:
            return {
                "state": "FOUND", "order_id": str(exact[0].get("orderid") or ""),
                "reason": "working protective stop found", "matches": exact,
            }
        if len(conflicts) > 1:
            return {
                "state": "AMBIGUOUS", "order_id": "", "matches": conflicts,
                "reason": "multiple working stops, none matching this position",
            }
        return {
            "state": "CONFLICT", "order_id": "", "matches": conflicts,
            "reason": "a working stop exists but its quantity/product does not match",
        }

    def find_working_protective_stop(
        self, symbol: str, token: str, side: str, quantity: int = 0,
        product_type: str = "",
    ) -> str:
        # product_type is forwarded rather than left to the inspector's CARRYFORWARD
        # default: with an INTRADAY/MIS position that default classifies a perfectly
        # good MIS stop as CONFLICT, which reads here as "no stop".
        result = self.inspect_working_protective_stop(
            symbol, token, side, quantity=quantity, product_type=product_type,
        )
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

        # Reserve against CONCURRENT entries. Without this, two entry workers reading the
        # same free cash both pass and the account over-commits: /user/margins does not
        # reflect an order that was placed moments ago, so the second check sees money the
        # first has already spent. The reservation is this process's own memory of what it
        # has committed but not yet seen deducted.
        #
        # reservation_id and release_entry_margin_reservation were already declared on the
        # base Broker (broker.py:173-183) and returned "" here — the contract was designed
        # and never implemented. entry_margin_buffer_pct (settings.py:423, with a 0-100
        # validator) and requires_entry_margin_preflight (line 103 of this file) were both
        # likewise sitting unread.
        buffer_pct = self._entry_margin_buffer_pct()
        committed = required * (1.0 + buffer_pct / 100.0)
        with self._margin_lock:
            self._expire_margin_reservations()
            outstanding = sum(r["amount"] for r in self._margin_reservations.values())
            spendable = available - outstanding
            if spendable < committed:
                return {
                    "ok": False, "reservation_id": "", "basis": "KITE_ORDER_MARGIN",
                    "available": available, "required": required,
                    "reserved": outstanding,
                    "limitation": (
                        f"insufficient funds: {committed:,.2f} required "
                        f"(incl {buffer_pct:.0f}% buffer), {available:,.2f} available"
                        + (f", {outstanding:,.2f} already reserved by in-flight entries"
                           if outstanding else "")
                    ),
                }
            reservation_id = f"{symbol}:{time.time():.6f}"
            self._margin_reservations[reservation_id] = {
                "amount": committed, "at": time.time(), "symbol": symbol,
            }
        return {
            "ok": True,
            "reservation_id": reservation_id,
            "basis": "KITE_ORDER_MARGIN",
            "available": available,
            "required": required,
            "reserved": outstanding,
            "limitation": "",
        }

    def _entry_margin_buffer_pct(self) -> float:
        try:
            from config.settings import get_settings

            value = float(getattr(get_settings(), "entry_margin_buffer_pct", 10.0) or 0.0)
        except Exception:
            return 10.0
        return min(100.0, max(0.0, value))

    def _expire_margin_reservations(self) -> None:
        """Drop reservations older than the TTL. Caller holds _margin_lock.

        A reservation only covers the window between placing an order and Kite's own
        margin figure reflecting it. Without expiry, an entry that died in a way no
        release path caught would hold cash hostage for the rest of the session — which
        would turn a safety feature into a self-inflicted trading halt.
        """
        cutoff = time.time() - self._margin_reservation_ttl_seconds
        for key in [k for k, r in self._margin_reservations.items() if r["at"] < cutoff]:
            logger.warning(
                f"Margin reservation {key} expired after "
                f"{self._margin_reservation_ttl_seconds:.0f}s without release."
            )
            self._margin_reservations.pop(key, None)

    def release_entry_margin_reservation(self, reservation_id: str) -> None:
        """Give the cash back once the entry is definitively not going to consume it.

        Deliberately NOT called on an ambiguous acknowledgement: an order that may be
        live must keep holding its margin, or the next entry spends money this one is
        about to.
        """
        key = str(reservation_id or "")
        if not key:
            return
        with self._margin_lock:
            if self._margin_reservations.pop(key, None) is not None:
                logger.debug(f"Released margin reservation {key}")
