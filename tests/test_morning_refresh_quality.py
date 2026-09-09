"""Morning refresh quality gate: complete thresholds, partial re-warm, 09:15 catch-up."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import scanner as scanner_module
from scanner import Scanner


def _clock_settings(**overrides):
    base = dict(
        real_mode_premarket_arm_start="09:00:00",
        real_mode_premarket_settle_end="09:08:00",
        real_mode_premarket_refresh_end="09:14:30",
        real_mode_live_start="09:15:00",
        morning_refresh_require_history=True,
        morning_refresh_require_expiry=True,
        morning_refresh_require_all_quotes=True,
        morning_refresh_require_all_chains=True,
        morning_refresh_require_vix=False,
        morning_refresh_catchup_at_live_open=True,
        morning_refresh_max_attempts=3,
        morning_refresh_parallelism=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _scanner(market_open=False):
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc.active_indices = ["NIFTY", "BANKNIFTY"]
    sc.config = {"indices": {"NIFTY": {}, "BANKNIFTY": {}}}
    sc._morning_refresh_last_date = None
    sc._morning_refresh_inflight = False
    sc._morning_refresh_summary = {}
    sc._morning_refresh_attempts = {}
    sc._morning_refresh_catchup_done_date = None
    sc.market_info = {}
    sc._premium_cache = {}
    sc._last_full_history_refresh = None
    sc.data = SimpleNamespace(
        is_special_trading_session=lambda dt: False,
        is_market_holiday=lambda dt: False,
        is_market_open=lambda dt=None: market_open,
    )
    sc.log_event = lambda *a, **k: None
    return sc


def _complete_summary(session_day="2026-06-16", n=2):
    return {
        "session_day": session_day,
        "history": True,
        "expiry": True,
        "index_quotes": n,
        "chains": n,
        "vix": True,
        "futures": n,
        "option_tokens": n * 2,
        "option_ltp": n * 2,
        "intel": n,
        "indices": {
            "NIFTY": {"quote": True, "chain": True, "intel": True, "fut": True},
            "BANKNIFTY": {"quote": True, "chain": True, "intel": True, "fut": True},
        },
        "missing_keys": [],
        "complete": True,
    }


def _incomplete_summary(session_day="2026-06-16"):
    return {
        "session_day": session_day,
        "history": True,
        "expiry": True,
        "index_quotes": 1,
        "chains": 0,
        "vix": False,
        "futures": 1,
        "option_tokens": 0,
        "option_ltp": 0,
        "intel": 0,
        "indices": {
            "NIFTY": {"quote": True, "chain": False, "intel": False, "fut": True},
            "BANKNIFTY": {"quote": False, "chain": False, "intel": False, "fut": False},
        },
        "missing_keys": ["quotes", "chains", "quote:BANKNIFTY", "chain:NIFTY", "chain:BANKNIFTY"],
        "complete": False,
    }


# ─── 1) Quality gate: only mark complete when thresholds pass ───────────────


def test_evaluate_quality_complete_when_thresholds_met(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _clock_settings)
    sc = _scanner()
    ok, missing = sc._evaluate_morning_refresh_quality(_complete_summary(), 2)
    assert ok is True
    assert missing == []


def test_evaluate_quality_fails_on_missing_history_expiry_quotes_chains(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _clock_settings)
    sc = _scanner()
    summary = {
        "history": False,
        "expiry": False,
        "index_quotes": 0,
        "chains": 0,
        "vix": False,
        "indices": {
            "NIFTY": {"quote": False, "chain": False},
            "BANKNIFTY": {"quote": False, "chain": False},
        },
    }
    ok, missing = sc._evaluate_morning_refresh_quality(summary, 2)
    assert ok is False
    assert "history" in missing
    assert "expiry" in missing
    assert "quotes" in missing
    assert "chains" in missing
    assert "quote:NIFTY" in missing
    assert "chain:BANKNIFTY" in missing


def test_evaluate_quality_vix_optional_by_default(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _clock_settings)
    sc = _scanner()
    summary = _complete_summary()
    summary["vix"] = False
    ok, missing = sc._evaluate_morning_refresh_quality(summary, 2)
    assert ok is True
    assert "vix" not in missing


def test_evaluate_quality_vix_required_when_enabled(monkeypatch):
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: _clock_settings(morning_refresh_require_vix=True),
    )
    sc = _scanner()
    summary = _complete_summary()
    summary["vix"] = False
    ok, missing = sc._evaluate_morning_refresh_quality(summary, 2)
    assert ok is False
    assert "vix" in missing


def test_incomplete_refresh_does_not_set_last_date(monkeypatch):
    """Core hardening #1: incomplete must NOT mark the day complete."""
    monkeypatch.setattr(scanner_module, "get_settings", _clock_settings)

    sc = _scanner()
    sc.active_indices = ["NIFTY"]
    sc.config = {"indices": {"NIFTY": {"token": "1", "exchange": "NSE", "strike_interval": 50}}}
    sc.expiry = SimpleNamespace(pre_market_check=lambda: {})
    sc.candles = SimpleNamespace(
        update_latest_price=lambda *a, **k: None,
        get_latest_price=lambda n: 0.0,
        get_candles=lambda *a, **k: None,
    )
    sc.intel = SimpleNamespace(analyze=lambda **k: None)
    sc._get_option_chain_cached = lambda *a, **k: (None, {})
    sc._live_analysis_window = lambda df, tf: df
    sc._regime_history_ready = lambda df: False
    sc._fetch_all_data = AsyncMock(return_value=None)

    # Quotes fail → incomplete
    sc.data.get_index_quote = lambda name: {"ltp": 0}
    sc.data.get_previous_close = lambda name: 0.0
    sc.data.get_ltp = lambda *a, **k: 0
    sc.data.get_latest_price_by_token = lambda *a, **k: 0
    sc.data.get_option_token = lambda *a, **k: None

    asyncio.run(
        sc._morning_market_refresh(
            {"NIFTY": sc.config["indices"]["NIFTY"]},
            "2026-06-16",
            force_full=True,
            reason="test",
        )
    )

    assert sc._morning_refresh_last_date is None
    summary = sc._morning_refresh_summary
    assert summary.get("complete") is False
    assert summary.get("session_day") == "2026-06-16"
    assert "missing_keys" in summary
    assert len(summary["missing_keys"]) > 0
    assert sc._morning_refresh_inflight is False


