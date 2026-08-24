"""The watchdog's in-memory health branch must still name the futures fallback.

`_alert_volume_source_degradation` runs on a timer and cannot reach the broker, so it
calls `volume_source_health(probe_broker=False)`. That branch used to infer the fallback
from `_index_cum_volume_source == "kite_index_futures"`.

Futures volume no longer lands there — it is a different unit from the cash shares an
index candle's volume column holds, so it is recorded separately and never ticked. The
risk that creates is silence: if the health branch kept reading the cumulative source, a
quiet cash feed would report `source="none"` and the operator would lose both the OI and
the name of the feed actually in play. The fallback being active is precisely what has to
be surfaced, because trading continues through it.

`probe_broker=False` had no test coverage at all before this file.
"""
import time

import pytest

from scanner import Scanner

FUTURES_VOLUME = 250_000.0
FUTURES_OI = 1_200_000.0
CASH_VOLUME = 409_000_000.0


class _Provider:
    """The in-memory state the tick path maintains after the unit split."""

    def __init__(self, *, cash=None, cash_age=0.0, fallback=None, fallback_age=0.0):
        now = time.time()
        self._index_cum_volume = {"NIFTY": cash} if cash else {}
        self._index_cum_volume_source = {"NIFTY": "fyers_index"} if cash else {}
        self._index_cum_volume_at = {"NIFTY": now - cash_age} if cash else {}
        self._index_futures_fallback = (
            {"NIFTY": {"volume": fallback, "oi": FUTURES_OI, "at": now - fallback_age}}
            if fallback
            else {}
        )


def _scanner(provider):
    s = Scanner.__new__(Scanner)
    s.data = provider
    s.config = {"indices": {"NIFTY": {}}}
    s.active_indices = ["NIFTY"]
    return s


def _row(**kwargs):
    health = _scanner(_Provider(**kwargs)).volume_source_health(probe_broker=False)
    return health, health["instruments"]["NIFTY"]


def test_a_live_cash_feed_reports_healthy():
    health, row = _row(cash=CASH_VOLUME)
    assert health["degraded"] == []
    assert row["source"] == "fyers_index"
    assert row["unit"] == "shares"
    assert row["comparable_to_thresholds"] is True
    assert row["fyers_online"] is True
    assert row["volume"] == CASH_VOLUME


def test_a_quiet_cash_feed_still_names_the_futures_fallback():
    """The whole point: trading continues, so the degradation has to announce itself."""
    health, row = _row(cash=CASH_VOLUME, cash_age=600.0, fallback=FUTURES_VOLUME)
    assert health["degraded"] == ["NIFTY"]
    assert row["degraded"] is True
    assert row["source"] == "kite_index_futures"
    assert row["unit"] == "contracts"
    assert row["comparable_to_thresholds"] is False
    assert row["fyers_online"] is False
    assert row["futures_available"] is True
    assert row["futures_volume"] == FUTURES_VOLUME
    assert row["futures_oi"] == FUTURES_OI, "the OI read is the fallback's other half"


def test_the_fallback_is_named_even_when_no_cash_total_was_ever_seen():
    """A session that starts with Fyers already quiet has no cash total to go stale."""
    health, row = _row(fallback=FUTURES_VOLUME)
    assert health["degraded"] == ["NIFTY"]
    assert row["source"] == "kite_index_futures"
    assert row["futures_oi"] == FUTURES_OI


def test_a_total_outage_reports_no_source():
    health, row = _row()
    assert health["degraded"] == ["NIFTY"]
    assert row["source"] == "none"
    assert row["volume"] == 0.0
    assert row["futures_available"] is False


def test_a_stale_fallback_is_not_reported_as_live():
    """A record from ten minutes ago is not evidence of a feed that is still answering."""
    health, row = _row(fallback=FUTURES_VOLUME, fallback_age=600.0)
    assert row["source"] == "none"
    assert health["degraded"] == ["NIFTY"]


def test_a_live_cash_feed_outranks_a_lingering_fallback_record():
    """Recovery must read as recovered, not stay pinned to the last outage."""
    health, row = _row(cash=CASH_VOLUME, fallback=FUTURES_VOLUME)
    assert row["source"] == "fyers_index"
    assert row["comparable_to_thresholds"] is True
    assert health["degraded"] == []


def test_the_degradation_alert_fires_off_the_in_memory_branch():
    """End to end: the timer path must still produce an operator-visible warning."""
    s = _scanner(_Provider(fallback=FUTURES_VOLUME))
    seen = []
    s.on_notification = lambda msg, level="info": seen.append((msg, level))
    s._alert_volume_source_degradation()
    assert seen, "a live futures fallback produced no alert at all"
    assert any("NIFTY" in msg for msg, _ in seen)
