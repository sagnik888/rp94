"""A refused entry must not be reported as ambiguous (live incident, 2026-07-31 13:56).

place_order logged "PLACING REAL ORDER: BUY 65 NIFTY04AUG2624350CE" and 348ms later the
caller reported "acknowledgement is ambiguous". The operator's AngelOne terminal showed no
pending order and no position — the order never reached the exchange — yet the trade was
parked in RECOVERY_REQUIRED limbo where nothing reconciles it.

Two defects behind that:
  * The `no order id` raise logged NOTHING, so the broker's actual response was discarded.
  * "No order id" was treated as unknowable. It is only unknowable if we cannot see the
    order book; if we CAN read it and the contract is absent, the placement definitively
    failed and the trade should fail cleanly instead of stranding.

The safety rule that must never regress: we may never conclude "not placed" from a scan we
could not complete, because re-placing a live order caused duplicate orders previously.

Proved here against KiteBroker, the live execution broker. The order-book payload changed
shape in the Kite migration (a bare list of typed rows instead of an AngelOne envelope) —
which is exactly the kind of change that can quietly turn "could not read" into "nothing
there", so the UNKNOWN-never-ABSENT direction is re-proved against the new shape.
"""
import threading

import pytest

# NetworkException via kite_broker, which stubs it when the SDK is absent, so this
# file still collects on a machine without kiteconnect.
from trading.kite_broker import KiteBroker, NetworkException
from trading.order_retry import BrokerOrderAmbiguous


class _Api:
    def __init__(self, book):
        self._book = book
        self.calls = 0

    def orders(self):
        self.calls += 1
        if isinstance(self._book, Exception):
            raise self._book
        return self._book


def _broker(book=None):
    b = KiteBroker(kite=_Api(book))
    b._execution_lease = None          # lease is tested separately
    b._min_call_interval = 0.0
    return b


def _row(**over):
    row = {
        "tradingsymbol": "NIFTY04AUG2624350CE", "transaction_type": "BUY",
        "quantity": 65, "status": "OPEN", "order_id": "OID-1",
        "order_type": "LIMIT",
    }
    row.update(over)
    return row


# ── the scan itself ──────────────────────────────────────────────────────────

def test_absent_only_when_the_book_was_actually_read():
    r = _broker([]).inspect_recent_entry_order(
        "NIFTY04AUG2624350CE", side="BUY", quantity=65
    )
    assert r["state"] == "ABSENT"


def test_live_order_is_found_and_its_id_returned():
    r = _broker([_row()]).inspect_recent_entry_order(
        "NIFTY04AUG2624350CE", side="BUY", quantity=65
    )
    assert r["state"] == "FOUND" and r["order_id"] == "OID-1"


@pytest.mark.parametrize("book", [
    NetworkException("network down"),
    RuntimeError("unclassified failure"),
    "not-a-list",
    None,
])
def test_an_unreadable_book_is_UNKNOWN_never_absent(book):
    """The safety-critical direction: never infer 'not placed' from a failed scan."""
    r = _broker(book).inspect_recent_entry_order("NIFTY04AUG2624350CE", side="BUY", quantity=65)
    assert r["state"] == "UNKNOWN", "a scan we could not complete must not read as ABSENT"


def test_terminal_and_stop_rows_are_not_mistaken_for_a_live_entry():
    for over in ({"status": "REJECTED"}, {"status": "CANCELLED"},
                 {"order_type": "SL-M"}, {"order_type": "SL"},
                 {"transaction_type": "SELL"}):
        r = _broker([_row(**over)]).inspect_recent_entry_order(
            "NIFTY04AUG2624350CE", side="BUY", quantity=65
        )
        assert r["state"] == "ABSENT", f"{over} should not count as a live entry"