def test_complete_refresh_sets_last_date(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _clock_settings)

    sc = _scanner()
    sc.active_indices = ["NIFTY"]
    sc.config = {"indices": {"NIFTY": {"token": "1", "exchange": "NSE", "strike_interval": 50}}}
    sc.expiry = SimpleNamespace(pre_market_check=lambda: {"NIFTY": {}})
    sc.candles = SimpleNamespace(
        update_latest_price=lambda *a, **k: None,
        get_latest_price=lambda n: 25000.0,
        get_candles=lambda *a, **k: None,
    )
    sc.intel = SimpleNamespace(analyze=lambda **k: None)
    # Non-empty chain mock
    fake_chain = MagicMock()
    fake_chain.empty = False
    sc._get_option_chain_cached = lambda *a, **k: (fake_chain, {})
    sc._live_analysis_window = lambda df, tf: df
    sc._regime_history_ready = lambda df: False
    sc._fetch_all_data = AsyncMock(return_value=None)
    sc.data.get_index_quote = lambda name: {"ltp": 25000.0}
    sc.data.get_previous_close = lambda name: 24900.0
    sc.data.get_ltp = lambda *a, **k: 12.5
    sc.data.get_latest_price_by_token = lambda *a, **k: 25000.0
    sc.data.get_option_token = lambda *a, **k: {"symbol": "NIFTY25000CE", "token": "99"}

    asyncio.run(
        sc._morning_market_refresh(
            {"NIFTY": sc.config["indices"]["NIFTY"]},
            "2026-06-16",
            force_full=True,
            reason="test",
        )
    )

    assert sc._morning_refresh_last_date == "2026-06-16"
    assert sc._morning_refresh_summary.get("complete") is True
    assert sc._morning_refresh_summary.get("missing_keys") == []
    assert sc._morning_refresh_inflight is False
    sc._fetch_all_data.assert_awaited_once()
    assert sc._fetch_all_data.await_args.kwargs["timeframes"] == ["1min"]


def test_readiness_wait_path_publishes_scanner_heartbeat():
    source = inspect.getsource(Scanner._scan_cycle)
    readiness_branch = source.split(
        'if self.mode == "REAL" and not cycle_is_warmup and self._ensure_market_session_refresh',
        1,
    )[1].split("# ══ DAILY DATA MAINTENANCE", 1)[0]

    assert "self.last_scan_time = datetime.now(IST)" in readiness_branch
    assert '"market_status": "READINESS"' in readiness_branch
    assert "self._update_dashboard_cache(results)" in readiness_branch


# ─── 2) Store summary + partial re-warm missing keys only ───────────────────


