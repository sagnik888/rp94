"""No fake or stale price may reach the engine or the screen unannounced.

Live incident, 2026-08-01. The dashboard showed:

    NIFTY 25,965.35 (+1581.75, 6.49%)     real close: 24,383.60
    BANKNIFTY 60,222.70 (+2957.85, 5.17%) real close: 57,264.85
    SENSEX 83,266.12 (+5171.48, 6.62%)    real close: 78,094.64

Fyers was returning a corrupt last price — its own prev_close field said 24,383.60
while its `lp` said 25,965.35 — and get_latest_price_by_token() consulted Fyers BEFORE
the execution broker, so the wrong number won. The engine would have sized, entered and
trailed against a price 6.5% away from where orders actually fill.

Two rules protect this now:
  * the execution broker outranks every other feed, because its price is the one orders
    fill against;
  * when two feeds disagree beyond a threshold, that is a data fault and it is
    announced, not silently resolved.
"""
from datetime import datetime

import pytest

from data.market_data import MarketDataProvider


def _provider():
    p = MarketDataProvider.__new__(MarketDataProvider)
    p._quote_divergence = {}
    p._ltp_cache = {}
    p._last_ltp_time = 0.0
    return p


# The exact readings from the incident.
INCIDENT = [
    ("NIFTY", 24383.60, 25965.35, 6.49),
    ("BANKNIFTY", 57264.85, 60222.70, 5.17),
    ("SENSEX", 78094.64, 83266.12, 6.62),
    ("MIDCPNIFTY", 14784.40, 15661.40, 5.93),
]


@pytest.mark.parametrize("symbol,kite,fyers,expected_pct", INCIDENT)
def test_the_live_incident_is_detected(symbol, kite, fyers, expected_pct):
    p = _provider()
    deviation = p._flag_quote_divergence(symbol, "kite", kite, "fyers", fyers)
    assert deviation == pytest.approx(expected_pct, abs=0.02)
    assert symbol in p.get_quote_divergences()


def test_agreeing_feeds_raise_nothing():
    p = _provider()
    assert p._flag_quote_divergence("NIFTY", "kite", 24383.60, "fyers", 24383.75) < 0.75
    assert p.get_quote_divergences() == {}


def test_a_recovered_feed_clears_the_flag():
    """A stuck flag would leave the operator distrusting good data indefinitely."""
    p = _provider()
    p._flag_quote_divergence("NIFTY", "kite", 24383.60, "fyers", 25965.35)
    assert "NIFTY" in p.get_quote_divergences()
    p._flag_quote_divergence("NIFTY", "kite", 24383.60, "fyers", 24383.60)
    assert p.get_quote_divergences() == {}


def test_a_missing_feed_is_not_a_divergence():
    """Absent data is not wrong data — it must not cry wolf."""
    p = _provider()
    assert p._flag_quote_divergence("NIFTY", "kite", 24383.60, "fyers", 0.0) == 0.0
    assert p._flag_quote_divergence("NIFTY", "kite", 0.0, "fyers", 25965.35) == 0.0
    assert p.get_quote_divergences() == {}


def test_the_divergence_report_names_both_sides():
    """The operator has to know which feed to distrust, not just that one is wrong."""
    p = _provider()
    p._flag_quote_divergence("NIFTY", "kite", 24383.60, "fyers", 25965.35)
    row = p.get_quote_divergences()["NIFTY"]
    assert row["primary"] == "kite" and row["primary_ltp"] == 24383.60
    assert row["secondary"] == "fyers" and row["secondary_ltp"] == 25965.35
    assert row["deviation_pct"] == pytest.approx(6.487, abs=0.01)


def test_threshold_sits_below_a_plausible_index_move():
    """A 6% index day is a once-in-years event; a 6% feed disagreement is a fault."""
    assert 0 < MarketDataProvider.QUOTE_DIVERGENCE_ALERT_PCT < 2.0
