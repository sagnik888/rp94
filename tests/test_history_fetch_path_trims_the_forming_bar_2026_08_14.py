"""The trim has to happen where the fetch lands, not only in a helper.

Drives the real `MarketDataProvider.get_historical_candles` Fyers-index branch with a
stub client that answers the way Fyers did live on 2026-08-14: complete minutes followed
by the minute currently being traded, carrying a fraction of its eventual volume.

The assertion that matters is on the CSV the fetch persists. That file is the candle
store's durable copy, and on 2026-08-14 it held `2026-08-14 14:55:00,...,36075` — a bar
the market was five seconds into — alongside neighbours averaging 349,417.
"""
import time
from pathlib import Path

import pandas as pd
import pytest

from data.market_data import FYERS_INDEX_SYMBOLS, MarketDataProvider


class _StubFyers:
    """Answers `history` the way the live SDK does, including the in-progress bar."""

    def __init__(self, candles):
        self._candles = candles
        self.calls = 0

    def history(self, data=None):
        self.calls += 1
        return {"s": "ok", "candles": self._candles}


def _provider(fyers):
    p = MarketDataProvider.__new__(MarketDataProvider)
    p.fyers = fyers
    p.smart_api = None
    p.is_connected = False
    p._api_hist_cache = {}
    p._last_api_hist_fetch = {}
    p.is_market_open = lambda: True
    return p


def _live_shaped_candles(now):
    """Four closed minutes and the one in progress, with the real 14:51..14:55 volumes."""
    minute = now.floor("min")
    volumes = [465886, 430065, 890948, 446737, 36075]
    candles = []
    for offset, volume in zip(range(4, -1, -1), volumes):
        stamp = minute - pd.Timedelta(minutes=offset)
        epoch = int(stamp.tz_localize("Asia/Kolkata").timestamp())
        candles.append([epoch, 57500.0, 57535.0, 57495.0, 57520.0, volume])
    return candles


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path / "data_store" / "candles" / "BANKNIFTY_1min.csv"


def test_the_in_progress_minute_never_reaches_the_candle_store(store):
    now = pd.Timestamp.now()
    fyers = _StubFyers(_live_shaped_candles(now))
    provider = _provider(fyers)

    provider.get_historical_candles(
        token=None, exchange="NFO", interval="1min",
        days_back=1, instrument_name="BANKNIFTY",
    )

    assert fyers.calls == 1, "the Fyers index branch must actually have run"
    saved = pd.read_csv(store, index_col=0, parse_dates=True)
    assert 36075 not in set(saved["volume"]), "the partial bar was persisted"
    assert saved["volume"].iloc[-1] == 446737
    assert saved.index[-1] == now.floor("min") - pd.Timedelta(minutes=1)


def test_the_complete_minutes_are_all_kept(store):
    now = pd.Timestamp.now()
    provider = _provider(_StubFyers(_live_shaped_candles(now)))

    provider.get_historical_candles(
        token=None, exchange="NFO", interval="1min",
        days_back=1, instrument_name="BANKNIFTY",
    )

    saved = pd.read_csv(store, index_col=0, parse_dates=True)
    assert list(saved["volume"]) == [465886, 430065, 890948, 446737]


def test_repeated_fetches_never_shrink_the_store(store):
    """The trim defers a bar; it must never retract one already banked.

    Every fetch during a live minute carries a forming tail, so the trim runs on all of
    them. If trimming propagated into the merge the store would lose its right edge once
    per fetch — roughly once a minute, all session.
    """
    now = pd.Timestamp.now()
    provider = _provider(_StubFyers(_live_shaped_candles(now)))
    provider.get_historical_candles(
        token=None, exchange="NFO", interval="1min",
        days_back=1, instrument_name="BANKNIFTY",
    )
    first = pd.read_csv(store, index_col=0, parse_dates=True)

    for _ in range(3):
        provider._last_api_hist_fetch.clear()  # let the TTL-gated fetch run again
        provider.fyers = _StubFyers(_live_shaped_candles(pd.Timestamp.now()))
        provider.get_historical_candles(
            token=None, exchange="NFO", interval="1min",
            days_back=1, instrument_name="BANKNIFTY",
        )

    again = pd.read_csv(store, index_col=0, parse_dates=True)
    assert list(again.index) >= list(first.index)
    assert list(again["volume"].tail(4)) == [465886, 430065, 890948, 446737]