def test_partial_refresh_skips_already_good_index(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _clock_settings)

    sc = _scanner()
    sc.active_indices = ["NIFTY", "BANKNIFTY"]
    sc.config = {
        "indices": {
            "NIFTY": {"token": "1", "exchange": "NSE", "strike_interval": 50},
            "BANKNIFTY": {"token": "2", "exchange": "NSE", "strike_interval": 100},
        }
    }
    # Prior incomplete: NIFTY good quote+chain; BANKNIFTY missing both
    sc._morning_refresh_summary = {
        "session_day": "2026-06-16",
        "history": True,
        "expiry": True,
        "index_quotes": 1,
        "chains": 1,
        "vix": True,
        "futures": 1,
        "option_tokens": 2,
        "option_ltp": 2,
        "intel": 1,
        "indices": {
            "NIFTY": {"quote": True, "chain": True, "intel": True, "fut": True},
            "BANKNIFTY": {"quote": False, "chain": False, "intel": False, "fut": False},
        },
        "missing_keys": ["quotes", "chains", "quote:BANKNIFTY", "chain:BANKNIFTY"],
        "complete": False,
    }
    sc.expiry = SimpleNamespace(pre_market_check=lambda: (_ for _ in ()).throw(AssertionError("expiry should skip")))
    sc.candles = SimpleNamespace(
        update_latest_price=lambda *a, **k: None,
        get_latest_price=lambda n: 55000.0 if n == "BANKNIFTY" else 25000.0,
        get_candles=lambda *a, **k: None,
    )
    sc.intel = SimpleNamespace(analyze=lambda **k: None)
    fake_chain = MagicMock()
    fake_chain.empty = False
    chain_calls = []

    def _chain(name, *a, **k):
        chain_calls.append(name)
        return (fake_chain, {})

    sc._get_option_chain_cached = _chain
    sc._live_analysis_window = lambda df, tf: df
    sc._regime_history_ready = lambda df: False

    history_calls = []

    async def _fetch(*a, **k):
        history_calls.append(1)
        raise AssertionError("history already ok — should not re-fetch on pure quote/chain partial")

    sc._fetch_all_data = _fetch  # should not be called (history not in missing)

    quote_calls = []

    def _quote(name):
        quote_calls.append(name)
        return {"ltp": 55000.0}

    sc.data.get_index_quote = _quote
    sc.data.get_previous_close = lambda name: 54000.0
    sc.data.get_ltp = lambda *a, **k: 20.0
    sc.data.get_latest_price_by_token = lambda *a, **k: 55000.0
    sc.data.get_option_token = lambda *a, **k: {"symbol": "X", "token": "1"}

    asyncio.run(
        sc._morning_market_refresh(
            sc.config["indices"],
            "2026-06-16",
            force_full=False,
            reason="partial_test",
        )
    )

    # Only BANKNIFTY should have been re-warmed for quotes/chains
    assert "NIFTY" not in quote_calls
    assert "BANKNIFTY" in quote_calls
    assert "NIFTY" not in chain_calls
    assert "BANKNIFTY" in chain_calls
    assert history_calls == []  # history not missing
    assert sc._morning_refresh_summary.get("partial") is True
    # After fixing BANKNIFTY, day should complete
    assert sc._morning_refresh_last_date == "2026-06-16"
    assert sc._morning_refresh_summary.get("complete") is True


def test_schedule_partial_when_incomplete_at_live(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _clock_settings)
    sc = _scanner(market_open=True)
    sc._morning_refresh_summary = _incomplete_summary()
    sc._morning_refresh_catchup_done_date = "2026-06-16"  # catch-up already used
    scheduled = []

    def _sched(indices, now, *, reason="scheduled", force_full=False):
        scheduled.append({"reason": reason, "force_full": force_full})

    sc._schedule_morning_market_refresh = _sched

    blocked = sc._ensure_market_session_refresh(
        {"NIFTY": {}, "BANKNIFTY": {}},
        datetime(2026, 6, 16, 10, 0, 0),
    )
    # Mid-session partial re-warm is non-blocking: keep scanning on partial data
    # while the missing keys re-warm in the background.
    assert blocked is False
    assert len(scheduled) == 1
    assert scheduled[0]["force_full"] is False
    assert scheduled[0]["reason"] == "live_incomplete_partial"


# ─── 3) 09:15 live-open catch-up if incomplete ──────────────────────────────


