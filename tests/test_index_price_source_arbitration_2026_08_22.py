"""Regression coverage for active/standby index price routing.

The two broker sockets run on independent SDK threads.  These tests pin the rule that
only one of their prices may mutate CandleBuilder while Fyers can still contribute its
cumulative index volume.
"""

from datetime import datetime
import threading

import pytest

from data.market_data import FYERS_INDEX_SYMBOLS, MarketDataProvider


NOW = 2_000_000_000.0
KITE_TOKEN = "256265"


def _provider(market_open=True):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._ws_cache = {}
    provider._ltp_cache = {}
    provider._index_quote_lock = threading.RLock()
    provider._index_price_route = {}
    provider._kite_index_names = {KITE_TOKEN: "NIFTY"}
    provider._quote_divergence = {}
    provider._quote_divergence_alerted_at = {}
    provider._tick_callback_ref = None
    provider._tick_callback_takes_source = None
    provider._last_ltp_time = 0.0
    provider._yahoo_request_count = 0
    provider._yahoo_last_used_at = None
    provider._prev_close_global_cache = {}
    provider._prev_close_cache_time = {}
    provider._prev_close_cache_source = {}
    provider.smart_api = None
    provider.is_connected = False
    provider.kite = None
    provider.fyers = None
    provider.is_market_open = lambda dt=None: bool(market_open)
    provider.get_market_session_label = (
        lambda dt=None: "LIVE" if market_open else "CLOSED"
    )
    provider._flag_quote_divergence = lambda *args, **kwargs: 0.0
    return provider


def _quote(ltp, source, received_at, exchange_at=None, **extra):
    return {
        "ltp": float(ltp),
        "source": source,
        "time": float(received_at),
        "received_at": float(received_at),
        "exchange_at": exchange_at,
        **extra,
    }


def test_fresh_kite_price_wins_even_when_fyers_packet_is_newer():
    provider = _provider()
    provider._ws_cache[KITE_TOKEN] = _quote(
        24_500.0, "kite_ws", NOW - 0.8, NOW - 0.9
    )
    provider._ws_cache[FYERS_INDEX_SYMBOLS["NIFTY"]] = _quote(
        24_501.0, "fyers_ws", NOW - 0.01, NOW - 0.02
    )

    selected, age = provider._select_index_ws_quote("NIFTY", now_ts=NOW)

    assert selected["source"] == "kite_ws"
    assert selected["ltp"] == 24_500.0
    assert age == pytest.approx(0.9)


def test_fyers_volume_uses_cached_kite_price_without_price_contamination():
    provider = _provider()
    provider._ws_cache[KITE_TOKEN] = _quote(
        24_500.0, "kite_ws", NOW - 0.1, NOW - 0.2
    )
    emitted = []
    provider.on_tick = lambda instrument, price, volume, stamp, source: emitted.append(
        (instrument, price, volume, stamp, source)
    )

    assert provider._emit_arbitrated_index_tick(
        "NIFTY", 24_620.0, 0, "", "fyers_ws", NOW, NOW - 0.01
    ) is False
    assert provider._emit_arbitrated_index_tick(
        "NIFTY", 24_620.0, 12_345, "fyers_index", "fyers_ws", NOW, NOW - 0.01
    ) is True

    assert len(emitted) == 1
    assert emitted[0][1] == 24_500.0
    assert emitted[0][2] == 12_345
    assert emitted[0][4] == "fyers_index"


def test_fyers_price_requires_hysteresis_then_kite_recovers_immediately():
    provider = _provider()
    provider._ws_cache[KITE_TOKEN] = _quote(
        24_400.0, "kite_ws", NOW - 10.0, NOW - 10.0
    )
    emitted = []
    provider.on_tick = lambda instrument, price, volume, stamp, source: emitted.append(
        (price, volume, source)
    )

    assert provider._emit_arbitrated_index_tick(
        "NIFTY", 24_510.0, 0, "", "fyers_ws", NOW, NOW
    ) is False
    assert provider._emit_arbitrated_index_tick(
        "NIFTY", 24_512.0, 0, "", "fyers_ws", NOW + 0.30, NOW + 0.30
    ) is True
    assert emitted[-1][0] == 24_512.0
    assert provider._index_price_route["NIFTY"]["active_source"] == "fyers_ws"

    provider._ws_cache[KITE_TOKEN] = _quote(
        24_505.0, "kite_ws", NOW + 0.40, NOW + 0.40
    )
    assert provider._emit_arbitrated_index_tick(
        "NIFTY", 24_505.0, 0, "", "kite_ws", NOW + 0.40, NOW + 0.40
    ) is True
    assert provider._index_price_route["NIFTY"]["active_source"] == "kite_ws"

    assert provider._emit_arbitrated_index_tick(
        "NIFTY", 24_700.0, 15_000, "fyers_index", "fyers_ws",
        NOW + 0.50, NOW + 0.50,
    ) is True
    assert emitted[-1] == (24_505.0, 15_000.0, "fyers_index")


def test_old_exchange_packet_is_rejected_even_when_just_received():
    provider = _provider()
    emitted = []
    provider.on_tick = lambda *args: emitted.append(args)

    accepted = provider._emit_arbitrated_index_tick(
        "NIFTY",
        24_500.0,
        10_000,
        "fyers_index",
        "fyers_ws",
        received_at=NOW,
        exchange_at=NOW - 30.0,
    )

    assert accepted is False
    assert emitted == []


def test_closed_session_packets_are_cached_elsewhere_but_never_build_candles():
    provider = _provider(market_open=False)
    emitted = []
    provider.on_tick = lambda *args: emitted.append(args)

    assert provider._emit_arbitrated_index_tick(
        "NIFTY", 24_500.0, 0, "", "kite_ws", NOW, NOW
    ) is False
    assert provider._emit_arbitrated_index_tick(
        "NIFTY", 24_700.0, 10_000, "fyers_index", "fyers_ws", NOW, NOW
    ) is False
    assert emitted == []


