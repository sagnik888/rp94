import logging
import threading
import time
from types import SimpleNamespace

import pytest

from data.market_data import MarketDataProvider
from engine.expiry_manager import expiry_manager
from engine.signal_processor import TradeCandidate
from scanner import Scanner
from trading.api_rate_limiter import SharedApiRateLimiter, get_shared_api_limiter
from trading.secure_logging import (
    SensitiveDataFilter,
    redact_sensitive_log_file,
    redact_sensitive_text,
)


def _provider(api):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._ws_cache = {}
    provider._ltp_cache = {}
    provider._yahoo_request_count = 0
    provider._yahoo_last_used_at = None
    provider._last_ltp_time = 0.0
    provider._angel_rest_backoff_until = 0.0
    provider._angel_rest_backoff_failures = 0
    provider._last_angel_rest_backoff_reason = ""
    provider._rest_lock = threading.Lock()
    provider._angel_quote_lease_state = threading.local()
    provider.is_connected = True
    provider.smart_api = api
    provider.fyers = None
    get_shared_api_limiter(api).min_interval_seconds = 0.001
    return provider


def test_angel_timeout_exception_latches_backoff_skips_repeat_and_success_resets():
    class Api:
        def __init__(self):
            self.calls = 0
            self.recovered = False

        def ltpData(self, *_args):
            self.calls += 1
            if not self.recovered:
                raise TimeoutError("Angel read timed out")
            return {"status": True, "data": {"ltp": 123.45, "close": 120.0}}

    api = Api()
    provider = _provider(api)

    assert provider.get_latest_price_by_token("825546", "SENSEX2671677600PE", "BFO") == 0.0
    assert provider._angel_rest_backoff_until > time.time()
    assert provider._angel_rest_backoff_failures == 1

    assert provider.get_latest_price_by_token("825546", "SENSEX2671677600PE", "BFO") == 0.0
    assert api.calls == 1

    provider._angel_rest_backoff_until = 0.0
    get_shared_api_limiter(api)._last_call = 0.0
    api.recovered = True
    assert provider.get_latest_price_by_token("825546", "SENSEX2671677600PE", "BFO") == 123.45
    assert provider._angel_rest_backoff_failures == 0
    assert provider._angel_rest_backoff_until == 0.0


def test_one_best_effort_quote_in_flight_and_priority_call_excludes_quotes():
    limiter = SharedApiRateLimiter(min_interval_seconds=0.001)
    assert limiter.try_begin_best_effort_quote() is True
    assert limiter.try_begin_best_effort_quote() is False

    priority_acquired = threading.Event()
    release_priority = threading.Event()

    def priority_worker():
        assert limiter.begin_priority_call(timeout=1.0) is True
        priority_acquired.set()
        release_priority.wait(timeout=1.0)
        limiter.end_priority_call()

    worker = threading.Thread(target=priority_worker)
    worker.start()
    deadline = time.time() + 1.0
    while time.time() < deadline and limiter.snapshot()["priority_waiters"] == 0:
        time.sleep(0.001)
    assert limiter.snapshot()["priority_waiters"] == 1

    limiter.end_best_effort_quote()
    assert priority_acquired.wait(timeout=1.0)
    assert limiter.try_begin_best_effort_quote() is False
    assert limiter.snapshot()["active_priority_calls"] == 1

    release_priority.set()
    worker.join(timeout=1.0)
    assert not worker.is_alive()


def test_hanging_angel_cannot_consume_scan_budget_when_exact_fyers_premium_exists(monkeypatch):
    monkeypatch.setattr(
        expiry_manager,
        "expiries",
        {"MIDCPNIFTY": {"active_monthly": "28JUL2026"}},
    )

    class Fyers:
        def __init__(self):
            self.calls = 0

        def optionchain(self, data):
            self.calls += 1
            if not data.get("timestamp"):
                return {
                    "s": "ok",
                    "data": {"expiryData": [{"date": "28-07-2026", "expiry": "1785196800"}]},
                }
            return {
                "s": "ok",
                "data": {
                    "optionsChain": [
                        {
                            "strike_price": 14750.0,
                            "option_type": "CE",
                            "ltp": 123.45,
                            # Deliberately provider-specific: exactness comes from
                            # active expiry + locally resolved Angel contract.
                            "symbol": "NSE:MIDCPNIFTY26JUL14750CE",
                            "fyToken": "FYERS_TOKEN",
                        }
                    ]
                },
            }

    class Data:
        def __init__(self):
            self.fyers = Fyers()
            self.angel_calls = 0
            self.subscriptions = []

        def subscribe_tokens(self, tokens, exchange_type=1):
            self.subscriptions.append((tuple(tokens), exchange_type))

        def get_cached_contract_ltp(self, *_args, **_kwargs):
            return None

        def get_cached_option_chain(self, *_args, **_kwargs):
            return None

        def get_option_token(self, instrument, strike, option_type):
            assert (instrument, strike, option_type) == ("MIDCPNIFTY", 14750.0, "CE")
            return {
                "symbol": "MIDCPNIFTY28JUL2614750CE",
                "token": "63515",
            }

        def get_ltp(self, *_args):
            self.angel_calls += 1
            time.sleep(0.75)  # Regression sentinel: this must never run.
            raise TimeoutError("hanging Angel endpoint")

    scanner = Scanner.__new__(Scanner)
    scanner._premium_cache = {}
    scanner.market_info = {"MIDCPNIFTY": {"is_monthly_week": True, "is_monthly_rollover": False}}
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 14745.0)
    scanner.data = Data()
    candidate = TradeCandidate(
        instrument="MIDCPNIFTY",
        direction="LONG",
        price=121.0,
        stop=110.0,
        target=150.0,
        lots=1,
        lot_size=120,
        grade="A",
        confidence=0.9,
        timeframe="5min",
        inst_type="OPT",
        option_type="CE",
        atm_strike=14750.0,
        multiplier=0.5,
        trading_symbol="MIDCPNIFTY28JUL2614750CE",
        symbol_token="63515",
    )
    candidate.entry_spot = 14745.0

    started = time.perf_counter()
    premium = scanner._get_live_premium_cached(candidate)
    elapsed = time.perf_counter() - started

    assert premium == 123.45
    assert candidate.quote_source == "fyers_option_chain_exact"
    assert candidate.quote_verified is True
    assert scanner.data.angel_calls == 0
    assert scanner.data.fyers.calls == 2
    assert elapsed < 0.25  # Far below the live 9s per-index scan budget.


def test_smartapi_sensitive_headers_are_redacted_in_records_and_existing_log(tmp_path):
    secret_token = "eyJhbGciOiJIUzUxMiJ9.secret.signature"
    secret_key = "private-api-key"
    message = (
        "Headers: {'Authorization': 'Bearer " + secret_token + "', "
        "'X-PrivateKey': '" + secret_key + "'}"
    )
    safe = redact_sensitive_text(message)
    assert secret_token not in safe
    assert secret_key not in safe
    assert safe.count("<redacted>") == 2

    record = logging.LogRecord("smartapi", logging.ERROR, __file__, 1, "%s", (message,), None)
    assert SensitiveDataFilter().filter(record) is True
    assert secret_token not in record.getMessage()
    assert secret_key not in record.getMessage()

    sdk_log = tmp_path / "app.log"
    sdk_log.write_text(message, encoding="utf-8")
    assert redact_sensitive_log_file(sdk_log) is True
    sanitized = sdk_log.read_text(encoding="utf-8")
    assert secret_token not in sanitized
    assert secret_key not in sanitized
