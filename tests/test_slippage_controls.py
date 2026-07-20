"""Slippage controls: marketable-limit caps on entries, tick alignment, and the
realized-slippage tracker that auto-calibrates the charge model from real fills."""
import threading
from datetime import datetime

import pytest

from config.settings import get_settings
from engine.slippage_tracker import SlippageTracker
from engine.trade_accounting import estimate_trade_charges
from trading.broker import SmartApiBroker
from trading.trade_manager import Trade, TradeManager


class _CaptureApi:
    def __init__(self):
        self.params = {}

    def placeOrder(self, params):
        self.params.update(params)
        return {"data": {"orderid": "X1"}}


def _broker():
    api = _CaptureApi()
    return SmartApiBroker(api), api


# ── Marketable-limit entry caps ──────────────────────────────────────

def test_option_buy_limit_uses_pct_buffer_tick_aligned(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "option_limit_buffer_pct", 0.40)
    monkeypatch.setattr(settings, "option_limit_buffer_min_ticks", 2)
    broker, api = _broker()
    broker.place_order("NIFTY25JUL25000CE", "111", 75, "BUY", price=150.0)
    assert api.params["ordertype"] == "LIMIT"
    # 150 * 0.40% = 0.60 above signal, already tick-aligned.
    assert float(api.params["price"]) == pytest.approx(150.60)


def test_cheap_option_gets_tick_floor_not_pct(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "option_limit_buffer_pct", 0.40)
    monkeypatch.setattr(settings, "option_limit_buffer_min_ticks", 2)
    broker, api = _broker()
    broker.place_order("MIDCPNIFTY25JUL12000CE", "111", 140, "BUY", price=3.0)
    # 0.40% of 3.0 is 0.012 — the 2-tick floor (0.10) keeps it fillable.
    assert float(api.params["price"]) == pytest.approx(3.10)


def test_option_sell_limit_buffers_downward(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "option_limit_buffer_pct", 0.40)
    monkeypatch.setattr(settings, "option_limit_buffer_min_ticks", 2)
    broker, api = _broker()
    broker.place_order("NIFTY25JUL25000PE", "111", 75, "SELL", price=150.0)
    assert float(api.params["price"]) == pytest.approx(149.40)


def test_option_limit_price_is_tick_aligned(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "option_limit_buffer_pct", 0.40)
    monkeypatch.setattr(settings, "option_limit_buffer_min_ticks", 2)
    broker, api = _broker()
    broker.place_order("NIFTY25JUL25000CE", "111", 75, "BUY", price=100.13)
    limit = float(api.params["price"])
    # Exchange rejects non-tick prices; must land on a 0.05 grid.
    assert round(limit * 100) % 5 == 0


def test_futures_entry_gets_protection_limit(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "fut_entry_protection_limit_enabled", True)
    monkeypatch.setattr(settings, "fut_limit_buffer_bps", 5.0)
    broker, api = _broker()
    broker.place_order("NIFTY25JULFUT", "222", 65, "BUY", price=25000.0)
    assert api.params["ordertype"] == "LIMIT"
    # 25000 * 5bps = 12.50 above signal — hard cap instead of a naked MARKET order.
    assert float(api.params["price"]) == pytest.approx(25012.50)


def test_futures_protection_toggle_off_restores_market(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "fut_entry_protection_limit_enabled", False)
    broker, api = _broker()
    broker.place_order("NIFTY25JULFUT", "222", 65, "BUY", price=25000.0)
    assert api.params["ordertype"] == "MARKET"
    assert "price" not in api.params


def test_no_reference_price_falls_back_to_market():
    broker, api = _broker()
    broker.place_order("NIFTY25JULFUT", "222", 65, "BUY", price=0.0)
    assert api.params["ordertype"] == "MARKET"


def test_touch_quote_off_shadows_but_keeps_ltp_anchor(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "execution_use_touch_quote", False)
    monkeypatch.setattr(settings, "execution_touch_quote_shadow_enabled", True)
    monkeypatch.setattr(settings, "option_limit_buffer_pct", 0.40)
    monkeypatch.setattr(settings, "option_limit_buffer_min_ticks", 2)
    monkeypatch.setattr(settings, "option_touch_limit_extra_ticks", 1)
    broker, api = _broker()
    broker.place_order(
        "NIFTY25JUL25000CE",
        "111",
        75,
        "BUY",
        price=150.0,
        touch_quote={"bid": 150.15, "ask": 150.20, "age_seconds": 0.2, "usable": True},
    )
    assert float(api.params["price"]) == pytest.approx(150.60)
    assert broker._last_execution_preview["anchor"] == "ltp"
    assert broker._last_execution_preview["touch_limit_price"] == pytest.approx(150.25)


