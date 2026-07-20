"""
Trading-logic audit fixes: C2 (per-strike delta), H1 (slippage), H2 (marketable limit).
"""

from types import SimpleNamespace

import pytest

from engine.signal_processor import SignalProcessor
from engine.trade_accounting import estimate_trade_charges
from config.settings import get_settings


# ── C2: per-strike Black-Scholes delta (not flat ATM 0.5) ───────────
def test_per_strike_delta_is_moneyness_aware():
    intel = {"greeks": {"call": {"iv": 15.0}, "put": {"iv": 15.0}}}
    f = SignalProcessor._option_signed_delta
    atm = f(24000, 24000, "CE", intel, 3, 0.5)
    itm = f(24000, 23500, "CE", intel, 3, 0.5)
    otm = f(24000, 24500, "CE", intel, 3, 0.5)
    assert 0.4 < atm < 0.6           # ATM ~0.5
    assert itm > atm                 # ITM call delta higher (was flat 0.5 before)
    assert otm < atm                 # OTM lower
    # Puts are signed negative; ITM put more negative.
    p_atm = f(24000, 24000, "PE", intel, 3, -0.5)
    p_itm = f(24000, 24500, "PE", intel, 3, -0.5)
    assert p_atm < 0 and p_itm < p_atm


def test_per_strike_delta_falls_back_on_error():
    # Bad spot -> exception path -> fallback returned.
    assert SignalProcessor._option_signed_delta("x", "y", "CE", {}, 3, 0.5) == 0.5


# ── H1: modeled slippage in charges (sim/backtest realism) ──────────
def test_charges_include_option_slippage():
    s = get_settings()
    s.opt_cost = 80.0
    s.slippage_opt_pct = 0.30
    s.slippage_auto_calibration_enabled = False  # pin statics: live fills must not flip this test
    charge = estimate_trade_charges(140.0, 150.0, 75, "OPT", s, 0.5)
    # base 80 + (140+150)*75*0.003 = 80 + 65.25
    assert charge == pytest.approx(145.25, abs=0.5)
    assert charge > 80.0


def test_charges_include_futures_slippage():
    s = get_settings()
    s.fut_cost = 200.0
    s.slippage_fut_bps = 2.0
    s.slippage_auto_calibration_enabled = False
    charge = estimate_trade_charges(24000.0, 24100.0, 65, "FUT", s, 1.0)
    # base 200 + (24000+24100)*65*0.0002 = 200 + 625.3
    assert charge == pytest.approx(825.3, abs=1.0)
    assert charge > 200.0


def test_zero_slippage_setting_returns_flat_base():
    s = get_settings()
    s.opt_cost = 80.0
    s.slippage_opt_pct = 0.0
    s.slippage_auto_calibration_enabled = False
    assert estimate_trade_charges(140.0, 150.0, 75, "OPT", s, 0.5) == 80.0


# ── H2: marketable-limit buffer on option entries ───────────────────
def test_option_entry_uses_marketable_limit():
    from trading.broker import SmartApiBroker
    get_settings().option_limit_buffer_pct = 1.0
    captured = {}

    class _Api:
        def placeOrder(self, params):
            captured.update(params)
            return {"data": {"orderid": "X1"}}

    broker = SmartApiBroker(_Api())
    broker.place_order("NIFTY25JUN24000CE", "111", 75, "BUY", price=100.0)
    # BUY limit placed 1% above the signal price so it actually fills.
    assert captured["ordertype"] == "LIMIT"
    assert float(captured["price"]) == pytest.approx(101.0)
