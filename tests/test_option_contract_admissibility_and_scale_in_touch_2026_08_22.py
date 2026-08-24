"""Post-selection option liquidity and scale-in execution safety."""

from types import SimpleNamespace

import pandas as pd

import config.settings as cs
from data.market_data import MarketDataProvider
from engine.signal_processor import SignalProcessor
from trading.trade_manager import TradeManager


def _settings(**overrides):
    values = dict(
        option_contract_liquidity_gate_enabled=True,
        option_contract_min_oi_lots=5.0,
        option_contract_min_volume_lots=1.0,
        option_contract_min_touch_depth_lots=1.0,
        execution_touch_quote_max_age_seconds=1.5,
        option_touch_quote_max_spread_pct=1.0,
        option_touch_quote_max_spread_ticks=40,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _trade():
    return SimpleNamespace(
        id="T1",
        trading_symbol="NIFTY25000CE",
        symbol_token="123",
        inst_type="OPT",
        direction="LONG",
        lot_size=65,
        quantity=65,
        broker_quantity=65,
        scale_in_order_id="",
        scale_in_requested_quantity=0,
    )


def _touch(**overrides):
    quote = dict(
        bid=99.5,
        ask=100.0,
        age_seconds=0.1,
        usable=True,
        reason="ok",
        source="kite_rest_touch",
        bid_qty=65,
        ask_qty=65,
        bid_qty_available=True,
        ask_qty_available=True,
        oi=1_000,
        volume=500,
        oi_available=True,
        volume_available=True,
    )
    quote.update(overrides)
    return quote


def _scale_manager(quote, *, accepts_touch=True):
    tm = TradeManager.__new__(TradeManager)
    calls = {"orders": [], "preflight": [], "booked": []}
    tm._lock = __import__("threading").RLock()

    if accepts_touch:
        def place_order(symbol, token, qty, side, product_type, price=0.0,
                        touch_quote=None, trade_id="", confidence=0.0, **kwargs):
            calls["orders"].append(
                dict(price=price, touch_quote=touch_quote, confidence=confidence)
            )
            return "ADD-1"
    else:
        def place_order(symbol, token, qty, side, product_type, price=0.0):
            calls["orders"].append(dict(price=price))
            return "ADD-1"

    tm.broker = SimpleNamespace(place_order=place_order)
    tm.scanner = SimpleNamespace(
        data=SimpleNamespace(get_contract_touch_quote=lambda *a, **k: quote)
    )
    tm.product_type = "CARRYFORWARD"
    tm._tag_kwargs = lambda *a, **k: {}
    tm._entry_margin_verdict = lambda *a: calls["preflight"].append(a) or None
    tm._confirmed_scaled_quantity = lambda *a, **k: 130
    tm._order_filled_quantity = lambda *a, **k: 65
    tm._book_scale_in = lambda t, lots, price, reason, **k: calls["booked"].append(price)
    tm._mark_dirty = lambda: None
    tm.ensure_broker_protection = lambda *a, **k: True
    tm._release_margin_reservations = lambda *a, **k: None
    tm.calls = calls
    return tm


def test_measured_zero_liquidity_is_distinct_from_missing_chain():
    measured = pd.DataFrame([
        {"strike": 25000.0, "call_oi": 0, "call_volume": 0}
    ])

    zero = SignalProcessor._strike_liquidity_snapshot(measured, 25000.0, "CE")
    missing = SignalProcessor._strike_liquidity_snapshot(None, 25000.0, "CE")

    assert zero["available"] is True and zero["oi"] == zero["volume"] == 0
    assert missing["available"] is False


def test_absolute_gate_rejects_measured_thin_contract_and_depth(monkeypatch):
    monkeypatch.setattr(cs, "get_settings", lambda: _settings())
    tm = TradeManager.__new__(TradeManager)

    ok, reason = tm._option_contract_liquidity_admission(
        inst_type="OPT",
        lot_size=65,
        trading_symbol="NIFTY25000CE",
        touch_quote=_touch(oi=100, volume=500),
    )
    assert ok is False and "open interest" in reason

    ok, reason = tm._option_contract_liquidity_admission(
        inst_type="OPT",
        lot_size=65,
        trading_symbol="NIFTY25000CE",
        touch_quote=_touch(ask_qty=10),
    )
    assert ok is False and "ask depth" in reason


def test_absolute_gate_does_not_invent_zero_when_metric_is_absent(monkeypatch):
    monkeypatch.setattr(cs, "get_settings", lambda: _settings())
    tm = TradeManager.__new__(TradeManager)

    assert tm._option_contract_liquidity_admission(
        inst_type="OPT",
        lot_size=65,
        touch_quote={"bid": 99.5, "ask": 100.0, "usable": True},
    ) == (True, "")


def test_option_scale_in_without_executable_book_never_reaches_margin_or_broker(monkeypatch):
    monkeypatch.setattr(cs, "get_settings", lambda: _settings())
    tm = _scale_manager({"usable": False, "reason": "unavailable"})

    tm._run_scale_in(_trade(), 1, 65, 95.0, "CASCADE_ADD")

    assert tm.calls["preflight"] == []
    assert tm.calls["orders"] == []


def test_option_scale_in_sends_fresh_touch_and_nonzero_ask_price(monkeypatch):
    monkeypatch.setattr(cs, "get_settings", lambda: _settings())
    quote = _touch()
    tm = _scale_manager(quote)

    tm._run_scale_in(_trade(), 1, 65, 95.0, "CASCADE_ADD")

    assert tm.calls["preflight"][0][3] == 100.0
    assert tm.calls["orders"] == [
        {"price": 100.0, "touch_quote": quote, "confidence": 1.0}
    ]
    assert tm.calls["booked"] == [100.0]


def test_option_scale_in_refuses_broker_that_cannot_accept_touch(monkeypatch):
    monkeypatch.setattr(cs, "get_settings", lambda: _settings())
    tm = _scale_manager(_touch(), accepts_touch=False)

    tm._run_scale_in(_trade(), 1, 65, 95.0, "CASCADE_ADD")

    assert tm.calls["preflight"] == []
    assert tm.calls["orders"] == []


def test_nested_depth_quantities_survive_touch_normalisation():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    quote = provider._touch_quote_from_payload(
        {
            "time": 100.0,
            "depth": {
                "buy": [{"price": 99.5, "quantity": 40}, {"price": 99.0, "quantity": 25}],
                "sell": [{"price": 100.0, "quantity": 80}],
            },
        },
        "ws_touch",
        now_ts=100.1,
        max_age_seconds=1.5,
    )

    assert quote["usable"] is True
    assert quote["bid_qty"] == 65 and quote["ask_qty"] == 80
    assert quote["depth_available"] is True


def test_execution_broker_kite_book_wins_before_data_broker_fallback():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._touch_quote_cache = {}
    provider._ws_cache = {}
    calls = {"kite": 0, "fyers": 0}

    def kite_depth(*args):
        calls["kite"] += 1
        return _touch()

    def fyers_quotes(*args):
        calls["fyers"] += 1
        raise AssertionError("Fyers must not replace a current Kite execution book")

    provider.kite = SimpleNamespace(is_connected=True, get_depth=kite_depth)
    provider.fyers = SimpleNamespace(quotes=fyers_quotes)

    quote = provider.get_contract_touch_quote("NFO", "NIFTY25000CE", "123")

    assert quote["usable"] is True and quote["source"] == "kite_rest_touch"
    assert calls == {"kite": 1, "fyers": 0}