def test_touch_quote_on_uses_ask_plus_tick_for_option_buy(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "execution_use_touch_quote", True)
    monkeypatch.setattr(settings, "execution_touch_quote_strict", False)
    monkeypatch.setattr(settings, "option_touch_limit_extra_ticks", 1)
    broker, api = _broker()
    broker.place_order(
        "NIFTY25JUL25000CE",
        "111",
        75,
        "BUY",
        price=150.0,
        touch_quote={"bid": 150.15, "ask": 150.20, "age_seconds": 0.2, "usable": True},
    )
    assert api.params["ordertype"] == "LIMIT"
    assert float(api.params["price"]) == pytest.approx(150.25)
    assert broker._last_execution_preview["anchor"] == "touch"


def test_touch_quote_on_uses_bid_minus_tick_for_option_sell(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "execution_use_touch_quote", True)
    monkeypatch.setattr(settings, "option_touch_limit_extra_ticks", 1)
    broker, api = _broker()
    broker.place_order(
        "NIFTY25JUL25000PE",
        "111",
        75,
        "SELL",
        price=150.0,
        touch_quote={"bid": 149.80, "ask": 149.85, "age_seconds": 0.2, "usable": True},
    )
    assert float(api.params["price"]) == pytest.approx(149.75)
    assert broker._last_execution_preview["anchor"] == "touch"


def test_touch_quote_on_non_strict_falls_back_to_ltp_anchor(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "execution_use_touch_quote", True)
    monkeypatch.setattr(settings, "execution_touch_quote_strict", False)
    monkeypatch.setattr(settings, "option_limit_buffer_pct", 0.40)
    monkeypatch.setattr(settings, "option_limit_buffer_min_ticks", 2)
    broker, api = _broker()
    order_id = broker.place_order("NIFTY25JUL25000CE", "111", 75, "BUY", price=150.0)
    assert order_id == "X1"
    assert float(api.params["price"]) == pytest.approx(150.60)
    assert broker._last_execution_preview["anchor"] == "ltp"
    assert broker._last_execution_preview["touch_reason"] == "missing"


def test_touch_quote_strict_blocks_missing_touch(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "execution_use_touch_quote", True)
    monkeypatch.setattr(settings, "execution_touch_quote_strict", True)
    broker, api = _broker()
    assert broker.place_order("NIFTY25JUL25000CE", "111", 75, "BUY", price=150.0) is None
    assert api.params == {}


def test_touch_quote_strict_blocks_stale_touch(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "execution_use_touch_quote", True)
    monkeypatch.setattr(settings, "execution_touch_quote_strict", True)
    monkeypatch.setattr(settings, "execution_touch_quote_max_age_seconds", 1.5)
    broker, api = _broker()
    assert broker.place_order(
        "NIFTY25JUL25000CE",
        "111",
        75,
        "BUY",
        price=150.0,
        touch_quote={"bid": 150.15, "ask": 150.20, "age_seconds": 5.0, "usable": True},
    ) is None
    assert api.params == {}


# ── Realized-slippage tracker ────────────────────────────────────────

def test_tracker_ewma_and_min_samples_gate(tmp_path):
    tracker = SlippageTracker(path=str(tmp_path / "slip.json"))
    for _ in range(4):
        tracker.record(inst_type="OPT", instrument="NIFTY", reference_price=100.0, adverse_points=0.20)
    assert tracker.calibrated_fraction("OPT", "NIFTY", min_samples=5) is None
    tracker.record(inst_type="OPT", instrument="NIFTY", reference_price=100.0, adverse_points=0.20)
    measured = tracker.calibrated_fraction("OPT", "NIFTY", min_samples=5)
    # Identical samples -> EWMA equals the sample: 0.20/100 = 0.002
    assert measured == pytest.approx(0.002)


def test_tracker_prefers_per_instrument_over_aggregate(tmp_path):
    tracker = SlippageTracker(path=str(tmp_path / "slip.json"))
    for _ in range(5):
        tracker.record(inst_type="OPT", instrument="NIFTY", reference_price=100.0, adverse_points=0.10)
    for _ in range(5):
        tracker.record(inst_type="OPT", instrument="BANKNIFTY", reference_price=100.0, adverse_points=0.40)
    assert tracker.calibrated_fraction("OPT", "NIFTY", min_samples=5) == pytest.approx(0.001)
    assert tracker.calibrated_fraction("OPT", "BANKNIFTY", min_samples=5) == pytest.approx(0.004)


def test_tracker_rejects_implausible_fills(tmp_path):
    tracker = SlippageTracker(path=str(tmp_path / "slip.json"))
    for _ in range(5):
        tracker.record(inst_type="OPT", instrument="NIFTY", reference_price=100.0, adverse_points=50.0)
    # 50% "slippage" is a data error, never a fill — must not poison the EWMA.
    assert tracker.calibrated_fraction("OPT", "NIFTY", min_samples=1) is None


