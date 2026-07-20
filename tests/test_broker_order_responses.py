import pytest

from trading.broker import SmartApiBroker
from trading.order_retry import BrokerOrderAmbiguous


class FakeSmartApi:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def placeOrder(self, params):
        self.calls.append(params)
        return self.response


def test_smartapi_requires_explicit_order_id():
    api = FakeSmartApi({"status": True, "data": {"message": "accepted"}})
    broker = SmartApiBroker(api)

    with pytest.raises(BrokerOrderAmbiguous):
        broker.place_order("NIFTYTESTFUT", "123", 10, "BUY")


def test_smartapi_accepts_nested_order_id():
    api = FakeSmartApi({
        "status": True,
        "data": {"orderid": "240614000001"},
    })
    broker = SmartApiBroker(api)

    assert (
        broker.place_order("NIFTYTESTFUT", "123", 10, "BUY")
        == "240614000001"
    )
