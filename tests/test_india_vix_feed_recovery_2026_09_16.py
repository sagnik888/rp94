"""India VIX must have a live source, and a dead one must not hide behind its own cache.

`data_store/vix_history.json` stopped at 2026-08-01 00:06. Two defects, not one:

1. No source. India VIX arrived as AngelOne token 99926017 through the AngelOne socket.
   The Kite migration retired that socket and nothing re-subscribed the token, leaving
   `get_ltp("NSE", "INDIAVIX", "99926017")` with no working tier -- Kite has no INDIAVIX
   instrument, and FYERS_INDEX_SYMBOLS, INDEX_SPOT_TOKENS and YAHOO_TICKERS all lack it.
   Fyers does quote `NSE:INDIAVIX-INDEX` (verified live at 13.39 on 2026-09-16).

2. An immortal cache. `_get_live_volatility_context` re-stamped the cache entry's
   timestamp on a CACHE HIT, so the TTL was refreshed by the read it was meant to bound
   and the live quote was never consulted again. That is why the file recorded an
   identical 11.76 every minute all night and then stopped dead on the next restart.
"""
import time

import pytest

from data.market_data import (
    FYERS_AUX_SYMBOLS,
    FYERS_INDEX_SYMBOLS,
    FYERS_SYMBOL_TO_INDEX,
    fyers_quote_symbol,
)


# --- the source -----------------------------------------------------------------
def test_india_vix_resolves_to_a_fyers_quote_symbol():
    assert fyers_quote_symbol("INDIAVIX") == "NSE:INDIAVIX-INDEX"
    assert fyers_quote_symbol("indiavix") == "NSE:INDIAVIX-INDEX"


def test_tradable_indices_still_resolve_unchanged():
    for name, symbol in FYERS_INDEX_SYMBOLS.items():
        assert fyers_quote_symbol(name) == symbol


def test_an_unknown_symbol_resolves_to_empty_not_to_itself():
    """The Fyers tier is skipped on a falsy symbol; returning the raw name would
    send a junk symbol to the broker on every quote."""
    assert fyers_quote_symbol("NOT_A_SYMBOL") == ""
    assert fyers_quote_symbol("") == ""
    assert fyers_quote_symbol(None) == ""


def test_vix_is_kept_out_of_the_tradable_index_map():
    """FYERS_INDEX_SYMBOLS feeds the websocket subscribe list and the reverse
    index-name lookup. A volatility index is quotable, not tradable."""
    assert "INDIAVIX" not in FYERS_INDEX_SYMBOLS
    assert set(FYERS_INDEX_SYMBOLS) == {"NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"}
    assert "INDIAVIX" not in FYERS_SYMBOL_TO_INDEX.values()
    assert "INDIAVIX" in FYERS_AUX_SYMBOLS


# --- the immortal cache ----------------------------------------------------------
class _FakeData:
    def __init__(self, prices):
        self.prices = list(prices)
        self.calls = 0

    def get_ltp(self, exchange, symbol, token):
        self.calls += 1
        return self.prices.pop(0) if self.prices else None


class _Scanner:
    """The real method under test, bound to a minimal host."""

    def __init__(self, data):
        self.data = data
        self._premium_cache = {}
        self._vix_data = {}
        self.persisted = 0
        self.unavailable_notes = 0

    def _persist_vix_history(self):
        self.persisted += 1

    def _note_vix_unavailable(self):
        self.unavailable_notes += 1

    def _vix_percentile(self, value, as_of):
        return None


@pytest.fixture
def live_volatility_context():
    from scanner import Scanner
    return Scanner._get_live_volatility_context


def test_a_cache_hit_does_not_refresh_its_own_ttl(live_volatility_context, monkeypatch):
    """The defect: reading the cache renewed it, so the quote was never re-fetched."""
    from config.settings import Settings

    settings = Settings()
    settings.vix_live_cache_seconds = 60.0
    monkeypatch.setattr("scanner.get_settings", lambda: settings)

    host = _Scanner(_FakeData([13.39]))
    first = live_volatility_context(host)
    assert first["value"] == pytest.approx(13.39)
    assert host.data.calls == 1

    stamped_at = host._premium_cache["INDIAVIX"]["time"]

    # A second pass inside the TTL serves the cache...
    second = live_volatility_context(host)
    assert second["value"] == pytest.approx(13.39)
    assert host.data.calls == 1, "cache hit should not re-fetch inside the TTL"
    # ...but must NOT push the expiry out.
    assert host._premium_cache["INDIAVIX"]["time"] == stamped_at


def test_the_quote_is_re_consulted_once_the_ttl_lapses(live_volatility_context, monkeypatch):
    from config.settings import Settings

    settings = Settings()
    settings.vix_live_cache_seconds = 60.0
    monkeypatch.setattr("scanner.get_settings", lambda: settings)

    host = _Scanner(_FakeData([13.39, 14.02]))
    live_volatility_context(host)
    host._premium_cache["INDIAVIX"]["time"] = time.time() - 120.0

    refreshed = live_volatility_context(host)
    assert refreshed["value"] == pytest.approx(14.02)
    assert host.data.calls == 2


def test_a_dead_feed_is_reported_not_swallowed(live_volatility_context, monkeypatch):
    from config.settings import Settings

    settings = Settings()
    monkeypatch.setattr("scanner.get_settings", lambda: settings)

    host = _Scanner(_FakeData([]))       # get_ltp returns None, as it did all August
    result = live_volatility_context(host)

    assert result["value"] is None
    assert result["percentile"] is None
    assert result["source"] == "india_vix_insufficient_history"
    assert host.unavailable_notes == 1, "a dead VIX feed must say so"
    assert host.persisted == 0, "nothing to persist when there is no reading"


def test_a_dead_feed_never_writes_a_fabricated_value(live_volatility_context, monkeypatch):
    """Missing data must not masquerade as a measured neutral 15.0."""
    from config.settings import Settings

    monkeypatch.setattr("scanner.get_settings", lambda: Settings())
    host = _Scanner(_FakeData([0.0, None]))
    live_volatility_context(host)

    assert host._vix_data == {}
    assert "INDIAVIX" not in host._premium_cache


def test_a_live_reading_is_recorded_once_per_minute_key(live_volatility_context, monkeypatch):
    from config.settings import Settings

    monkeypatch.setattr("scanner.get_settings", lambda: Settings())
    host = _Scanner(_FakeData([13.39]))
    live_volatility_context(host)

    assert len(host._vix_data) == 1
    assert list(host._vix_data.values()) == [pytest.approx(13.39)]
    assert host.persisted == 1
