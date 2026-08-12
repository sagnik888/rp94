"""The realized-slippage EWMA was measuring a simulation, and then grading it.

data_store/slippage_stats.json on 2026-08-11:

    "FUT":            {"ewma_frac": 0.0, "count": 12051, "last_leg": "ENTRY"}
    "FUT|NIFTY":      {"ewma_frac": 0.0, "count":  7885, "last_leg": "ENTRY"}
    ... and no OPT key at all, in a system that trades options exclusively.

Every row said ENTRY. The exit leg had always required an exit_order_id AND skipped a
fill equal to its reference; the entry leg required neither, so a simulated entry whose
"fill" was simply the signal price recorded a perfect zero — 12,051 times.

That is not a harmless statistic. trade_accounting._calibrated_leg_fraction lets the
measured EWMA REPLACE the static per-leg assumption once min_samples is reached, so the
model was pinned to the 0.2bp floor of _FUT_CALIBRATION_BOUNDS by its own simulation:
a backtest grading its own execution quality.

Two fixes, both asserted here: a sample must come from a real broker fill, and a
measurement must be recent enough to still describe the account that produced it.
"""
from datetime import datetime, timedelta

import pytest

from engine.slippage_tracker import SlippageTracker
from trading.trade_manager import Trade, TradeManager


def _trade(**kw):
    base = dict(
        id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=100.0, trailing_stop=90.0, current_stop=90.0,
        lots=1, lot_size=65, grade="A", inst_type="OPT",
        entry_time=datetime.now(),
    )
    base.update(kw)
    return Trade(**base)


# ── what counts as a real fill ───────────────────────────────────────────────

def test_a_ghost_trade_is_not_a_real_fill():
    t = _trade(is_ghost=True)
    t.broker_order_id = "251108000123"
    assert TradeManager._slippage_sample_is_a_real_fill(t, "ENTRY") is False


def test_an_entry_without_a_broker_order_is_not_a_real_fill():
    """The hole that let 12,051 simulated entries in."""
    t = _trade()
    assert TradeManager._slippage_sample_is_a_real_fill(t, "ENTRY") is False


def test_a_real_broker_entry_is_accepted():
    t = _trade()
    t.broker_order_id = "251108000123"
    assert TradeManager._slippage_sample_is_a_real_fill(t, "ENTRY") is True


def test_the_exit_leg_reads_its_own_order_id():
    """An entry order id must not vouch for the exit leg."""
    t = _trade()
    t.broker_order_id = "251108000123"
    t.exit_order_id = ""
    assert TradeManager._slippage_sample_is_a_real_fill(t, "EXIT") is False
    t.exit_order_id = "251108000999"
    assert TradeManager._slippage_sample_is_a_real_fill(t, "EXIT") is True


@pytest.mark.parametrize("order_id", ["", "   ", None])
def test_a_blank_order_id_is_refused(order_id):
    t = _trade()
    t.broker_order_id = order_id
    assert TradeManager._slippage_sample_is_a_real_fill(t, "ENTRY") is False


# ── the recorders honour it ──────────────────────────────────────────────────

@pytest.fixture
def tracker(tmp_path, monkeypatch):
    tr = SlippageTracker(path=str(tmp_path / "slip.json"))
    import engine.slippage_tracker as st

    monkeypatch.setattr(st, "get_slippage_tracker", lambda: tr)
    return tr


def test_a_simulated_entry_no_longer_reaches_the_ewma(tracker):
    t = _trade()
    t.signal_entry_price = 100.0
    TradeManager._record_entry_slippage(t, 100.0)   # fill == signal, the 12,051 case
    assert tracker.snapshot() == {}


def test_a_real_entry_still_reaches_the_ewma(tracker):
    t = _trade()
    t.signal_entry_price = 100.0
    t.broker_order_id = "251108000123"
    TradeManager._record_entry_slippage(t, 100.4)
    snap = tracker.snapshot()
    assert "OPT" in snap and snap["OPT"]["count"] == 1
    assert snap["OPT"]["ewma_frac"] == pytest.approx(0.004)