def test_a_differently_sized_order_is_not_adopted_as_this_entry():
    """Adopting a stranger's order books this trade against someone else's position.

    Two trades on one strike is the ambiguity Kite tags exist to solve. Until the scan
    matches on tag, a CONFIDENT quantity mismatch must still disqualify the row —
    otherwise a lost ack on a 65-lot entry adopts a resting 130-lot order, the stop is
    placed for 65, and 65 lots sit permanently naked.
    """
    r = _broker([_row(quantity=130, order_id="OID-OTHER")]).inspect_recent_entry_order(
        "NIFTY04AUG2624350CE", side="BUY", quantity=65
    )
    assert r["state"] != "FOUND", "adopted a resting order of a different size"
    assert r["order_id"] != "OID-OTHER"


def test_an_unreadable_quantity_does_not_manufacture_ABSENT():
    """The unsafe direction of the same filter.

    If the row's size cannot be parsed we do NOT know it is a different order, and
    ABSENT would tell place_order the entry definitively failed — which re-places a
    possibly-live order. Unknown size must stay adoptable.
    """
    for bad in (None, "", "abc"):
        r = _broker([_row(quantity=bad)]).inspect_recent_entry_order(
            "NIFTY04AUG2624350CE", side="BUY", quantity=65
        )
        assert r["state"] == "FOUND", f"quantity={bad!r} wrongly read as a clean failure"


def test_an_unidentifiable_contract_is_UNKNOWN_not_absent():
    """An empty symbol matches no row, and "matched nothing" must not read as ABSENT."""
    r = _broker([_row()]).inspect_recent_entry_order("", side="BUY", quantity=65)
    assert r["state"] == "UNKNOWN"


def test_base_broker_defaults_to_unknown():
    """A broker with no order-book support must never claim ABSENT."""
    from trading.broker import Broker

    assert Broker().inspect_recent_entry_order("X")["state"] == "UNKNOWN"


# ── how place_order uses it ──────────────────────────────────────────────────

def test_place_order_wiring_is_failsafe():
    import inspect

    # The submit body lives in _submit_entry; place_order wraps it so a failure between
    # taking an entry-rate slot and submitting gives the slot back.
    src = inspect.getsource(KiteBroker._submit_entry)
    body = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    assert "inspect_recent_entry_order" in body, "no order-book check after a missing id"
    # ABSENT -> clean, non-terminal rejection; anything else must still be able to raise
    assert '"ABSENT"' in body and "BrokerOrderRejected" in body
    assert "BrokerOrderAmbiguous" in body, "genuine ambiguity must still raise"

    wrapper = "\n".join(
        l.split("#", 1)[0] for l in inspect.getsource(KiteBroker.place_order).splitlines()
    )
    assert "_entry_rate_allowed" in wrapper, "entries are no longer rate-gated"
    assert "_release_entry_reservation" in wrapper, "a failed submit keeps its slot"


def test_a_missing_order_id_with_an_unreadable_book_stays_ambiguous():
    """Behavioural counterpart to the wiring check above.

    An entry that returns no id, when the order book cannot be read either, is the
    2026-07-31 case exactly. It must raise rather than resolve either way.
    """
    class _Silent:
        def place_order(self, **_kw):
            return None

        def orders(self):
            raise NetworkException("order book unreadable")

    b = KiteBroker(kite=_Silent())
    b._execution_lease = None
    b._min_call_interval = 0.0
    with pytest.raises(BrokerOrderAmbiguous):
        b.place_order("NIFTY04AUG2624350CE", "60620", 65, "BUY")


def test_sdk_logs_are_captured_into_the_application_log():
    """0 SDK lines had ever reached ut1.log; the reason was always thrown away."""
    import inspect
    from trading import secure_logging
    src = inspect.getsource(secure_logging)
    assert "_SdkToLoguruHandler" in src
    assert "addHandler" in src
    inst = inspect.getsource(secure_logging.install_smartapi_log_redaction)
    assert "addHandler" in inst, "the capture handler is never installed"
