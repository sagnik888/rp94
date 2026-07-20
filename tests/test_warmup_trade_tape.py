import asyncio
import collections
from datetime import datetime
from types import SimpleNamespace

import scanner as scanner_module
from scanner import Scanner


def test_log_event_suppresses_trade_only_while_warmup():
    s = Scanner.__new__(Scanner)
    s.activity_log = collections.deque(maxlen=10)
    s._persist_rejected_signal_event = lambda *a, **k: None

    # During warmup, "trade" lifecycle events are intentionally muted.
    s.is_warmup = True
    s.log_event("Signal rejected: BANKNIFTY SELL 5min", "trade")
    assert len(s.activity_log) == 0

    # Once live (warmup cleared), the same event reaches the live tape.
    s.is_warmup = False
    s.log_event("Signal rejected: BANKNIFTY SELL 5min", "trade")
    assert len(s.activity_log) == 1


def test_initial_setup_clears_warmup_even_if_preset_true():
    # Reproduces the bug: scan_count==1 pre-sets is_warmup=True before the
    # background setup task runs; the task must leave it False afterwards so the
    # live tape is not muted for the whole session.
    s = Scanner.__new__(Scanner)
    s.is_warmup = True
    s.mode = "HISTORICAL"
    s.active_chart_instrument = "NIFTY"
    s.active_chart_tf = "5min"
    s._initial_setup_completed = False
    s._initial_setup_task = "task"
    s._option_history_bundle_status = None
    s.trades = SimpleNamespace(open_trades={}, closed_trades=[])
    s.log_event = lambda *a, **k: None

    async def _fetch_all_data(*a, **k):
        return None

    async def _sync_intel(*a, **k):
        return None

    s._fetch_all_data = _fetch_all_data
    s._sync_intelligence_history = _sync_intel
    s.signal_processor = SimpleNamespace(
        _option_history_store=SimpleNamespace(ensure_bundle=lambda: {})
    )
    s.data = SimpleNamespace(
        is_market_open=lambda: False,
        get_previous_close=lambda name: 0.0,
    )

    asyncio.run(s._background_initial_setup({}))

    assert s.is_warmup is False
    assert s._initial_setup_completed is True


def test_real_mid_session_initial_setup_runs_live_startup_refresh(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 6, 16, 10, 30)
            return tz.localize(base) if tz else base

    monkeypatch.setattr(scanner_module, "datetime", FixedDateTime)

    s = Scanner.__new__(Scanner)
    s.is_warmup = True
    s.mode = "REAL"
    s.active_chart_instrument = "NIFTY"
    s.active_chart_tf = "5min"
    s._initial_setup_completed = False
    s._initial_setup_task = "task"
    s._option_history_bundle_status = None
    s._morning_refresh_last_date = None
    s._morning_refresh_inflight = False
    s.trades = SimpleNamespace(open_trades={}, closed_trades=[])
    s.log_event = lambda *a, **k: None
    fetch_calls = []
    refresh_calls = []
    sync_calls = []

    async def _fetch_all_data(indices, **kwargs):
        fetch_calls.append((indices, kwargs))

    async def _morning_market_refresh(indices, session_day, *, force_full=False, reason="scheduled"):
        refresh_calls.append((indices, session_day, force_full, reason))
        s._morning_refresh_last_date = session_day
        s._morning_refresh_inflight = False

    async def _sync_intel(*a, **k):
        sync_calls.append((a, k))

    s._fetch_all_data = _fetch_all_data
    s._morning_market_refresh = _morning_market_refresh
    s._sync_intelligence_history = _sync_intel
    s.signal_processor = SimpleNamespace(
        _option_history_store=SimpleNamespace(ensure_bundle=lambda: {})
    )
    s.data = SimpleNamespace(
        is_market_open=lambda dt=None: True,
        is_market_holiday=lambda dt: False,
        is_special_trading_session=lambda dt: False,
        get_previous_close=lambda name: 0.0,
    )

    asyncio.run(s._background_initial_setup({"NIFTY": {"token": "99926000", "exchange": "NSE"}}))

    assert fetch_calls == [
        (
            {"NIFTY": {"token": "99926000", "exchange": "NSE"}},
            {"force": True, "timeframes": ["5min"], "days_back_override": 1},
        )
    ]
    assert refresh_calls == [
        ({"NIFTY": {"token": "99926000", "exchange": "NSE"}}, "2026-06-16", True, "startup")
    ]
    assert sync_calls == []
    assert s.is_warmup is False
    assert s._initial_setup_completed is True
