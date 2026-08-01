"""AngelOne's gateway refusal envelope must read as a refusal (live incident, 2026-07-31).

AngelOne answers a refusal in two unrelated shapes. The order/quote service replies
    {"status": False, "errorcode": "AB4046", "message": ...}
while the API gateway — unregistered source IP, dead session, throttling — replies
    {"success": False, "errorCode": "AG7002", "message": ...}
with no "status" key at all and a capital-C "errorCode".

Every check in trading/broker.py tested only the first shape. For the gateway shape
`response.get("status") is not False` is True and `response.get("errorcode")` is None,
so cancel_order() and modify_protective_stop() returned True — they reported a hard
rejection as SUCCESS. emergency_flatten_trade() treats a True cancel as proof the SL-M
is gone: it clears protective_order_id and fires a market exit while the stop is still
resting at the exchange, so the stop later triggers on a flat book and opens a naked
reversed leg. That polarity must never regress.
"""
import threading

import pytest

from trading.broker import SmartApiBroker
from trading.order_retry import (
    BrokerOrderRejected,
    ORDER_RETRY_DELAYS,
    broker_rejection,
    execute_order_with_retry,
    is_terminal_rejection,
)

# The exact payload logged at 2026-07-31 14:51:19.874.
AG7002 = {
    "success": False,
    "message": "49.37.34.211 is not a registered IP, please check your registered IP.",
    "errorCode": "AG7002",
    "data": "",
}
AB4046 = {
    "message": "Symbol token not found in scrip master cache for the given exchange",
    "errorcode": "AB4046",
    "status": False,
    "data": None,
}
ACCEPTED = {"status": True, "message": "SUCCESS", "errorcode": "", "data": {"orderid": "250731000123"}}


class _Api:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def _record(self, *_args, **_kwargs):
        self.calls += 1
        return self.response

    placeOrder = _record
    cancelOrder = _record
    modifyOrder = _record


def _broker(response):
    b = SmartApiBroker.__new__(SmartApiBroker)
    b.api = _Api(response)
    b._order_lock = threading.Lock()
    b._execution_lease = None
    b._api_call = lambda ctx, fn, timeout=5.0: fn()
    b._entry_rate_allowed = lambda: True   # rate limiting is tested separately
    return b


@pytest.mark.parametrize("payload", [AG7002, AB4046], ids=["gateway", "application"])
def test_both_refusal_envelopes_are_recognised(payload):
    assert broker_rejection(payload)


def test_success_envelopes_are_not_refusals():
    assert broker_rejection(ACCEPTED) == ""
    assert broker_rejection({"status": True, "data": {"availablecash": "50000"}}) == ""
    # placeOrder() returns a bare order-id string; "not a dict" is not a refusal.
    assert broker_rejection("250731000123") == ""


def test_rejected_cancellation_reports_failure_not_success():
    """A True here makes emergency_flatten_trade double-exit the position."""
    assert _broker(AG7002).cancel_order("250731000123", "STOPLOSS") is False
    assert _broker(AB4046).cancel_order("250731000123", "STOPLOSS") is False
    assert _broker(ACCEPTED).cancel_order("250731000123", "STOPLOSS") is True


def test_rejected_protective_stop_modify_reports_failure_not_success():
    """A True here records a trigger the exchange never accepted, permanently."""
    broker = _broker(AG7002)
    assert broker.modify_protective_stop(
        "OID-1", "NIFTY04AUG2624350CE", "60620", 65, "SELL", 105.78
    ) is False
    assert _broker(ACCEPTED).modify_protective_stop(
        "OID-1", "NIFTY04AUG2624350CE", "60620", 65, "SELL", 105.78
    ) is True


def test_entry_rejection_carries_the_brokers_own_words():
    """The dashboard showed a bare "BROKER ERROR"; the cause was known and discarded."""
    with pytest.raises(BrokerOrderRejected) as excinfo:
        _broker(AG7002).place_order("MIDCPNIFTY25AUG2614825PE", "60620", 120, "BUY", price=200.8)
    assert "not a registered IP" in excinfo.value.reason
    assert excinfo.value.terminal is True


def test_terminal_rejection_stops_after_one_attempt():
    """Seven doomed live attempts over ~40s burn the entry rate limit for nothing."""
    attempts_made = []

    def operation():
        attempts_made.append(1)
        raise BrokerOrderRejected(
            "[AG7002] 49.37.34.211 is not a registered IP", terminal=True
        )

    order_id, error, attempts = execute_order_with_retry(
        operation, lambda r: "", "Entry order TEST"
    )
    assert order_id == ""
    assert attempts == 1
    assert len(attempts_made) == 1
    assert "registered IP" in error


def test_non_terminal_rejection_still_uses_the_full_retry_schedule(monkeypatch):
    monkeypatch.setattr("trading.order_retry.time.sleep", lambda _s: None)
    calls = []

    def operation():
        calls.append(1)
        raise BrokerOrderRejected("RMS: order price out of range", terminal=False)

    _, _, attempts = execute_order_with_retry(operation, lambda r: "", "Entry order TEST")
    assert attempts == len(ORDER_RETRY_DELAYS) + 1
    assert len(calls) == len(ORDER_RETRY_DELAYS) + 1


def test_terminal_classification_covers_the_known_permanent_codes():
    assert is_terminal_rejection("[AG7002] ... is not a registered IP")
    assert is_terminal_rejection("[AB1010] Invalid session")
    assert not is_terminal_rejection("RMS: order price out of range")
    assert not is_terminal_rejection("")
