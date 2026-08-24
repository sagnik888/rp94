"""Near-month futures CONTRACTS must never reach an index candle's volume column.

An index candle's volume is Fyers cash-market SHARES: `get_historical_candles` backfills
it every ~60s ("Prioritizing Fyers history for ... to get volume"), and every volume
threshold in the system is calibrated on that quantity. The tick path used to carry the
Kite near-month FUTURES total onto a tick whenever the cash feed went quiet, putting two
quantities ~1000x apart (measured 0.006 for NIFTY, 0.001 for SENSEX) into one column.

Nothing downstream could separate them. `volume_window_is_homogeneous` reads an
unlabelled row as "unknown, not a unit" — and backfilled rows carry no `volume_basis` —
so a window of cash bars with contract bars at its right edge passed as homogeneous.
Measured live on 2026-08-14: forming bars of 30, 60, 120, 180, 240, 455, 510, 720 and 780
sitting against 1m cash bars of 17k (SENSEX) to 1M (NIFTY), with zero
`volume source ->` rebase events all session to mark the boundary.

The dangerous direction is the recovery, not the failover: a long Fyers-history outage
builds a contracts-based 20-bar mean, then one cash bar lands at ~700x, `is_surge` fires
EXTREME and clears the 2.5x gate that is a 1min signal's only route into a live entry.

The futures figure is still resolved — `get_index_activity` reports it for the health
endpoint and the OI read — it is simply never ticked.
"""
import time

import pytest

from data.market_data import MarketDataProvider


class _Provider(MarketDataProvider):
    def __init__(self, activity):
        self._activity = activity
        self._index_cum_volume = {}
        self._index_cum_volume_source = {}
        self._index_cum_volume_at = {}
        self._index_futures_fallback = {}
        self._index_futures_volume_at = {}
        import threading

        self._index_volume_refresh_guard = threading.Lock()
        self._index_volume_refresh_inflight = {"NIFTY"}

    def get_index_activity(self, symbol):
        return dict(self._activity)


FUTURES = {
    "symbol": "NIFTY", "volume": 1_403_480.0, "oi": 12_793_170.0,
    "source": "kite_index_futures", "unit": "contracts",
    "comparable_to_thresholds": False, "confirmation": "fallback_only",
}
CASH = {
    "symbol": "NIFTY", "volume": 409_196_200.0,
    "source": "fyers_index", "unit": "shares",
    "comparable_to_thresholds": True, "confirmation": "broker_confirms",
}


def test_the_futures_fallback_is_not_written_to_the_tick_total():
    p = _Provider(FUTURES)
    p._refresh_index_volume_worker("NIFTY")
    assert "NIFTY" not in p._index_cum_volume, "contracts reached the candle tick path"
    assert p._index_cum_volume_source.get("NIFTY") is None


def test_the_futures_fallback_is_still_recorded_for_telemetry():
    """Not ticked is not discarded — the health endpoint and OI read still need it."""
    p = _Provider(FUTURES)
    p._refresh_index_volume_worker("NIFTY")
    recorded = p._index_futures_fallback["NIFTY"]
    assert recorded["volume"] == 1_403_480.0
    assert recorded["oi"] == 12_793_170.0
    assert recorded["at"] > 0


def test_cash_volume_still_reaches_the_tick_total():
    p = _Provider(CASH)
    p._refresh_index_volume_worker("NIFTY")
    assert p._index_cum_volume["NIFTY"] == 409_196_200.0
    assert p._index_cum_volume_source["NIFTY"] == "fyers_index"


def test_a_cash_recovery_clears_the_fallback_record():
    p = _Provider(FUTURES)
    p._refresh_index_volume_worker("NIFTY")
    assert "NIFTY" in p._index_futures_fallback
    p._activity = CASH
    p._index_futures_volume_at.clear()
    p._refresh_index_volume_worker("NIFTY")
    assert "NIFTY" not in p._index_futures_fallback


def test_a_quiet_cash_feed_carries_the_last_cash_total_not_contracts():
    """The delta simply stops growing: an empty bar, never a bar of the wrong unit."""
    p = _Provider(CASH)
    p._refresh_index_volume_worker("NIFTY")
    p._activity = FUTURES
    p._index_futures_volume_at.clear()
    p._refresh_index_volume_worker("NIFTY")

    carried, source = p._index_volume_for_tick("NIFTY", time.time())
    assert carried == 409_196_200.0, "a contracts total was carried onto a tick"
    assert source == "fyers_index"


def test_a_zero_reading_is_recorded_nowhere():
    p = _Provider({**FUTURES, "volume": 0.0})
    p._refresh_index_volume_worker("NIFTY")
    assert p._index_futures_fallback == {}
    assert p._index_cum_volume == {}


def test_the_refresh_always_releases_its_in_flight_marker():
    """Every early return still has to clear the slot, or the index never refreshes again."""
    for activity in (FUTURES, CASH, {**FUTURES, "volume": 0.0}):
        p = _Provider(activity)
        p._refresh_index_volume_worker("NIFTY")
        assert "NIFTY" not in p._index_volume_refresh_inflight
