"""An entry is only "placed" when the broker hands back an order id.

Kite returns the id as a bare string, so there is no envelope to misparse — but the
failure mode the AngelOne version guarded against survives the migration: an
acknowledgement that carries no id must never be reported as a placed order, and must
not be reported as a clean failure either unless the order book proves it.
"""
import pytest

from trading.kite_broker import KiteBroker
from trading.order_retry import BrokerOrderAmbiguous, BrokerOrderRejected


class FakeKite:
    def __init__(self, response, order_book=None):
        self.response = response
        self.order_book = order_book if order_book is not None else []
        self.calls = []

    def place_order(self, **params):
        self.calls.append(params)
        return self.response

    def orders(self):
        if isinstance(self.order_book, Exception):
            raise self.order_book
        return self.order_book


def _broker(response, order_book=None):
    b = KiteBroker(kite=FakeKite(response, order_book))
    b._min_call_interval = 0.0
    return b


def test_missing_order_id_with_an_empty_book_is_a_clean_rejection():
    """The book was read and holds nothing, so the placement definitively failed."""
    broker = _broker(None, order_book=[])

    with pytest.raises(BrokerOrderRejected) as excinfo:
        broker.place_order("NIFTYTESTFUT", "123", 10, "BUY")
    # Non-terminal: nothing reached the exchange, so a retry cannot duplicate anything.
    assert excinfo.value.terminal is False


def test_missing_order_id_with_an_unreadable_book_stays_ambiguous():
    broker = _broker(None, order_book=RuntimeError("order book unreadable"))

    with pytest.raises(BrokerOrderAmbiguous):
        broker.place_order("NIFTYTESTFUT", "123", 10, "BUY")


def test_broker_accepts_a_returned_order_id():
    assert (
        _broker("240614000001").place_order("NIFTYTESTFUT", "123", 10, "BUY")
        == "240614000001"
    )


def test_a_lost_ack_adopts_the_live_order_rather_than_placing_a_second():
    """The order reached the exchange; only the acknowledgement was lost."""
    book = [{
        "order_id": "240614000009", "status": "OPEN", "order_type": "MARKET",
        "tradingsymbol": "NIFTYTESTFUT", "transaction_type": "BUY", "quantity": 10,
    }]
    broker = _broker(None, order_book=book)

    assert broker.place_order("NIFTYTESTFUT", "123", 10, "BUY") == "240614000009"
    assert len(broker.api.calls) == 1, "a second order was sent for one entry"
