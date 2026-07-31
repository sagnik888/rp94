"""A refused entry must not be reported as ambiguous (live incident, 2026-07-31 13:56).

place_order logged "PLACING REAL ORDER: BUY 65 NIFTY04AUG2624350CE" and 348ms later the
caller reported "acknowledgement is ambiguous". The operator's AngelOne terminal showed no
pending order and no position — the order never reached the exchange — yet the trade was
parked in RECOVERY_REQUIRED limbo where nothing reconciles it.

Two defects behind that:
  * The `no order id` raise logged NOTHING, so the broker's actual response was discarded.
    The SDK does report the reason, but through logzero (SmartApi/smartConnect.py `_request`
    and `placeOrderFullResponse`), which never reached ut1.log — 0 SDK lines in the whole
    file.
  * "No order id" was treated as unknowable. It is only unknowable if we cannot see the
    order book; if we CAN read it and the contract is absent, the placement definitively
    failed and the trade should fail cleanly instead of stranding.

The safety rule that must never regress: we may never conclude "not placed" from a scan we
could not complete, because re-placing a live order caused duplicate orders previously.
"""
import threading

import pytest

from trading.broker import BrokerOrderAmbiguous, SmartApiBroker


class _Api:
    def __init__(self, book):
        self._book = book
        self.calls = 0

    def orderBook(self):
        self.calls += 1
        if isinstance(self._book, Exception):
            raise self._book
        return self._book


def _broker(book=None):
    b = SmartApiBroker.__new__(SmartApiBroker)
    b.api = _Api(book)
    b._order_lock = threading.Lock()
    b._execution_lease = None          # lease is tested separately
    b._api_call = lambda ctx, fn, timeout=5.0: fn()
    return b


def _row(**over):
    row = {
        "tradingsymbol": "NIFTY04AUG2624350CE", "transactiontype": "BUY",
        "quantity": "65", "orderstatus": "open", "orderid": "OID-1",
        "variety": "NORMAL", "ordertype": "LIMIT",
    }
    row.update(over)
    return row


def _ok(rows):
    return {"status": True, "data": rows}


# ── the scan itself ──────────────────────────────────────────────────────────

def test_absent_only_when_the_book_was_actually_read():
    r = _broker(_ok([]))._inspect = None or _broker(_ok([])).inspect_recent_entry_order(
        "NIFTY04AUG2624350CE", side="BUY", quantity=65
    )
    assert r["state"] == "ABSENT"


def test_live_order_is_found_and_its_id_returned():
    r = _broker(_ok([_row()])).inspect_recent_entry_order(
        "NIFTY04AUG2624350CE", side="BUY", quantity=65
    )
    assert r["state"] == "FOUND" and r["order_id"] == "OID-1"


@pytest.mark.parametrize("book", [
    RuntimeError("network down"),
    {"status": False, "message": "rejected"},
    {"status": True, "data": "not-a-list"},
    None,
])
def test_an_unreadable_book_is_UNKNOWN_never_absent(book):
    """The safety-critical direction: never infer 'not placed' from a failed scan."""
    r = _broker(book).inspect_recent_entry_order("NIFTY04AUG2624350CE", side="BUY", quantity=65)
    assert r["state"] == "UNKNOWN", "a scan we could not complete must not read as ABSENT"


def test_terminal_and_stop_rows_are_not_mistaken_for_a_live_entry():
    for over in ({"orderstatus": "rejected"}, {"orderstatus": "cancelled"},
                 {"variety": "STOPLOSS"}, {"ordertype": "STOPLOSS_MARKET"},
                 {"transactiontype": "SELL"}, {"quantity": "130"}):
        r = _broker(_ok([_row(**over)])).inspect_recent_entry_order(
            "NIFTY04AUG2624350CE", side="BUY", quantity=65
        )
        assert r["state"] == "ABSENT", f"{over} should not count as a live entry"


def test_base_broker_defaults_to_unknown():
    """A broker with no order-book support must never claim ABSENT."""
    import inspect as _i
    from trading import broker as bm
    base = None
    for _, obj in _i.getmembers(bm, _i.isclass):
        if obj.__name__ != "SmartApiBroker" and hasattr(obj, "inspect_recent_entry_order"):
            base = obj
            break
    assert base is not None
    inst = base.__new__(base)
    assert base.inspect_recent_entry_order(inst, "X")["state"] == "UNKNOWN"


# ── how place_order uses it ──────────────────────────────────────────────────

def test_place_order_wiring_is_failsafe():
    import inspect
    src = inspect.getsource(SmartApiBroker.place_order)
    body = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    assert "inspect_recent_entry_order" in body, "no order-book check after a missing id"
    assert "Raw broker response" in src, "the broker response is still discarded unlogged"
    # ABSENT -> clean failure; anything else must still be able to raise
    assert '"ABSENT"' in body and "return None" in body
    assert "BrokerOrderAmbiguous" in body, "genuine ambiguity must still raise"


def test_sdk_logs_are_captured_into_the_application_log():
    """0 SmartAPI lines had ever reached ut1.log; the reason was always thrown away."""
    import inspect
    from trading import secure_logging
    src = inspect.getsource(secure_logging)
    assert "_SdkToLoguruHandler" in src
    assert "addHandler" in src
    inst = inspect.getsource(secure_logging.install_smartapi_log_redaction)
    assert "addHandler" in inst, "the capture handler is never installed"