def test_tracker_persists_across_instances(tmp_path):
    path = str(tmp_path / "slip.json")
    first = SlippageTracker(path=path)
    for _ in range(5):
        first.record(inst_type="FUT", instrument="SENSEX", reference_price=84000.0, adverse_points=8.4)
    reloaded = SlippageTracker(path=path)
    assert reloaded.calibrated_fraction("FUT", "SENSEX", min_samples=5) == pytest.approx(0.0001)


# ── Charge-model calibration ─────────────────────────────────────────

class _StubTracker:
    def __init__(self, frac):
        self._frac = frac

    def calibrated_fraction(self, inst_type, instrument="", min_samples=5):
        return self._frac


def test_charges_use_calibrated_slippage(monkeypatch):
    import engine.slippage_tracker as st
    settings = get_settings()
    monkeypatch.setattr(settings, "slippage_auto_calibration_enabled", True)
    monkeypatch.setattr(settings, "opt_cost", 80.0)
    monkeypatch.setattr(settings, "slippage_opt_pct", 0.30)
    monkeypatch.setattr(st, "get_slippage_tracker", lambda: _StubTracker(0.001))
    charge = estimate_trade_charges(140.0, 150.0, 75, "OPT", settings, 0.5, instrument="NIFTY")
    # measured 0.10% per leg replaces the static 0.30%: 80 + 290*75*0.001 = 101.75
    assert charge == pytest.approx(101.75, abs=0.1)


def test_calibrated_slippage_is_clamped(monkeypatch):
    import engine.slippage_tracker as st
    settings = get_settings()
    monkeypatch.setattr(settings, "slippage_auto_calibration_enabled", True)
    monkeypatch.setattr(settings, "opt_cost", 80.0)
    monkeypatch.setattr(st, "get_slippage_tracker", lambda: _StubTracker(0.05))
    charge = estimate_trade_charges(140.0, 150.0, 75, "OPT", settings, 0.5, instrument="NIFTY")
    # 5% measured is clamped to the 0.60% ceiling: 80 + 290*75*0.006 = 210.5
    assert charge == pytest.approx(210.5, abs=0.1)


def test_calibration_toggle_off_uses_statics(monkeypatch):
    import engine.slippage_tracker as st
    settings = get_settings()
    monkeypatch.setattr(settings, "slippage_auto_calibration_enabled", False)
    monkeypatch.setattr(settings, "opt_cost", 80.0)
    monkeypatch.setattr(settings, "slippage_opt_pct", 0.30)
    monkeypatch.setattr(st, "get_slippage_tracker", lambda: _StubTracker(0.001))
    charge = estimate_trade_charges(140.0, 150.0, 75, "OPT", settings, 0.5, instrument="NIFTY")
    assert charge == pytest.approx(145.25, abs=0.5)


# ── Fill-vs-signal measurement signs ─────────────────────────────────

def _capture_records(monkeypatch):
    records = []

    class _Rec:
        def record(self, **kw):
            records.append(kw)

    import engine.slippage_tracker as st
    monkeypatch.setattr(st, "get_slippage_tracker", lambda: _Rec())
    return records


def _trade(**overrides):
    base = dict(
        id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=100.0, entry_time=datetime(2026, 7, 1, 10, 0),
        trailing_stop=90.0, current_stop=90.0, lots=1, lot_size=65, grade="A",
        inst_type="OPT", signal_entry_price=100.0,
    )
    base.update(overrides)
    return Trade(**base)


def test_entry_slippage_sign_for_option_buy(monkeypatch):
    records = _capture_records(monkeypatch)
    trade = _trade()
    TradeManager._record_entry_slippage(trade, 100.35)
    assert trade.entry_slippage == pytest.approx(0.35)  # paid 0.35 worse
    assert records and records[0]["adverse_points"] == pytest.approx(0.35)


def test_entry_slippage_sign_for_short_futures(monkeypatch):
    records = _capture_records(monkeypatch)
    trade = _trade(inst_type="FUT", direction="SHORT",
                   entry_price=84000.0, signal_entry_price=84000.0)
    TradeManager._record_entry_slippage(trade, 83995.0)
    # SHORT enters by SELLING: a lower fill is worse.
    assert trade.entry_slippage == pytest.approx(5.0)
    assert records and records[0]["adverse_points"] == pytest.approx(5.0)


def test_exit_slippage_sign_for_option_sell_exit(monkeypatch):
    records = _capture_records(monkeypatch)
    trade = _trade()
    TradeManager._record_exit_slippage(trade, 130.0, 129.55)
    # Option exit is a SELL: filling below the decision price is adverse.
    assert trade.exit_slippage == pytest.approx(0.45)
    assert records and records[0]["leg"] == "EXIT"
