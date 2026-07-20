"""AngelOne history rate-limit cooldown: stop hammering the endpoint after an
"exceeding access rate" response (which the SDK surfaces as a JSON parse error)."""

import pandas as pd

from data.market_data import MarketDataProvider


class _RateLimitedApi:
    def __init__(self):
        self.calls = 0

    def getCandleData(self, params):
        self.calls += 1
        raise Exception(
            "Couldn't parse the JSON response received from the server: "
            "b'Access denied because of exceeding access rate'"
        )


def _provider():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.is_connected = True
    provider.smart_api = _RateLimitedApi()
    return provider


def test_rate_limited_history_fetch_enters_cooldown_and_skips_next_calls():
    provider = _provider()

    df = provider.get_historical_candles_angel("99926000", "NSE", "5min")
    assert df.empty
    assert provider.smart_api.calls == 1
    assert provider._angel_history_rate_limited() is True

    # While cooling down, the endpoint must not be touched at all.
    df = provider.get_historical_candles_angel("99926000", "NSE", "5min")
    assert df.empty
    assert provider.smart_api.calls == 1


def test_rate_limit_cooldown_escalates_and_resets_on_success():
    provider = _provider()

    provider._note_angel_history_rate_limit("test")
    first_until = provider._angel_hist_rate_limited_until
    provider._angel_hist_rate_limited_until = 0.0  # simulate cooldown expiry
    provider._note_angel_history_rate_limit("test")
    assert provider._angel_hist_rate_limit_streak == 2
    assert (provider._angel_hist_rate_limited_until - first_until) > 5.0

    class _OkApi:
        def getCandleData(self, params):
            return {
                "status": True,
                "data": [["2026-07-02T09:15:00+05:30", 1, 2, 0.5, 1.5, 100]],
            }

    provider.smart_api = _OkApi()
    provider._angel_hist_rate_limited_until = 0.0
    df = provider.get_historical_candles_angel("99926000", "NSE", "5min")
    assert not df.empty
    assert provider._angel_hist_rate_limit_streak == 0
