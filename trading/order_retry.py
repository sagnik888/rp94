"""Shared retry policy for broker entry and exit orders."""

from __future__ import annotations

import time
from typing import Callable, Tuple

from loguru import logger


class BrokerOrderAmbiguous(RuntimeError):
    """Broker call may have reached the market, but no order id was returned."""


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
            order_id = extract_order_id(operation())
            if order_id:
                return order_id, "", attempt + 1
            last_error = "Broker rejected order"
        except BrokerOrderAmbiguous as exc:
            last_error = f"ambiguous broker acknowledgement: {exc}"
            logger.error(
                f"{label} acknowledgement is ambiguous; stopping retries to avoid duplicate live orders"
            )
            return "", last_error, attempt + 1
        except Exception as exc:
            last_error = str(exc)

        if attempt < len(ORDER_RETRY_DELAYS):
            delay = ORDER_RETRY_DELAYS[attempt]
            logger.warning(
                f"{label} failed on attempt {attempt + 1}/{attempts}; "
                f"retrying in {delay:.0f}s"
            )
            time.sleep(delay)

    return "", last_error or "Unknown broker order failure", attempts
