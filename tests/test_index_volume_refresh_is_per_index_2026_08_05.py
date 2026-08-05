"""All four indices must be able to refresh their cumulative volume, not just one.

Kite sends no volume for an index, so a Kite index tick carries the last known cumulative
total instead. When that total is zero the tick records volume_available=false, which
starves the entry confidence score and kills the exit volume test.

The refresh was serialised on ONE global lock while all four indices tick within the same
instant. The first to arrive won; the other three failed a non-blocking acquire — having
ALREADY stamped their 15s throttle — so they would not ask again for 15 seconds, lose the
same race, and repeat. Measured with a realistic REST latency this left exactly 1 of 4
indices ever refreshing, and the other three carried zero all session.

Two things fix it and both are asserted here: the in-flight guard is PER INDEX, and the
throttle is stamped only once a dispatch is certain.
"""
import threading
import time

import pytest

from data.market_data import MarketDataProvider

NAMES = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")


def _provider(latency=0.0, source="kite_index_futures", volume=2_326_415.0):
    p = MarketDataProvider.__new__(MarketDataProvider)
    p._index_cum_volume = {}
    p._index_cum_volume_source = {}
    p._index_cum_volume_at = {}
    p._index_futures_volume_at = {}
    p._index_volume_refresh_guard = threading.Lock()
    p._index_volume_refresh_inflight = set()
    p.served = []

    def activity(name):
        time.sleep(latency)
        p.served.append(name)
        return {"source": source, "volume": volume}

    p.get_index_activity = activity
    return p


def _tick_all(provider, at=None):
    at = at or time.time()
    for name in NAMES:
        provider._index_volume_for_tick(name, at)


@pytest.mark.parametrize("latency", [0.0, 0.2, 0.5])
def test_every_index_refreshes_even_when_they_tick_together(latency):
    """The exact live condition: four indices, one instant, a slow REST call."""
    provider = _provider(latency=latency)
    _tick_all(provider)
    time.sleep(latency + 0.5)
    refreshed = [n for n in NAMES if provider._index_cum_volume.get(n)]
    assert sorted(refreshed) == sorted(NAMES), (
        f"only {len(refreshed)}/4 indices refreshed at {latency}s latency: {refreshed}"
    )


def test_volume_available_becomes_true_for_all_four():
    """This is the observable the dashboard reports and the scorers consume."""
    provider = _provider(latency=0.2)
    _tick_all(provider)
    time.sleep(0.6)
    carried = {n: provider._index_volume_for_tick(n, time.time())[0] for n in NAMES}
    unavailable = [n for n, v in carried.items() if not v > 0]
    assert unavailable == [], f"volume_available would still be false for {unavailable}"


def test_a_losing_index_does_not_burn_its_throttle():
    """The core defect: stamping before dispatch turned a lost race into a dead feed."""
    provider = _provider(latency=0.5)
    now = time.time()
    provider._index_volume_for_tick("NIFTY", now)
    # Same instant, same index again — this one must NOT dispatch, but must also leave
    # the door open rather than silently consuming the window.
    provider._index_volume_for_tick("NIFTY", now)
    assert provider._index_futures_volume_at["NIFTY"] == pytest.approx(now)
    time.sleep(0.8)
    assert provider._index_cum_volume.get("NIFTY")
    assert provider.served.count("NIFTY") == 1, "dispatched twice for one index"


def test_an_index_is_not_refreshed_twice_concurrently():
    provider = _provider(latency=0.4)
    now = time.time()
    for _ in range(5):
        provider._index_volume_for_tick("SENSEX", now)
    time.sleep(0.8)
    assert provider.served.count("SENSEX") == 1


def test_the_throttle_still_applies_between_refreshes():
    """It must not become a free-for-all in the other direction."""
    provider = _provider(latency=0.0)
    now = time.time()
    provider._index_volume_for_tick("NIFTY", now)
    time.sleep(0.3)
    provider._index_volume_for_tick("NIFTY", now + 1.0)     # well inside the 15s window
    time.sleep(0.3)
    assert provider.served.count("NIFTY") == 1, "throttle no longer limits refreshes"


def test_an_unusable_source_is_not_stored():
    """A 'none' source must not be mistaken for real volume."""
    provider = _provider(latency=0.0, source="none", volume=0.0)
    _tick_all(provider)
    time.sleep(0.4)
    assert all(not provider._index_cum_volume.get(n) for n in NAMES)


def test_a_failing_refresh_releases_its_slot():
    """Otherwise one exception permanently blocks that index from ever refreshing."""
    provider = _provider(latency=0.0)

    def boom(name):
        raise RuntimeError("fyers down")

    provider.get_index_activity = boom
    provider._index_volume_for_tick("NIFTY", time.time())
    time.sleep(0.4)
    assert "NIFTY" not in provider._index_volume_refresh_inflight, (
        "a failed refresh left the index marked in-flight forever"
    )
