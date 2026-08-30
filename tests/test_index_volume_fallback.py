"""Index volume: Fyers primary, broker futures as cross-check and as fallback.

Kite publishes no volume for an index — an index is a computed number, not a traded
instrument — so Fyers is the only source of the cash-market volume every threshold in
this system was calibrated against. That leaves a single point of failure with no
cross-check, on a feed that was demonstrably serving fabricated data on 2026-08-01.

The broker's near-month FUTURES fill that hole, but they are a DIFFERENT QUANTITY:
contracts traded in one instrument versus summed share volume across every constituent.
Measured on real data for 2026-07-31 the ratio was 0.006 (NIFTY) and 0.001 (SENSEX) —
not a scale factor. So the fallback must announce the unit change, and any caller
comparing it against a calibrated threshold must be able to see that it cannot.
"""
import time

import pytest

from data.market_data import FYERS_INDEX_SYMBOLS, MarketDataProvider


class _Source:
    def __init__(self, available=True, volume=2_333_305.0, oi=13_076_635.0):
        self.is_connected = True
        self._payload = {
            "available": available, "tradingsymbol": "NIFTY26AUGFUT",
            "instrument_token": 1, "volume": volume, "oi": oi, "unit": "contracts",
        }
        if not available:
            self._payload = {"available": False, "reason": "no live contract"}

    def index_futures_activity(self, index):
        return dict(self._payload)


def _provider(fyers_volume=None, age=0.0, futures=_Source()):
    p = MarketDataProvider.__new__(MarketDataProvider)
    p._ws_cache = {}
    p._quote_divergence = {}
    p._volume_fallback_alerted = {}
    p.kite = futures
    if fyers_volume is not None:
        p._ws_cache[FYERS_INDEX_SYMBOLS["NIFTY"]] = {
            "ltp": 24383.6,
            "vol": fyers_volume,
            "time": time.time() - age,
            # Volume freshness is intentionally independent of quote freshness.
            "vol_time": time.time() - age,
        }
    return p


def test_fyers_is_primary_when_online():
    activity = _provider(fyers_volume=409_196_200.0).get_index_activity("NIFTY")
    assert activity["source"] == "fyers_index"
    assert activity["unit"] == "shares"
    assert activity["volume"] == 409_196_200.0
    # Thresholds were calibrated on this quantity, so they still apply.
    assert activity["comparable_to_thresholds"] is True


def test_broker_futures_confirm_participation_without_replacing_the_number():
    activity = _provider(fyers_volume=409_196_200.0).get_index_activity("NIFTY")
    assert activity["confirmation"] == "broker_confirms"
    assert activity["futures"]["oi"] == 13_076_635.0
    # The broker figure must NOT overwrite the primary reading.
    assert activity["volume"] == 409_196_200.0


def test_fallback_engages_when_fyers_goes_offline():
    activity = _provider(fyers_volume=None).get_index_activity("NIFTY")
    assert activity["fyers_online"] is False
    assert activity["source"] == "kite_index_futures"
    assert activity["volume"] == 2_333_305.0
    assert activity["oi"] == 13_076_635.0


def test_fallback_engages_when_the_fyers_reading_goes_stale():
    """A frozen feed is offline even though the socket is technically still there."""
    fresh = _provider(fyers_volume=409_196_200.0, age=5.0).get_index_activity("NIFTY")
    assert fresh["source"] == "fyers_index"
    stale = _provider(fyers_volume=409_196_200.0, age=600.0).get_index_activity("NIFTY")
    assert stale["source"] == "kite_index_futures"


def test_the_fallback_declares_that_thresholds_no_longer_apply():
    """Contracts vs constituent shares differ by ~1000x; a silent swap is a wrong trade."""
    activity = _provider(fyers_volume=None).get_index_activity("NIFTY")
    assert activity["unit"] == "contracts"
    assert activity["comparable_to_thresholds"] is False
    assert activity["confirmation"] == "fallback_only"


def test_no_source_at_all_reports_itself_rather_than_returning_zero():
    activity = _provider(fyers_volume=None, futures=_Source(available=False)).get_index_activity("NIFTY")
    assert activity["source"] == "none"
    assert activity["confirmation"] == "no_volume_source"
    assert activity["comparable_to_thresholds"] is False


def test_fallback_warning_is_throttled():
    p = _provider(fyers_volume=None)
    for _ in range(10):
        p.get_index_activity("NIFTY")
    assert len(p._volume_fallback_alerted) == 1


# ── volume-series provenance ─────────────────────────────────────────────────
def _prov():
    p = MarketDataProvider.__new__(MarketDataProvider)
    p._volume_source_changed_at = {}
    p._volume_source_active = {}
    return p


def test_ratio_window_is_suppressed_across_a_source_switch():
    """A ratio is unit-free only while the whole window shares one source.

    3 recent bars of futures contracts over 17 prior bars of constituent shares gives
    ~0.006 and reads THIN; the reverse gives ~175 and reads SURGE. Both are artefacts
    of the join, and either would drive a real trade.
    """
    p = _prov()
    p._note_volume_source("NIFTY", "fyers_index")
    assert p.volume_series_is_homogeneous("NIFTY", 5.0, 20) is True
    p._note_volume_source("NIFTY", "kite_index_futures")
    assert p.volume_series_is_homogeneous("NIFTY", 5.0, 20) is False
    assert p.volume_series_is_homogeneous("NIFTY", 1.0, 20) is False


def test_window_recovers_once_it_is_one_source_again():
    p = _prov()
    p._note_volume_source("NIFTY", "fyers_index")
    p._note_volume_source("NIFTY", "kite_index_futures")
    p._volume_source_changed_at["NIFTY"] = time.time() - (5 * 60 * 20 + 5)
    assert p.volume_series_is_homogeneous("NIFTY", 5.0, 20) is True
    # A 15min window spans longer, so it is still mixed at the same moment.
    assert p.volume_series_is_homogeneous("NIFTY", 15.0, 20) is False


def test_repeating_the_same_source_does_not_reset_the_window():
    p = _prov()
    p._note_volume_source("NIFTY", "fyers_index")
    for _ in range(5):
        p._note_volume_source("NIFTY", "fyers_index")
    assert p._volume_source_changed_at == {}
    assert p.volume_series_is_homogeneous("NIFTY", 5.0, 20) is True


def test_first_ever_source_is_not_treated_as_a_switch():
    """Startup is not a transition; suppressing volume then would blind the open."""
    p = _prov()
    p._note_volume_source("NIFTY", "fyers_index")
    assert p.volume_series_is_homogeneous("NIFTY", 5.0, 20) is True
