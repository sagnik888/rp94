"""Top reject "execution geometry is missing, non-finite, unverified, or unsourced;
execution geometry is stale or has no valid source clock" (2026-09-24 09:55/10:00 and
2026-09-25 09:25, all MIDCPNIFTY 5m S1 SELLs).

The option quote was fine (Kite touch 0.05s old). The final check also needs a fresh
live INDEX price from the primary broker feed (Kite, 7s) to re-anchor the index levels;
while scans took 8-12s the Kite tick thread fell behind and the index price aged out, so
the geometry stayed on the signal-bar clock and was refused. The block is right (never
enter on a stale index price); the message now says which input was refused.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from scanner import Scanner


def _scanner(quote):
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc.data = SimpleNamespace(get_cached_index_quote_snapshot=lambda instrument: quote)
    return sc


NOW = datetime(2026, 9, 25, 9, 25, 32)


@pytest.mark.parametrize("quote,why", [
    ({"spot_price": 14041.0, "quote_age_seconds": 11.0, "is_fresh": False, "source": "kite_ws",
      "source_role": "primary"}, "Kite index price is older than the freshness limit"),
    ({"spot_price": 14041.0, "quote_age_seconds": 0.5, "is_fresh": True, "source": "fyers_ws",
      "source_role": "failover"}, "index price is not from the primary broker feed (fyers_ws)"),
    ({"spot_price": 0.0, "quote_age_seconds": 0.5, "is_fresh": True, "source": "kite_ws",
      "source_role": "primary"}, "no live index price"),
    (None, "no live index quote"),
])
def test_a_refused_index_quote_is_named(quote, why):
    sc = _scanner(quote)
    assert sc._entry_policy_live_index_quote("MIDCPNIFTY", NOW) == (0.0, "", "")
    assert sc._index_quote_refusals["MIDCPNIFTY"] == why


def test_a_good_quote_clears_the_earlier_refusal():
    sc = _scanner({"spot_price": 14041.0, "quote_age_seconds": 11.0, "is_fresh": False,
                   "source": "kite_ws", "source_role": "primary"})
    sc._entry_policy_live_index_quote("MIDCPNIFTY", NOW)
    sc.data = SimpleNamespace(get_cached_index_quote_snapshot=lambda instrument: {
        "spot_price": 14041.0, "quote_age_seconds": 0.4, "is_fresh": True,
        "source": "kite_ws", "source_role": "primary"})
    price, _, source = sc._entry_policy_live_index_quote("MIDCPNIFTY", NOW)
    assert price == 14041.0 and source == "kite_ws"
    assert "MIDCPNIFTY" not in sc._index_quote_refusals


def test_the_final_policy_reject_says_why_the_geometry_was_unverified():
    sc = _scanner(None)
    sc._index_quote_refusals = {"MIDCPNIFTY": "Kite index price is older than the freshness limit"}
    logged = []
    sc.log_signal_decision_once = lambda key, msg, kind: logged.append(msg)
    sc._final_entry_policy_verdict = lambda holder, **kw: SimpleNamespace(
        allowed=False,
        reason="execution geometry is missing, non-finite, unverified, or unsourced; "
               "execution geometry is stale or has no valid source clock",
    )
    holder = SimpleNamespace(instrument="MIDCPNIFTY", timeframe="5min", direction="SHORT")
    ok, reason = sc._entry_policy_allows(holder, execution_levels=(83.75, 75.4, 92.1), phase="post_lane_final")
    assert ok is False
    assert reason.endswith("[live MIDCPNIFTY Kite index price is older than the freshness limit]")
    assert "MIDCPNIFTY 5min SHORT" in logged[0]