def test_a_real_zero_slippage_fill_is_still_a_measurement(tracker):
    """Dropping exact-reference fills would bias the EWMA upward by keeping only the
    adverse ones. Once simulated fills are excluded, a real zero is real information."""
    t = _trade()
    t.signal_entry_price = 100.0
    t.broker_order_id = "251108000123"
    TradeManager._record_entry_slippage(t, 100.0)
    assert tracker.snapshot()["OPT"]["count"] == 1


def test_the_trade_still_reports_its_own_slippage_either_way(tracker):
    """entry_slippage is reporting and stays on every trade; only the SHARED estimate,
    which overrides the cost model, is restricted."""
    t = _trade()
    t.signal_entry_price = 100.0
    TradeManager._record_entry_slippage(t, 100.4)
    assert t.entry_slippage == pytest.approx(0.4)
    assert tracker.snapshot() == {}


def test_a_simulated_exit_no_longer_reaches_the_ewma(tracker):
    t = _trade()
    TradeManager._record_exit_slippage(t, 120.0, 119.5)
    assert tracker.snapshot() == {}
    assert t.exit_slippage == pytest.approx(0.5)


def test_a_real_exit_reaches_the_ewma(tracker):
    t = _trade()
    t.exit_order_id = "251108000999"
    TradeManager._record_exit_slippage(t, 120.0, 119.5)
    assert tracker.snapshot()["OPT"]["count"] == 1


# ── the age bound ────────────────────────────────────────────────────────────

def _aged(tracker, key, days_old, count=12051, frac=0.0):
    tracker._stats[key] = {
        "ewma_frac": frac,
        "count": count,
        "last_leg": "ENTRY",
        "updated_at": (datetime.now() - timedelta(days=days_old)).isoformat(),
    }


def test_a_fossil_measurement_is_ignored(tracker):
    """The exact shape of the FUT rows: a huge sample count, long dead."""
    _aged(tracker, "FUT", days_old=120)
    assert tracker.calibrated_fraction("FUT", max_age_days=30) is None


def test_a_recent_measurement_is_used(tracker):
    _aged(tracker, "FUT", days_old=2, frac=0.0003)
    assert tracker.calibrated_fraction("FUT", max_age_days=30) == pytest.approx(0.0003)


def test_an_undated_row_is_treated_as_stale(tracker):
    """Rows predating updated_at are exactly the legacy ones being excluded, so an
    unreadable stamp must not read as a pass."""
    tracker._stats["FUT"] = {"ewma_frac": 0.0, "count": 12051}
    assert tracker.calibrated_fraction("FUT", max_age_days=30) is None


def test_an_unparseable_stamp_is_treated_as_stale(tracker):
    tracker._stats["FUT"] = {"ewma_frac": 0.0, "count": 99, "updated_at": "not-a-date"}
    assert tracker.calibrated_fraction("FUT", max_age_days=30) is None


def test_the_age_test_can_be_switched_off(tracker):
    _aged(tracker, "FUT", days_old=400, frac=0.0002)
    assert tracker.calibrated_fraction("FUT", max_age_days=0) == pytest.approx(0.0002)
    assert tracker.calibrated_fraction("FUT") == pytest.approx(0.0002)


def test_a_stale_instrument_row_falls_back_to_a_fresh_aggregate(tracker):
    """Per-instrument is preferred, but preference must not resurrect a fossil."""
    _aged(tracker, "FUT", days_old=1, frac=0.0004)
    _aged(tracker, "FUT|NIFTY", days_old=200, frac=0.0)
    assert tracker.calibrated_fraction("FUT", "NIFTY", max_age_days=30) == pytest.approx(0.0004)


def test_the_sample_floor_still_applies(tracker):
    _aged(tracker, "FUT", days_old=1, count=2, frac=0.0004)
    assert tracker.calibrated_fraction("FUT", min_samples=5, max_age_days=30) is None


# ── the consumer passes the bound ────────────────────────────────────────────

def test_trade_accounting_passes_an_age_bound():
    import inspect

    import engine.trade_accounting as ta

    src = inspect.getsource(ta._calibrated_leg_fraction)
    assert "max_age_days=" in src, "the charge model no longer age-bounds the measurement"
    assert "slippage_calibration_max_age_days" in src


def test_the_setting_exists():
    from config.settings import Settings

    assert "slippage_calibration_max_age_days" in Settings.model_fields
