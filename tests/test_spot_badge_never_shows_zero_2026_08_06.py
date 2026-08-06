"""An index does not trade at zero, so a zero spot badge is not a price.

get_index_quote's last fallback wrote whatever get_latest_price_by_token returned
straight into the panel, including 0.0. That happens routinely — before the open, between
websocket reconnects, and while both REST paths are timing out (the live log for
2026-08-06 shows Kite ltp failing with 503s and read timeouts). The panel then showed
0 for every index.

The last known price was in _ltp_cache the whole time, and the function read that very
dict for its `source` and `time` while ignoring its `ltp`.

Order of preference now: live tick, then last cached trade, then previous close, and only
then zero — with the source saying which, so nothing downstream mistakes a stale price for
a live one.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from data.market_data import MarketDataProvider


def _provider(**kw):
    p = MarketDataProvider.__new__(MarketDataProvider)
    p._ws_cache = {}
    p._ltp_cache = dict(kw.get("ltp_cache", {}))
    p._last_ltp_time = 0.0
    p.fyers = None
    p.nse_holidays = set()
    p.nse_special_trading_days = set()
    p.get_market_session_label = lambda dt=None: "LIVE"
    p.is_market_open = lambda dt=None: True
    p._select_index_ws_quote = lambda symbol, token="", now_ts=None: (None, None)
    p.get_latest_price_by_token = lambda token, symbol, exchange: kw.get("live", 0.0)
    p.get_previous_close = lambda symbol: kw.get("prev_close", 0.0)
    return p


def test_a_live_price_is_used_when_there_is_one():
    q = _provider(live=24500.0, prev_close=24400.0).get_index_quote("NIFTY")
    assert q["ltp"] == pytest.approx(24500.0)
    assert not q.get("is_stale")


def test_the_last_cached_trade_is_preferred_over_zero():
    """The live defect: this value was sitting in _ltp_cache and was ignored."""
    cache = {"NIFTY": {"ltp": 24483.75, "time": datetime.now(), "source": "kite"}}
    q = _provider(live=0.0, prev_close=24400.0, ltp_cache=cache).get_index_quote("NIFTY")
    assert q["ltp"] == pytest.approx(24483.75), "the panel showed 0 for a live index"
    assert q["is_stale"] is True
    assert q["source"].endswith("_stale")


def test_the_previous_close_is_the_next_best_answer():
    """Nothing cached yet — the first badge of the day is still not zero."""
    q = _provider(live=0.0, prev_close=24400.0).get_index_quote("NIFTY")
    assert q["ltp"] == pytest.approx(24400.0)
    assert q["source"] == "previous_close"
    assert q["is_stale"] is True


def test_genuinely_nothing_known_says_so_instead_of_pretending():
    q = _provider(live=0.0, prev_close=0.0).get_index_quote("NIFTY")
    assert q["ltp"] == 0.0
    assert q["source"] == "unavailable"
    assert q.get("is_stale") is False


def test_a_recovered_price_still_reports_the_day_change():
    cache = {"NIFTY": {"ltp": 24483.75, "time": datetime.now(), "source": "kite"}}
    q = _provider(live=0.0, prev_close=24400.0, ltp_cache=cache).get_index_quote("NIFTY")
    assert q["change"] == pytest.approx(83.75)
    assert q["change_pct"] == pytest.approx(83.75 / 24400.0 * 100.0)


def test_the_previous_close_fallback_reports_a_flat_day_not_a_fake_move():
    q = _provider(live=0.0, prev_close=24400.0).get_index_quote("NIFTY")
    assert q["change"] == pytest.approx(0.0)
    assert q["change_pct"] == pytest.approx(0.0)


def test_a_stale_cache_still_beats_zero_and_reports_its_age():
    """Old is a usable answer; zero is not. The age says how old."""
    cache = {
        "NIFTY": {
            "ltp": 24483.75,
            "time": datetime.now() - timedelta(minutes=40),
            "source": "kite",
        }
    }
    q = _provider(live=0.0, prev_close=24400.0, ltp_cache=cache).get_index_quote("NIFTY")
    assert q["ltp"] == pytest.approx(24483.75)
    assert q["quote_age_seconds"] > 2000


def test_a_corrupt_cache_entry_does_not_crash_the_panel():
    cache = {"NIFTY": {"ltp": "not-a-number", "time": datetime.now(), "source": "kite"}}
    q = _provider(live=0.0, prev_close=24400.0, ltp_cache=cache).get_index_quote("NIFTY")
    assert q["ltp"] == pytest.approx(24400.0)


@pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"])
def test_every_index_badge_is_covered(symbol):
    cache = {symbol: {"ltp": 1234.5, "time": datetime.now(), "source": "kite"}}
    q = _provider(live=0.0, prev_close=1200.0, ltp_cache=cache).get_index_quote(symbol)
    assert q["ltp"] == pytest.approx(1234.5)
