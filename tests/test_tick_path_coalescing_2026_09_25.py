"""Tick path CPU at the open (2026-09-25): the forming 1m row was merged into a 2000-row
frame every 0.1s per index (24.6% of a core per index offline). Production now publishes
the forming row at most once a second and re-aggregates the forming 5m/15m bucket at most
every 2s. These tests pin what must NOT degrade with that setting."""
import datetime as dt
import inspect
import re

import pandas as pd

import main
from data.candle_builder import CandleBuilder


def _builder(monkeypatch, clock):
    monkeypatch.setattr("data.candle_builder.time.monotonic", lambda: clock["now"])
    return CandleBuilder(max_candles=2000, partial_publish_min_interval_seconds=1.0,
                         resample_min_interval_seconds=2.0)


def test_the_latest_price_updates_on_every_tick(monkeypatch):
    clock = {"now": 10.0}
    b = _builder(monkeypatch, clock)
    start = dt.datetime(2026, 9, 25, 10, 0, 0)
    for i, price in enumerate((100.0, 100.5, 101.0, 99.5)):
        clock["now"] = 10.0 + i * 0.2
        b.update_from_tick("NIFTY", price, 1000.0 + i, start + dt.timedelta(seconds=i), "fyers_index")
        assert b.get_latest_price("NIFTY") == price           # exits read this, not the row


def test_the_forming_row_publishes_within_a_second_and_keeps_every_tick(monkeypatch):
    clock = {"now": 10.0}
    b = _builder(monkeypatch, clock)
    start = dt.datetime(2026, 9, 25, 10, 0, 0)
    b.update_from_tick("NIFTY", 100.0, 1000.0, start, "fyers_index")
    clock["now"] = 10.4
    b.update_from_tick("NIFTY", 103.0, 1100.0, start + dt.timedelta(seconds=10), "fyers_index")
    clock["now"] = 11.05
    b.update_from_tick("NIFTY", 101.0, 1200.0, start + dt.timedelta(seconds=20), "fyers_index")
    row = b.get_candles("NIFTY", "1min").iloc[-1]
    assert (float(row["high"]), float(row["close"]), int(row["tick_count"])) == (103.0, 101.0, 3)


def test_a_closed_minute_is_published_immediately(monkeypatch):
    clock = {"now": 10.0}
    b = _builder(monkeypatch, clock)
    start = dt.datetime(2026, 9, 25, 10, 0, 0)
    b.update_from_tick("NIFTY", 100.0, 1000.0, start, "fyers_index")
    clock["now"] = 10.1
    b.update_from_tick("NIFTY", 102.0, 1100.0, start + dt.timedelta(seconds=30), "fyers_index")
    clock["now"] = 10.2                                        # well inside the 1s window
    b.update_from_tick("NIFTY", 101.0, 1200.0, start + dt.timedelta(minutes=1), "fyers_index")
    frame = b.get_candles("NIFTY", "1min")
    closed = frame.loc[pd.Timestamp(start)]
    assert float(closed["high"]) == 102.0 and float(closed["close"]) == 102.0
    assert frame.index[-1] == pd.Timestamp(start + dt.timedelta(minutes=1))


def test_production_uses_the_coalesced_tick_settings():
    source = inspect.getsource(main.create_system)
    assert re.search(r"partial_publish_min_interval_seconds=2\.0", source)
    assert re.search(r"resample_min_interval_seconds=3\.0", source)
