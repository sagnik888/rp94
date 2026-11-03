"""India VIX should use the live Fyers socket quote cache, not contend for REST."""

import threading

import pytest

import data.market_data as md


def _capture_fyers_socket(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UT1_FYERS_LOG_DIR", "logs/fyers")
    monkeypatch.setattr(md, "FYERS_LOGS_READY", False)
    captured = {"subscribe_calls": []}

    class _FakeSocket:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __setattr__(self, name, value):
            if name == "on_open":
                captured[name] = value
            object.__setattr__(self, name, value)

        def connect(self):
            raise AssertionError("the test must not open a network socket")

        def subscribe(self, symbols, **kwargs):
            captured["subscribe_calls"].append(list(symbols))

    class _NoThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(md.data_ws, "FyersDataSocket", _FakeSocket)
    monkeypatch.setattr(threading, "Thread", _NoThread)
    provider = md.MarketDataProvider.__new__(md.MarketDataProvider)
    provider.start_fyers_websocket("dummy-token")
    return provider, captured


def test_fyers_socket_subscribes_auxiliary_vix_without_classifying_it_as_a_tradable_index(
    monkeypatch, tmp_path
):
    provider, captured = _capture_fyers_socket(monkeypatch, tmp_path)

    captured["on_open"]()

    assert captured["subscribe_calls"] == [[
        *md.FYERS_INDEX_SYMBOLS.values(),
        *md.FYERS_AUX_SYMBOLS.values(),
    ]]
    assert "NSE:INDIAVIX-INDEX" not in md.FYERS_SYMBOL_TO_INDEX
    assert provider._fyers_ws_connected is True


def test_vix_socket_tick_is_cached_for_ltp_without_emitting_an_index_candle(
    monkeypatch, tmp_path
):
    provider, captured = _capture_fyers_socket(monkeypatch, tmp_path)
    provider._ws_cache = {}
    emitted_ticks = []
    provider.on_tick = lambda *args: emitted_ticks.append(args)
    provider._call_fyers_bounded = lambda *args, **kwargs: pytest.fail(
        "a fresh VIX socket tick must avoid the shared Fyers REST slot"
    )

    captured["on_message"]({"symbol": "NSE:INDIAVIX-INDEX", "ltp": 11.94})

    assert provider.get_latest_price_by_token("99926017", "INDIAVIX", "NSE") == pytest.approx(11.94)
    assert provider._ws_cache["NSE:INDIAVIX-INDEX"]["source"] == "fyers_ws"
    assert emitted_ticks == []
