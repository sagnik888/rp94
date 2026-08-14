"""How an order filled must survive the save, or it can never be tuned against.

The trades table has carried six columns that nothing wrote — no code anywhere
referenced requested_entry_price, requested_exit_price, fill_latency_ms,
exit_fill_latency_ms, entry_filled_at or exit_filled_at. Schema with no producer:
every stored row read 0/NULL.

That is not cosmetic. entry_price is OVERWRITTEN by the broker fill on
confirmation, and signal_entry_price — the reference slippage is measured
against — was never persisted either. So the instant a row was saved, the price
that had been asked for was gone, and realised slippage could not be recomputed
from stored history. The in-memory EWMA was the only consumer that ever saw it,
and a restart erased the evidence behind it.

This system trades options first, and the options leg has never had a single
calibrated slippage sample: estimate_trade_charges prices every option on the
static slippage_opt_pct. Nothing can tune that number without a stored history of
real fills to tune it against, and now each row carries its own —
entry_price - requested_entry_price IS the entry slippage, re-derivable forever.
"""
from datetime import datetime, timedelta

import pytest
import pytz

from trading.trade_manager import Trade, TradeManager

IST = pytz.timezone("Asia/Kolkata")


def _trade(**kw):
    base = dict(
        id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=100.0, trailing_stop=90.0, current_stop=90.0,
        lots=1, lot_size=65, grade="A", inst_type="OPT",
        entry_time=datetime.now(IST), signal_entry_price=100.0,
        data_provenance="LIVE", broker_order_id="250815000111",
    )
    base.update(kw)
    return Trade(**base)


# ── the entry leg ────────────────────────────────────────────────────────────

def test_the_requested_entry_price_is_captured():
    t = _trade()

    TradeManager._stamp_fill_quality(t, leg="ENTRY", requested=100.0)

    assert t.requested_entry_price == 100.0


def test_entry_slippage_is_rederivable_from_the_stored_row():
    """The point of the whole change: the row alone answers 'how did this fill?'"""
    t = _trade()
    TradeManager._stamp_fill_quality(t, leg="ENTRY", requested=100.0)
    t.entry_price = 100.45  # broker fill overwrites, as it does in the live path

    assert t.entry_price - t.requested_entry_price == pytest.approx(0.45)


def test_the_entry_fill_time_is_recorded():
    t = _trade()

    TradeManager._stamp_fill_quality(t, leg="ENTRY", requested=100.0)

    assert t.entry_filled_at is not None


def test_entry_latency_is_measured_from_the_submit_time():
    t = _trade(entry_submitted_at=datetime.now(IST) - timedelta(milliseconds=800))

    TradeManager._stamp_fill_quality(t, leg="ENTRY", requested=100.0)

    assert 600 <= t.fill_latency_ms <= 3000


def test_latency_stays_zero_when_nothing_was_submitted():
    """Absent on a recovered leg. A fabricated zero would read as an instant fill."""
    t = _trade(entry_submitted_at=None)

    TradeManager._stamp_fill_quality(t, leg="ENTRY", requested=100.0)

    assert t.fill_latency_ms == 0.0
    assert t.requested_entry_price == 100.0, "the price is still captured"


def test_latency_is_never_negative():
    """A submit stamped after the fill (clock skew) must not produce a negative."""
    t = _trade(entry_submitted_at=datetime.now(IST) + timedelta(seconds=5))

    TradeManager._stamp_fill_quality(t, leg="ENTRY", requested=100.0)

    assert t.fill_latency_ms == 0.0


# ── the exit leg ─────────────────────────────────────────────────────────────

def test_the_requested_exit_price_is_captured():
    t = _trade()

    TradeManager._stamp_fill_quality(t, leg="EXIT", requested=118.0)

    assert t.requested_exit_price == 118.0
    assert t.exit_filled_at is not None


def test_the_exit_leg_does_not_touch_the_entry_fields():
    t = _trade()
    TradeManager._stamp_fill_quality(t, leg="ENTRY", requested=100.0)

    TradeManager._stamp_fill_quality(t, leg="EXIT", requested=118.0)

    assert t.requested_entry_price == 100.0


# ── a missing reference is not a measurement ─────────────────────────────────

@pytest.mark.parametrize("bad", [0.0, -1.0, None])
def test_no_reference_price_records_nothing(bad):
    t = _trade()

    TradeManager._stamp_fill_quality(t, leg="ENTRY", requested=bad)

    assert t.requested_entry_price == 0.0
    assert t.entry_filled_at is None


# ── it survives the round trip ───────────────────────────────────────────────

def test_the_fields_round_trip_through_serialisation():
    t = _trade(entry_submitted_at=datetime.now(IST) - timedelta(milliseconds=500))
    TradeManager._stamp_fill_quality(t, leg="ENTRY", requested=100.0)
    TradeManager._stamp_fill_quality(t, leg="EXIT", requested=118.0)

    back = Trade.from_dict(t.to_dict())

    assert back.requested_entry_price == t.requested_entry_price
    assert back.requested_exit_price == t.requested_exit_price
    assert back.fill_latency_ms == t.fill_latency_ms
    assert back.entry_filled_at is not None
    assert back.exit_filled_at is not None


def test_the_columns_are_persisted_and_migrated():
    """Round-tripping is not enough: they were dropped at save before this."""
    import inspect

    import data.sqlite_db as sqlite_db

    src = inspect.getsource(sqlite_db)
    for field in (
        "requested_entry_price", "requested_exit_price",
        "entry_filled_at", "exit_filled_at",
        "fill_latency_ms", "exit_fill_latency_ms",
    ):
        assert f'"{field}"' in src, f"{field} is not in the persisted field list"


def test_both_live_fill_paths_stamp_it():
    """Pinned at source: the entry must capture BEFORE entry_price is overwritten."""
    import inspect

    src = inspect.getsource(TradeManager)

    assert src.count("_stamp_fill_quality(") >= 3, "entry and exit fill paths must call it"
    i = src.index('_stamp_fill_quality(\n')
    after = src[i: i + 500]
    assert "trade.entry_price = fill_price" in after, (
        "the entry stamp must run before the fill overwrites entry_price"
    )
