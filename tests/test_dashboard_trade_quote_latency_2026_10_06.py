"""Dashboard trade refreshes must use cached marks without blocking the scan loop."""

from types import SimpleNamespace

import pytest

from engine.scanner_dashboard import ScannerTradePayloadBuilder
from tests.test_live_fallback_quote_latency_2026_10_06 import _fallback_host


def _dashboard_host(inst_type, cached_price=0.0, *, accepted=True):
    host, candidate, calls, requests, _ = _fallback_host(
        inst_type, cached_price, accepted=accepted,
    )
    host.backtest_days = host.scan_count = host.simulation_id = 1
    host.inst_pref = "AUTO"
    host.active_indices = ["NIFTY"]
    host.config = {"indices": {"NIFTY": {}}}
    host._session_trade_candidates = {"NIFTY": {}}
    host._session_candidate_day = "2026-10-06"
    host._get_current_session_day = lambda: host._session_candidate_day
    host._normalize_loaded_exit_candidate = lambda row: None
    host._setting_enabled = lambda value, default: default
    host.trades = SimpleNamespace(get_dashboard_payload=lambda **kwargs: {
        "open": [], "closed": [], "summary": {}, "analytics": {},
    })
    candidate.lifecycle_id = "dashboard-life"
    candidate.id = "SESSION|dashboard-life"
    candidate.direction = "LONG"
    candidate.timeframe = "5min"
    candidate.entry_timestamp = "2026-10-06T12:00:00"
    return host, candidate, calls, requests


@pytest.mark.parametrize("inst_type", ["OPT", "FUT"])
def test_trade_payload_keeps_stale_marks_without_broker_io(inst_type):
    host, candidate, calls, requests = _dashboard_host(inst_type)

    payload = ScannerTradePayloadBuilder(host).build()

    assert calls == [], "dashboard refresh attempted a blocking broker lookup"
    assert requests == [candidate]
    assert candidate.live_quote_stale is True
    assert candidate.quote_verified is True
    assert candidate.current_price == 100.0
    assert payload["signals"][0]["current_price"] == 100.0
    assert payload["signals"][0]["live_quote_stale"] is True


@pytest.mark.parametrize("inst_type", ["OPT", "FUT"])
def test_trade_payload_uses_fresh_cached_contract_marks(inst_type):
    host, candidate, calls, requests = _dashboard_host(inst_type, 105.0)

    payload = ScannerTradePayloadBuilder(host).build()

    assert calls == requests == []
    assert candidate.live_quote_stale is False
    assert candidate.current_price == 105.0 and candidate.pnl == 5.0
    assert payload["signals"][0]["current_price"] == 105.0
    assert payload["signals"][0]["pnl"] == 0.0, "manual marks are not broker P&L"


def test_trade_payload_does_not_bless_an_unaccepted_missing_quote():
    host, candidate, calls, _ = _dashboard_host("OPT", accepted=False)

    payload = ScannerTradePayloadBuilder(host).build()

    assert calls == []
    assert candidate.quote_verified is False
    assert candidate.status == "QUOTE UNAVAILABLE"
    assert payload["signals"] == []


def test_trade_payload_preserves_a_completed_exit_mark():
    host, candidate, calls, requests = _dashboard_host("OPT", 105.0)
    candidate.action = "EXIT"
    candidate.exit_price = 97.0
    candidate.exit_timestamp = "2026-10-06T12:01:00"

    payload = ScannerTradePayloadBuilder(host).build()

    assert calls == requests == []
    assert candidate.current_price == 97.0
    assert payload["signals"][0]["current_price"] == 97.0
