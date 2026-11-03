"""A stale current quote must not replay successfully evaluated native ticks."""

from datetime import datetime, timedelta
from types import SimpleNamespace as NS
import time

import pytest

from data.market_data import MarketDataProvider
from engine import ut_ema_lane
from scanner import Scanner


def manual_host(monkeypatch, *, verified=False, stale=True):
    host = Scanner.__new__(Scanner)
    host.mode = "REAL"
    host.data = MarketDataProvider.__new__(MarketDataProvider)
    host.data._record_contract_tick("A", 100.0, time.time() - 10)
    host.data._record_contract_tick("A", 101.0, time.time() - 9)
    trade = NS(instrument="NIFTY", timeframe="5min", inst_type="OPT", direction="LONG",
               source_engine="ut_ema", symbol_token="A", trading_symbol="TEST_A_CE",
               price=100.0, current_price=100.0, stop=90.0, target=110.0,
               action="ENTRY", quote_verified=verified, live_quote_stale=stale,
               utema_state={"stop": 25000.0, "side": 1, "premium_risk_pct": 10.0})
    host._loaded_session_is_current_day = lambda: True
    host._active_session_entry_candidates = lambda instrument: [trade]
    host._observe_manual_signal_continuity = lambda row, now: False
    host._option_premium_quote_plausible = lambda row, price: True
    host.log_event = lambda *args: None
    host._diag_inc = lambda *args: None
    monkeypatch.setattr(ut_ema_lane, "result_for_monitor", lambda *_args: None)
    return host, trade


@pytest.mark.parametrize("verified,stale", [(False, False), (True, True)])
def test_successful_stale_hold_consumes_path_without_authorizing_quote(monkeypatch, verified, stale):
    host, trade = manual_host(monkeypatch, verified=verified, stale=stale)
    observed = []
    original = ut_ema_lane.exit_decision
    def evaluate(*args, **kwargs):
        observed.append(list(kwargs["observed_contract_ticks"]))
        return original(*args, **kwargs)
    monkeypatch.setattr(ut_ema_lane, "exit_decision", evaluate)

    host._check_session_candidates_breaches_locked("NIFTY")
    host._check_session_candidates_breaches_locked("NIFTY")

    assert [price for price, _ in observed[0]] == [100.0, 101.0]
    assert observed[1] == []
    assert trade._contract_tick_cursor == 2
    assert trade.quote_verified is verified and trade.live_quote_stale is stale
    assert trade.action == "ENTRY" and trade.current_price == 100.0


def test_failed_evaluation_keeps_ticks_available_for_retry(monkeypatch):
    host, trade = manual_host(monkeypatch)
    def fail(*args, **kwargs):
        raise RuntimeError("native evaluation unavailable")
    monkeypatch.setattr(ut_ema_lane, "exit_decision", fail)

    with pytest.raises(RuntimeError, match="native evaluation unavailable"):
        host._check_session_candidates_breaches_locked("NIFTY")

    assert getattr(trade, "_contract_tick_cursor", 0) == 0
    assert trade._contract_tick_pending_cursor == 2


def test_future_observation_is_not_committed_before_it_can_be_evaluated(monkeypatch):
    host, trade = manual_host(monkeypatch)
    host.data._record_contract_tick("A", 95.0, time.time() + 86400)

    host._check_session_candidates_breaches_locked("NIFTY")

    assert getattr(trade, "_contract_tick_cursor", 0) == 0
    assert trade._contract_tick_pending_cursor == 3


def test_native_exit_without_observed_premium_keeps_retry_path(monkeypatch):
    host, trade = manual_host(monkeypatch)
    monkeypatch.setattr(ut_ema_lane, "engine_exit_for", lambda *_args, **_kwargs: {
        "reason": "UT T2", "time": datetime.now() - timedelta(seconds=1),
    })

    host._check_session_candidates_breaches_locked("NIFTY")

    assert getattr(trade, "_contract_tick_cursor", 0) == 0
    assert trade.action == "ENTRY"


def test_missing_native_state_does_not_claim_ticks_were_evaluated(monkeypatch):
    host, trade = manual_host(monkeypatch)
    trade.utema_state = {}

    host._check_session_candidates_breaches_locked("NIFTY")

    assert getattr(trade, "_contract_tick_cursor", 0) == 0


def test_failed_evaluation_then_failed_read_keeps_stop_tick_for_successful_retry(monkeypatch):
    from tests.test_ut_ema_execution_isolation_2026_10_29 import _monitor
    host, trade, exits = _monitor(monkeypatch)
    trade.live_quote_stale = True
    trade.quote_verified = False
    trade.symbol_token = "A"
    host._option_premium_quote_plausible = lambda *_args: True
    host._trade_contract_tick_path = Scanner._trade_contract_tick_path.__get__(host)
    host._commit_trade_contract_tick_path = Scanner._commit_trade_contract_tick_path
    host.data = MarketDataProvider.__new__(MarketDataProvider)
    host.data._record_contract_tick("A", 85.0, time.time() - 10)
    reader = host.data.get_contract_ticks_since
    evaluate = ut_ema_lane.exit_decision
    def fail_evaluation(*args, **kwargs):
        raise RuntimeError("native evaluation unavailable")
    monkeypatch.setattr(ut_ema_lane, "exit_decision", fail_evaluation)
    with pytest.raises(RuntimeError, match="native evaluation unavailable"):
        host._check_session_candidates_breaches_locked("NIFTY")
    assert trade._contract_tick_pending_cursor == 1

    def fail_read(*args, **kwargs):
        raise RuntimeError("tick history unavailable")
    monkeypatch.setattr(ut_ema_lane, "exit_decision", evaluate)
    monkeypatch.setattr(host.data, "get_contract_ticks_since", fail_read)
    host._check_session_candidates_breaches_locked("NIFTY")
    assert getattr(trade, "_contract_tick_cursor", 0) == 0
    assert not exits

    monkeypatch.setattr(host.data, "get_contract_ticks_since", reader)
    host._check_session_candidates_breaches_locked("NIFTY")
    assert len(exits) == 1
    assert exits[0].exit_reason == "UTEMA_PREMIUM_STOP"
    assert exits[0].exit_price == 85.0
    assert trade._contract_tick_cursor == 1
