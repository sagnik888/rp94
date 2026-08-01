"""Shared retry policy for broker entry and exit orders."""

from __future__ import annotations

import time
from typing import Any, Callable, Tuple

from loguru import logger


# ── AngelOne refusal envelopes ────────────────────────────────────────────────
# AngelOne answers a refusal in two unrelated shapes, and code written against one
# silently misses the other. The order/quote service replies
#     {"status": False, "errorcode": "AB4046", "message": ...}
# while the API gateway — unregistered source IP, dead session, throttling — replies
#     {"success": False, "errorCode": "AG7002", "message": ...}
# with no "status" key at all and a capital-C "errorCode". On 2026-07-31 every check
# in trading/broker.py tested only the first shape, so a hard AG7002 rejection read
# as a *successful* cancellation in cancel_order() and modify_protective_stop().
_TERMINAL_REJECTION_CODES = ("AG7002", "AG8001", "AG8002", "AG8003", "AB1010", "AB4046")

_TERMINAL_REJECTION_MARKERS = (
    "not a registered ip",
    "registered ip",
    "invalid token",
    "invalid session",
    "session expired",
    "token expired",
)


def broker_rejection(response: Any) -> str:
    """Return the broker's refusal text, or "" when the response is not a refusal.

    Recognises strictly MORE refusals than the per-call checks it replaces, so no
    response that previously read as success can start failing here. Non-dict
    responses are left unclassified on purpose: SmartConnect.placeOrder() returns a
    bare order-id string on success, so "not a dict" is not evidence of refusal.
    """
    if not isinstance(response, dict):
        return ""
    code = str(response.get("errorcode") or response.get("errorCode") or "").strip()
    if not code and response.get("status") is not False and response.get("success") is not False:
        return ""
    message = str(
        response.get("message")
        or response.get("errormessage")
        or response.get("errorMessage")
        or ""
    ).strip()
    if code and message:
        return f"[{code}] {message}"
    if code:
        return f"[{code}] broker refused without a message"
    return message or "broker refused without a message"


def is_terminal_rejection(reason: str) -> bool:
    """True when retrying this refusal cannot possibly succeed.

    Retrying a dead session or an unregistered source IP burns the entry rate limit
    and delays the operator alert by the full ~40s retry schedule for nothing.
    """
    text = str(reason or "")
    if any(marker in text.lower() for marker in _TERMINAL_REJECTION_MARKERS):
        return True
    upper = text.upper()
    return any(code in upper for code in _TERMINAL_REJECTION_CODES)


class BrokerOrderAmbiguous(RuntimeError):
    """Broker call may have reached the market, but no order id was returned."""


class BrokerOrderRejected(RuntimeError):
    """Broker explicitly refused the order; it never reached the market.

    `terminal` marks a refusal that cannot possibly succeed on a retry — an
    unregistered source IP, a dead session, an unknown contract token. Those
    burn the entry rate limit and delay the real alert by ~40s if retried, and
    on 2026-07-31 that is exactly what happened to every order after 14:51.
    """

    def __init__(self, message: str, code: str = "", terminal: bool = False) -> None:
        super().__init__(message)
        self.reason = str(message or "")
        self.code = str(code or "")
        self.terminal = bool(terminal)

    def describe(self) -> str:
        return f"{self.code}: {self.reason}" if self.code else self.reason


# Seven attempts total: five in the 3-5 second band, then two 10-second retries.
ORDER_RETRY_DELAYS = (4.0, 4.0, 4.0, 4.0, 10.0, 10.0)


def execute_order_with_retry(
    operation: Callable[[], object],
    extract_order_id: Callable[[object], str],
    label: str,
) -> Tuple[str, str, int]:
    """Execute one broker operation under the standard retry schedule."""
    last_error = ""
    attempts = len(ORDER_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            res = operation()
        except BrokerOrderAmbiguous as exc:
            last_error = f"ambiguous broker acknowledgement: {exc}"
            logger.error(
                f"{label} acknowledgement is ambiguous; stopping retries to avoid duplicate live orders"
            )
            return "", last_error, attempt + 1
        except BrokerOrderRejected as exc:
            # An explicit refusal never reached the market, so re-sending cannot
            # duplicate anything — but a terminal one (dead session, unregistered
            # source IP, unknown contract) cannot succeed either. Retrying it seven
            # times burns the entry rate limit and delays the real alert by ~40s.
            last_error = exc.describe()
            if exc.terminal:
                logger.error(
                    f"{label} refused by the broker: {last_error}. "
                    "This cannot succeed on a retry — stopping now."
                )
                return "", last_error, attempt + 1
            if attempt < len(ORDER_RETRY_DELAYS):
                delay = ORDER_RETRY_DELAYS[attempt]
                logger.warning(
                    f"{label} refused ({last_error}); retrying in {delay:.0f}s"
                )
                time.sleep(delay)
            continue
        except Exception as exc:
            last_error = str(exc)
            if attempt < len(ORDER_RETRY_DELAYS):
                delay = ORDER_RETRY_DELAYS[attempt]
                logger.warning(
                    f"{label} failed on attempt {attempt + 1}/{attempts}; "
                    f"retrying in {delay:.0f}s"
                )
                time.sleep(delay)
            continue
            
        try:
            order_id = extract_order_id(res)
            if order_id:
                return order_id, "", attempt + 1
            last_error = "Broker rejected order"
        except Exception as exc:
            last_error = f"ambiguous broker acknowledgement: {exc}"
            logger.error(f"{label} order placed but ID extraction failed; stopping retries: {exc}")
            return "", last_error, attempt + 1

        if attempt < len(ORDER_RETRY_DELAYS):
            delay = ORDER_RETRY_DELAYS[attempt]
            logger.warning(
                f"{label} failed on attempt {attempt + 1}/{attempts}; "
                f"retrying in {delay:.0f}s"
            )
            time.sleep(delay)

    return "", last_error or "Unknown broker order failure", attempts
