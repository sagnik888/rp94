"""A timed-out scan must not perform another broker round-trip on its loop."""

from types import SimpleNamespace

import pytest

from scanner import Scanner


def _fallback_host(inst_type, cached_price=0.0, *, accepted=True):
    host = Scanner.__new__(Scanner)
    host.mode = "REAL"
    host.market_info = {}
    host._premium_cache = {}
    candidate = SimpleNamespace(
        instrument="NIFTY", inst_type=inst_type, action="ENTRY", price=100.0,
        current_price=100.0, pnl=0.0, status="TRADE SIGNAL",
        trading_symbol="NIFTY26OCT25000CE" if inst_type == "OPT" else "NIFTY26OCTFUT",
        symbol_token="123", accepted_by_gate=accepted, quote_verified=True,
    )
    blocking_calls, refresh_requests, breach_checks = [], [], []

    def broker_quote(*args):
        blocking_calls.append(args)
        return 110.0

    host.data = SimpleNamespace(
        get_cached_contract_ltp=lambda *a, **kw: cached_price,
        subscribe_tokens=lambda *a, **kw: None,
        get_ltp=broker_quote,
    )
    host._get_option_chain_premium = lambda *a, **kw: 0.0
    host._option_premium_quote_plausible = lambda *a: True
    host._contract_quote_age_seconds = lambda *a: 0.0
    host._request_premium_refresh = refresh_requests.append
    host._get_candidate_ltp_cached = broker_quote
    host._loaded_session_is_current_day = lambda: True
    host._get_session_trade_candidates = lambda name: [candidate]
    host._check_session_candidates_breaches = breach_checks.append
    host._candidate_net_pnl = lambda row, price: price - row.price
    host._serialize_trade_candidate = lambda row: dict(vars(row))
    host.candles = SimpleNamespace(get_latest_price=lambda name: 25000.0)
    host.latest_results = {"instruments": {"NIFTY": {"ltp": 24999.0, "chart": {"cached": True}}}}
    return host, candidate, blocking_calls, refresh_requests, breach_checks


@pytest.mark.parametrize("inst_type", ["OPT", "FUT"])
def test_timeout_fallback_keeps_stale_marks_without_fetching_on_the_caller(inst_type):
    host, candidate, blocking_calls, requests, checks = _fallback_host(inst_type)

    ui, entries = host._build_live_fallback_result("NIFTY", {}, "Timeout")

    assert blocking_calls == [], "timeout recovery started a blocking broker lookup"
    assert requests == [candidate]
    assert candidate.live_quote_stale is True
    assert candidate.current_price == 100.0
    assert candidate.quote_verified is True, "accepted entry evidence must survive feed staleness"
    assert checks == ["NIFTY"], "fallback must retain the advisory protection check"
    assert entries == []
    assert ui["fallback_active"] is True and ui["fallback_reason"] == "Timeout"
    assert ui["ltp"] == 25000.0 and ui["quote_source"] == "fallback_cache"
    assert host.latest_results["instruments"]["NIFTY"]["ltp"] == 24999.0


@pytest.mark.parametrize("inst_type", ["OPT", "FUT"])
def test_timeout_fallback_uses_fresh_cached_contract_marks(inst_type):
    host, candidate, blocking_calls, requests, _ = _fallback_host(inst_type, 105.0)

    ui, entries = host._build_live_fallback_result("NIFTY", {}, "Timeout")

    assert blocking_calls == []
    assert requests == []
    assert candidate.live_quote_stale is False
    assert candidate.current_price == 105.0 and candidate.pnl == 5.0
    assert ui["trade_candidates"][0]["current_price"] == 105.0
    assert entries == []


def test_timeout_fallback_does_not_bless_an_unaccepted_missing_quote():
    host, candidate, blocking_calls, _, _ = _fallback_host("OPT", accepted=False)
    host._build_live_fallback_result("NIFTY", {}, "Timeout")
    assert blocking_calls == []
    assert candidate.quote_verified is False
    assert candidate.status == "QUOTE UNAVAILABLE"


def test_timeout_fallback_keeps_a_completed_exit_frozen():
    host, candidate, blocking_calls, requests, _ = _fallback_host("OPT", 105.0)
    candidate.action, candidate.exit_price = "EXIT", 97.0
    host._build_live_fallback_result("NIFTY", {}, "Timeout")
    assert candidate.current_price == 97.0
    assert blocking_calls == requests == []
