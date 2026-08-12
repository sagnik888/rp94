"""Slippage controls: entry price policy, tick alignment, and the realized-slippage
tracker that auto-calibrates the charge model from real fills.

The AngelOne marketable-limit subsystem this file used to cover — an LTP-anchored
percentage buffer, a futures bps buffer, and touch-quote (bid/ask) anchoring with a
shadow/strict mode — was deliberately retired in the Kite migration (commit e6778f3),
not lost. On 2026-07-31 every entry went out as a blind LTP mark-up limit against an
empty book and none filled. The replacement policy: take the market unless the signal
is strong enough that missing the fill beats paying the spread, and cap the damage with
Zerodha's market-protection band, which AngelOne had no equivalent for.

So the buffer/touch-quote tests are gone with the code they described rather than being
ported to a broker that deliberately does not behave that way. What survives is the part
that was never about AngelOne: a limit price must land on the exchange's tick grid, and a
market order must carry a slippage cap.
"""
import threading
from datetime import datetime

import pytest

from config.settings import get_settings
from engine.slippage_tracker import SlippageTracker
from engine.trade_accounting import estimate_trade_charges
from trading.kite_broker import KiteBroker
from trading.trade_manager import Trade, TradeManager


class _CaptureApi:
    def __init__(self):
        self.params = {}

    def place_order(self, **params):
        self.params.update(params)
        return "X1"

    def orders(self):
        return []


def _broker():
    api = _CaptureApi()
    broker = KiteBroker(kite=api)
    broker._min_call_interval = 0.0
    return broker, api


# ── Entry price policy ───────────────────────────────────────────────

def test_high_conviction_entry_uses_a_limit():
    """A limit risks missing the fill; only a strong signal earns that trade-off."""
    broker, api = _broker()
    # The NORMALISED floor. The raw setting may be on either scale (95.0 or 0.95);
    # the broker normalises it, so a test deriving a confidence from the raw value
    # compares two different scales.
    threshold = KiteBroker._entry_limit_confidence_threshold()
    broker.place_order("NIFTY25JUL25000CE", "111", 75, "BUY",
                       price=150.0, confidence=threshold)
    assert api.params["order_type"] == "LIMIT"
    assert float(api.params["price"]) == pytest.approx(150.0)


def test_ordinary_conviction_takes_the_market_with_a_protection_band():
    """The 2026-07-31 failure: blind mark-up limits against an empty book, no fills."""
    broker, api = _broker()
    # The NORMALISED floor. The raw setting may be on either scale (95.0 or 0.95);
    # the broker normalises it, so a test deriving a confidence from the raw value
    # compares two different scales.
    threshold = KiteBroker._entry_limit_confidence_threshold()
    broker.place_order("NIFTY25JUL25000CE", "111", 75, "BUY",
                       price=150.0, confidence=max(0.0, threshold - 0.10))
    assert api.params["order_type"] == "MARKET"
    assert "price" not in api.params
    # The slippage control that replaced the buffer: Zerodha caps how far from the
    # touch a market order may fill. Without it these fills are unbounded.
    assert api.params["market_protection"] == -1


def test_no_reference_price_falls_back_to_market():
    broker, api = _broker()
    broker.place_order("NIFTY25JULFUT", "222", 65, "BUY", price=0.0, confidence=100.0)
    assert api.params["order_type"] == "MARKET"


def test_limit_price_is_tick_aligned():
    """The exchange rejects a non-tick price outright, whoever the broker is."""
    broker, api = _broker()
    broker.place_order("NIFTY25JUL25000CE", "111", 75, "BUY",
                       price=100.13, confidence=100.0)
    limit = float(api.params["price"])
    assert round(limit * 100) % 5 == 0


def test_protective_stop_trigger_is_tick_aligned():
    broker, api = _broker()
    broker.place_protective_stop("NIFTY25JUL25000CE", "111", 75, "SELL", 95.03)
    assert float(api.params["trigger_price"]) == pytest.approx(95.05)


def test_exit_is_slippage_capped():
    broker, api = _broker()
    broker.close_order("NIFTY25JUL25000CE", "111", 75, "SELL")
    assert api.params["order_type"] == "MARKET"
    assert api.params["market_protection"] == -1


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

    def calibrated_fraction(self, inst_type, instrument="", min_samples=5,
                            max_age_days=None):
        # max_age_days accepted since 2026-08-11. _calibrated_leg_fraction swallows every
        # exception and falls back to statics, so a stub that rejects a new kwarg fails
        # as a silent wrong NUMBER rather than a TypeError.
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
        # Broker order ids since 2026-08-11: the EWMA takes real fills only, after
        # 12,051 simulated entries pinned measured futures slippage to zero. These tests
        # assert the SIGN convention, which is unchanged, so they describe real fills.
        broker_order_id="251108000123", exit_order_id="251108000999",
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
