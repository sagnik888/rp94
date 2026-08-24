from datetime import datetime

import pytest

from config.settings import get_settings
from trading.kite_broker import KiteBroker
from trading.trade_manager import TradeManager


def _touch(bid=100.0, ask=100.5, *, usable=True, reason="ok"):
    return {
        "bid": bid,
        "ask": ask,
        "usable": usable,
        "reason": reason,
        "source": "kite_ws",
        "age_seconds": 0.2,
    }


def _admit(quote, **overrides):
    kwargs = {
        "inst_type": "OPT",
        "price": 100.25,
        "signal_time": datetime(2026, 8, 21, 10, 0),
        "entry_time": datetime(2026, 8, 21, 10, 0, 1),
        "touch_quote": quote,
        "trading_symbol": "NIFTY2682725000CE",
        "is_recovery": False,
        "require_usable_touch": True,
    }
    kwargs.update(overrides)
    return TradeManager._entry_mark_admission(TradeManager.__new__(TradeManager), **kwargs)


def test_real_auto_option_entry_refuses_a_missing_or_stale_book():
    for quote in ({}, _touch(usable=False, reason="stale")):
        ok, reason = _admit(quote)
        assert ok is False
        assert "executable option book" in reason


def test_real_auto_option_entry_refuses_a_wide_book(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "option_touch_quote_max_spread_pct", 1.0)
    monkeypatch.setattr(settings, "option_touch_quote_max_spread_ticks", 40)
    ok, reason = _admit(_touch(98.0, 102.0))
    assert ok is False
    assert "spread" in reason


def test_historical_and_manual_compatibility_can_leave_touch_non_strict():
    ok, reason = _admit({}, require_usable_touch=False)
    assert ok is True, reason


class _Api:
    def __init__(self):
        self.sent = None

    def place_order(self, **params):
        self.sent = params
        return "OID-TOUCH"


def test_fresh_option_touch_becomes_a_bounded_marketable_limit(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "execution_use_touch_quote", True)
    monkeypatch.setattr(settings, "execution_touch_quote_strict", True)
    monkeypatch.setattr(settings, "option_touch_limit_extra_ticks", 1)
    monkeypatch.setattr(settings, "option_touch_quote_max_spread_pct", 1.0)
    monkeypatch.setattr(settings, "option_touch_quote_max_spread_ticks", 40)
    api = _Api()
    broker = KiteBroker(kite=api)
    broker._min_call_interval = 0.0

    result = broker.place_order(
        "NIFTY2682725000CE",
        "123",
        65,
        "BUY",
        price=99.0,
        confidence=0.1,
        touch_quote=_touch(100.0, 100.5),
    )

    assert result == "OID-TOUCH"
    assert api.sent["order_type"] == "LIMIT"
    assert api.sent["price"] == pytest.approx(100.55)
    assert "market_protection" not in api.sent


def test_touch_pricing_never_uses_a_wide_option_book(monkeypatch):
    from trading.order_retry import BrokerOrderRejected

    settings = get_settings()
    monkeypatch.setattr(settings, "execution_touch_quote_strict", True)
    monkeypatch.setattr(settings, "option_touch_quote_max_spread_pct", 1.0)
    api = _Api()
    broker = KiteBroker(kite=api)
    broker._min_call_interval = 0.0

    with pytest.raises(BrokerOrderRejected):
        broker.place_order(
            "NIFTY2682725000CE",
            "123",
            65,
            "BUY",
            price=100.0,
            touch_quote=_touch(98.0, 102.0),
        )
    assert api.sent is None