def test_live_open_catchup_schedules_once_when_incomplete(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _clock_settings)
    sc = _scanner(market_open=True)
    sc._morning_refresh_summary = _incomplete_summary()
    scheduled = []
    logs = []

    def _sched(indices, now, *, reason="scheduled", force_full=False):
        scheduled.append({"reason": reason, "force_full": force_full, "now": now})

    sc._schedule_morning_market_refresh = _sched
    sc.log_event = lambda msg, event_type="info": logs.append(msg)

    t_open = datetime(2026, 6, 16, 9, 15, 0)
    assert sc._ensure_market_session_refresh({"NIFTY": {}}, t_open) is True
    assert len(scheduled) == 1
    assert scheduled[0]["reason"] == "live_open_catchup"
    assert scheduled[0]["force_full"] is False
    assert sc._morning_refresh_catchup_done_date == "2026-06-16"
    assert any("catch-up" in m.lower() or "catch-up" in m for m in logs) or any(
        "catch-up" in m for m in logs
    ) or any("Live-open catch-up" in m for m in logs)

    # Second call: catch-up already done → falls through to live incomplete
    # partial, which is now NON-BLOCKING (background re-warm, keep scanning).
    sc._morning_refresh_inflight = False
    assert sc._ensure_market_session_refresh({"NIFTY": {}}, t_open) is False
    assert len(scheduled) == 2
    assert scheduled[1]["reason"] == "live_incomplete_partial"
    assert scheduled[1]["force_full"] is False


def test_live_open_catchup_skipped_when_complete(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _clock_settings)
    sc = _scanner(market_open=True)
    sc._morning_refresh_last_date = "2026-06-16"
    sc._morning_refresh_summary = _complete_summary()
    scheduled = []
    sc._schedule_morning_market_refresh = lambda *a, **k: scheduled.append(1)

    assert sc._ensure_market_session_refresh({"NIFTY": {}}, datetime(2026, 6, 16, 9, 15, 0)) is False
    assert scheduled == []


def test_live_open_catchup_can_be_disabled(monkeypatch):
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: _clock_settings(morning_refresh_catchup_at_live_open=False),
    )
    sc = _scanner(market_open=True)
    sc._morning_refresh_summary = _incomplete_summary()
    scheduled = []

    def _sched(indices, now, *, reason="scheduled", force_full=False):
        scheduled.append(reason)

    sc._schedule_morning_market_refresh = _sched

    sc._ensure_market_session_refresh({"NIFTY": {}}, datetime(2026, 6, 16, 9, 15, 0))
    assert "live_open_catchup" not in scheduled
    assert "live_incomplete_partial" in scheduled


def test_premarket_partial_uses_force_full_false_when_incomplete(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", _clock_settings)
    sc = _scanner(market_open=False)
    sc._morning_refresh_summary = _incomplete_summary()
    scheduled = []

    def _sched(indices, now, *, reason="scheduled", force_full=False):
        scheduled.append({"reason": reason, "force_full": force_full})

    sc._schedule_morning_market_refresh = _sched

    assert sc._ensure_market_session_refresh({"NIFTY": {}}, datetime(2026, 6, 16, 9, 10, 0)) is True
    assert scheduled[0]["force_full"] is False
    assert "analysis" in scheduled[0]["reason"]


# ─── Attempt cap: accept incomplete after max attempts ──────────────────────


def test_max_attempts_accepts_incomplete_and_stops_blocking(monkeypatch):
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: _clock_settings(morning_refresh_max_attempts=2),
    )
    sc = _scanner(market_open=True)
    sc._morning_refresh_summary = _incomplete_summary()
    sc._morning_refresh_attempts = {"2026-06-16": 2}
    sc._schedule_morning_market_refresh = Scanner._schedule_morning_market_refresh.__get__(sc, Scanner)

    # No event loop create_task path if we hit cap before create_task
    sc._schedule_morning_market_refresh(
        {"NIFTY": {}},
        datetime(2026, 6, 16, 10, 0, 0),
        reason="test_cap",
        force_full=False,
    )

    assert sc._morning_refresh_last_date == "2026-06-16"
    assert sc._morning_refresh_summary.get("accepted_incomplete") is True
    assert sc._morning_refresh_summary.get("complete") is False
    # Ensure no longer blocks
    assert sc._ensure_market_session_refresh({"NIFTY": {}}, datetime(2026, 6, 16, 10, 0, 0)) is False


def test_summary_for_day_isolates_other_days():
    sc = _scanner()
    sc._morning_refresh_summary = _incomplete_summary("2026-06-16")
    assert sc._morning_refresh_summary_for_day("2026-06-17") == {}
    assert sc._morning_refresh_summary_for_day("2026-06-16").get("complete") is False


def test_is_complete_for_day_uses_last_date_only():
    sc = _scanner()
    sc._morning_refresh_summary = _complete_summary()
    sc._morning_refresh_last_date = None
    assert sc._morning_refresh_is_complete_for_day("2026-06-16") is False
    sc._morning_refresh_last_date = "2026-06-16"
    assert sc._morning_refresh_is_complete_for_day("2026-06-16") is True
    assert sc._morning_refresh_is_complete_for_day("2026-06-17") is False