def test_cache_only_snapshot_prefers_kite_and_exposes_both_clock_ages():
    provider = _provider()
    provider._index_price_route["NIFTY"] = {"active_source": "kite_ws"}
    provider._prev_close_global_cache["NIFTY"] = 24_400.0
    provider._ws_cache[KITE_TOKEN] = _quote(
        24_500.0, "kite_ws", NOW - 0.2, NOW - 1.5
    )
    provider._ws_cache[FYERS_INDEX_SYMBOLS["NIFTY"]] = _quote(
        24_550.0, "fyers_ws", NOW - 0.01, NOW - 0.02
    )

    class NoNetworkAccess:
        def __getattribute__(self, name):
            raise AssertionError(f"cache snapshot touched broker attribute {name}")

    provider.kite = NoNetworkAccess()
    provider.fyers = NoNetworkAccess()

    snapshot = provider.get_cached_index_quote_snapshot("NIFTY", now_ts=NOW)

    assert snapshot["cached_only"] is True
    assert snapshot["ltp"] == 24_500.0
    assert snapshot["spot_price"] == 24_500.0
    assert snapshot["change"] == 100.0
    assert snapshot["change_points"] == 100.0
    assert snapshot["source"] == snapshot["quote_source"] == "kite_ws"
    assert snapshot["selected_price_source"] == "kite_ws"
    assert snapshot["source_role"] == "primary"
    assert snapshot["candle_price_source"] == "kite_ws"
    assert snapshot["receipt_age_seconds"] == pytest.approx(0.2)
    assert snapshot["exchange_age_seconds"] == pytest.approx(1.5)
    assert snapshot["quote_age_seconds"] == pytest.approx(1.5)
    assert snapshot["freshness_basis"] == "exchange_and_receipt"
    assert snapshot["exchange_timestamp"] is not None
    assert snapshot["is_fresh"] is True
    assert snapshot["quote_fresh"] is True
    assert snapshot["quote_stale"] is False
    assert snapshot["received_at_epoch"] == NOW - 0.2
    assert snapshot["exchange_timestamp_epoch"] == NOW - 1.5


def test_fresh_kite_rest_beats_fresh_fyers_websocket_for_entry_authority():
    provider = _provider()
    provider._ltp_cache["NIFTY"] = _quote(
        24_094.15, "kite_rest", NOW - 0.01, NOW - 0.02
    )
    provider._ws_cache[FYERS_INDEX_SYMBOLS["NIFTY"]] = _quote(
        24_080.0, "fyers_ws", NOW - 0.001, NOW - 0.002
    )

    snapshot = provider.get_cached_index_quote_snapshot("NIFTY", now_ts=NOW)

    assert snapshot["ltp"] == pytest.approx(24_094.15)
    assert snapshot["selected_price_source"] == "kite_rest"
    assert snapshot["source_role"] == "primary"
    assert snapshot["is_fresh"] is True


def test_snapshot_rejects_replayed_kite_packet_and_uses_fresh_fyers():
    provider = _provider()
    provider._ws_cache[KITE_TOKEN] = _quote(
        24_500.0, "kite_ws", NOW - 0.1, NOW - 20.0
    )
    provider._ws_cache[FYERS_INDEX_SYMBOLS["NIFTY"]] = _quote(
        24_510.0, "fyers_ws", NOW - 0.2, NOW - 0.3
    )

    snapshot = provider.get_cached_index_quote_snapshot("NIFTY", now_ts=NOW)

    assert snapshot["ltp"] == 24_510.0
    assert snapshot["source"] == "fyers_ws"
    assert snapshot["source_role"] == "failover"


def test_regular_no_ws_index_quote_uses_unified_resolver_not_direct_fyers():
    provider = _provider()
    provider.fyers = object()
    provider._preopen_rest_quote_allowed = lambda **kwargs: False
    direct_fyers_calls = []
    provider._fyers_index_quote_values = lambda symbol: direct_fyers_calls.append(symbol) or {
        "lp": 24_000.0
    }
    unified_calls = []

    def unified(token, symbol, exchange):
        unified_calls.append((token, symbol, exchange))
        provider._ltp_cache[symbol] = {
            "ltp": 24_500.0,
            "time": datetime.fromtimestamp(NOW),
            "received_at": NOW,
            "source": "kite",
        }
        return 24_500.0

    provider.get_latest_price_by_token = unified
    provider.get_previous_close = lambda symbol: 24_400.0

    quote = provider.get_index_quote("NIFTY")

    assert direct_fyers_calls == []
    assert unified_calls == [("99926000", "NIFTY", "NSE")]
    assert quote["ltp"] == 24_500.0
    assert quote["source"] == "kite"


def test_fyers_rest_fallback_caches_exchange_and_receipt_time():
    provider = _provider()

    class FakeFyers:
        def quotes(self, payload):
            return {
                "s": "ok",
                "d": [{
                    "v": {
                        "lp": 24_510.0,
                        "ch": 110.0,
                        "chp": 0.45,
                        "prev_close_price": 24_400.0,
                        "exch_feed_time": NOW - 0.75,
                    }
                }],
            }

    provider.fyers = FakeFyers()

    price = provider.get_latest_price_by_token("99926000", "NIFTY", "NSE")
    cached = provider._ltp_cache["NIFTY"]

    assert price == 24_510.0
    assert cached["source"] == "fyers"
    assert cached["exchange_at"] == NOW - 0.75
    assert cached["received_at"] > 0
    assert cached["previous_close"] == 24_400.0
