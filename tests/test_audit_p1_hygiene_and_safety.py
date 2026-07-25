"""P1 audit fixes: shadow-free dashboard accounting, config bounds, SQLite isolation, WS cap."""

from __future__ import annotations

import json
import os
from datetime import datetime
from types import SimpleNamespace

import pytest

from dashboard.server import (
    _cap_ws_payload,
    _valid_command_payload,
    _NUMERIC_FIELD_BOUNDS,
)
from engine.trade_row_adapter import hygiene_closed_rows, is_futures_shadow_row
from trading.trade_manager import IST, Trade, TradeManager


def _trade(tid, pnl, inst_type="FUT", symbol="BANKNIFTY-FUT", entry=None, **kw):
    return Trade(
        id=tid,
        instrument="BANKNIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        entry_time=entry or IST.localize(datetime(2026, 7, 8, 13, 35)),
        trailing_stop=99.0,
        current_stop=99.0,
        lots=1,
        lot_size=35,
        grade="A",
        inst_type=inst_type,
        trading_symbol=symbol,
        pnl=pnl,
        status="CLOSED",
        **kw,
    )


# ── Accounting hygiene ───────────────────────────────────────────────────────


def test_hygiene_excludes_shadow_and_dedups_dicts():
    rows = [
        {"id": "1", "instrument": "NIFTY", "timeframe": "5min", "direction": "LONG",
         "inst_type": "OPT", "trading_symbol": "NIFTYCE", "pnl": 1000,
         "entry_time": "2026-07-08T13:35:00"},
        {"id": "1_FUT", "instrument": "NIFTY", "timeframe": "5min", "direction": "LONG",
         "inst_type": "FUT", "trading_symbol": "FUT_SHADOW", "pnl": 500,
         "grade": "A (Hist, Fut Shadow)", "entry_time": "2026-07-08T13:35:00"},
        {"id": "2a", "instrument": "NIFTY", "timeframe": "5min", "direction": "LONG",
         "inst_type": "OPT", "trading_symbol": "NIFTYCE", "pnl": 200,
         "entry_time": "2026-07-08T14:00:00"},
        {"id": "2b", "instrument": "NIFTY", "timeframe": "5min", "direction": "LONG",
         "inst_type": "OPT", "trading_symbol": "NIFTYCE", "pnl": 200,
         "entry_time": "2026-07-08T14:00:00"},
    ]
    head, tel = hygiene_closed_rows(rows)
    assert len(head) == 2
    assert sum(float(r["pnl"]) for r in head) == 1200
    assert tel["futures_shadow"]["rows"] == 1
    assert tel["engine_replay"]["excess_rows"] == 1
    assert is_futures_shadow_row(rows[1]) is True


def test_hygiene_handles_blank_instrument_and_keeps_distinct_symbols():
    rows = [
        {"id": "blank", "instrument": "", "timeframe": "5min", "direction": "LONG",
         "inst_type": "OPT", "trading_symbol": "", "pnl": 10,
         "entry_time": "2026-07-08T10:00:00"},
        {"id": "a", "instrument": "NIFTY", "timeframe": "5min", "direction": "LONG",
         "inst_type": "OPT", "trading_symbol": "NIFTY01CE", "pnl": 100,
         "entry_time": "2026-07-08T10:01:00", "exit_price": 10},
        {"id": "b", "instrument": "NIFTY", "timeframe": "5min", "direction": "LONG",
         "inst_type": "OPT", "trading_symbol": "NIFTY02CE", "pnl": 100,
         "entry_time": "2026-07-08T10:01:00", "exit_price": 20},
    ]
    headline, telemetry = hygiene_closed_rows(rows)
    assert len(headline) == 3
    assert telemetry["engine_replay"]["excess_rows"] == 0


def test_dashboard_rebuild_matches_tm_summary_after_hygiene():
    """Regression: rebuild_display_accounting must not re-inflate shadow PnL."""
    tm = TradeManager(broker=None, max_positions=5)
    real = _trade("H_OPT", 1000.0, inst_type="OPT", symbol="BNCE")
    shadow = _trade("H_OPT_FUT", 24184.0, inst_type="FUT", symbol="FUT_SHADOW")
    dup = _trade("H_OPT_dup", 1000.0, inst_type="OPT", symbol="BNCE")
    s = tm.get_summary(filtered_trades=[real, shadow, dup], open_trades=[])
    assert s["daily_pnl"] == 1000
    assert s["futures_shadow"]["rows"] == 1

    # Dict path used by dashboard rebuild
    rows = [
        {"id": t.id, "instrument": t.instrument, "timeframe": t.timeframe,
         "direction": t.direction, "inst_type": t.inst_type,
         "trading_symbol": t.trading_symbol, "pnl": t.pnl,
         "entry_time": str(t.entry_time)[:19], "grade": t.grade}
        for t in (real, shadow, dup)
    ]
    head, tel = hygiene_closed_rows(rows)
    assert sum(float(r["pnl"]) for r in head) == float(s["daily_pnl"])
    assert tel["futures_shadow"]["rows"] == s["futures_shadow"]["rows"]


def test_get_dashboard_payload_closed_excludes_shadow():
    tm = TradeManager(broker=None, max_positions=5)
    real = _trade("H1", 500.0, inst_type="OPT", symbol="NIFTYCE")
    shadow = _trade("H1_FUT", 900.0, inst_type="FUT", symbol="FUT_SHADOW")
    shadow.grade = "A (Hist, Fut Shadow)"
    tm.closed_trades = [real, shadow]
    tm.open_trades = {}
    # Force historical path: use is_historical with backtest_days large
    payload = tm.get_dashboard_payload(is_historical=True, backtest_days=30, inst_pref="AUTO")
    closed = payload.get("closed") or []
    assert all(str(r.get("trading_symbol") or "").upper() != "FUT_SHADOW" for r in closed)
    # Summary must exclude shadow PnL
    assert float(payload["summary"]["daily_pnl"]) == 500.0


# ── Config bounds ────────────────────────────────────────────────────────────


def test_configure_rejects_absurd_risk_values():
    assert _valid_command_payload({"cmd": "configure", "futures_sl_pct": 0}) == "out_of_range:futures_sl_pct"
    assert _valid_command_payload({"cmd": "configure", "futures_sl_pct": 1000}) == "out_of_range:futures_sl_pct"
    assert _valid_command_payload({"cmd": "configure", "options_sl_pct": 1000}) == "out_of_range:options_sl_pct"
    assert _valid_command_payload({"cmd": "configure", "max_trades_per_index": 1_000_000}) == "out_of_range:max_trades_per_index"
    assert _valid_command_payload({"cmd": "configure", "futures_sl_pct": 0.05}) is None
    assert _valid_command_payload({"cmd": "configure", "max_trades_per_index": 1.9}) == "invalid_integer:max_trades_per_index"


def test_configure_rejects_bad_active_indices():
    assert _valid_command_payload({"cmd": "configure", "active_indices": []}) == "invalid_active_indices"
    assert _valid_command_payload({"cmd": "configure", "active_indices": "NIFTY"}) == "invalid_active_indices"
    assert _valid_command_payload({"cmd": "configure", "active_indices": [1, 2]}) == "invalid_active_indices"
    assert _valid_command_payload({"cmd": "configure", "active_indices": ["FOO"]}) == "invalid_active_index:FOO"
    msg = {"cmd": "configure", "active_indices": ["nifty", "NIFTY", "BANKNIFTY"]}
    assert _valid_command_payload(msg) is None
    assert msg["active_indices"] == ["NIFTY", "BANKNIFTY"]  # dedup + upper


def test_numeric_bounds_table_covers_core_risk_fields():
    for key in ("futures_sl_pct", "options_sl_pct", "max_trades_per_index", "volume_spike_entry_stabilization_seconds"):
        assert key in _NUMERIC_FIELD_BOUNDS


# ── SQLite isolation ─────────────────────────────────────────────────────────


def test_sqlite_uses_test_db_path_under_pytest():
    from data.sqlite_db import DatabaseManager, _resolve_db_path, _running_under_pytest

    assert _running_under_pytest() is True
    path = str(_resolve_db_path())
    assert "utbot" in path.lower() or "pytest" in path.lower() or "tmp" in path.lower()
    # Must not point at production data_store when tests run
    assert not path.replace("\\", "/").endswith("data_store/utbot_data.db") or "pytest" in path
    dm = DatabaseManager()
    assert "pytest" in str(dm.db_path).lower() or os.environ.get("UTBOT_TEST_DB_PATH") in str(dm.db_path)


# ── WS payload cap ───────────────────────────────────────────────────────────


def test_ws_cap_truncates_oversized_payload():
    huge_closed = [
        {"id": f"T{i}", "pnl": 1.0, "instrument": "NIFTY", "reasons": ["x" * 200]}
        for i in range(5000)
    ]
    payload = {
        "instruments": {
            "NIFTY": {"chart": {"1min": {"state": {"z": "y" * 1000}}}},
        },
        "trades": {"closed": huge_closed, "open": [], "signals": list(range(500)), "summary": {}},
        "equity_curve": [{"equity": i} for i in range(5000)],
        "analytics": {"by_grade": {str(i): i for i in range(200)}},
    }
    capped = _cap_ws_payload(payload)
    raw = json.dumps({"type": "full_update", "data": capped}, default=str)
    assert len(raw.encode("utf-8")) <= 900_000
    assert capped.get("ws_truncated") is True or len((capped.get("trades") or {}).get("closed") or []) <= 40


def test_ws_cap_has_hard_final_fallback():
    capped = _cap_ws_payload({"blob": "x" * 1_200_000})
    raw = json.dumps({"type": "full_update", "data": capped}, default=str)
    assert len(raw.encode("utf-8")) <= 900_000
    assert capped["ws_truncated"] is True
    assert capped["ws_payload_omitted"] is True


def test_ws_cap_windows_long_session_candidates_but_keeps_live_instrument_truth():
    candidates = [
        {
            "id": f"SIG-{i}",
            "status": "TRADE SIGNAL",
            "action": "ENTRY",
            "current_stop": 99.0,
            "intelligence_score": 0.72,
            "market_intelligence_context": {"history": "x" * 4_000},
        }
        for i in range(150)
    ]
    payload = {
        "mode": "REAL",
        "scan_count": 2_000,
        "instruments": {
            name: {
                "ltp": 25_000.0,
                "trade_state": {"open_mtm": 125.0, "current_stop": 99.0},
                "signals": [{"id": f"ACTIVE-{name}", "direction": "LONG"}],
                "intelligence": {"aggregate": {"score": 0.72, "bias": "BULLISH"}},
                "chart": {"5min": {"state": {"trailing_stop": 99.0}}},
                "trade_candidates": candidates,
            }
            for name in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
        },
        "trades": {
            "open": [{"id": "OPEN-1", "current_stop": 99.0}],
            "closed": [],
            "signals": [],
            "summary": {"open_count": 1},
        },
    }

    capped = _cap_ws_payload(payload)
    raw = json.dumps({"type": "full_update", "data": capped}, default=str)

    assert len(raw.encode("utf-8")) <= 900_000
    assert capped["ws_truncated"] is True
    assert capped.get("ws_payload_omitted") is not True
    assert capped["trades"]["open"][0]["current_stop"] == 99.0
    for instrument in capped["instruments"].values():
        assert instrument["ltp"] == 25_000.0
        assert instrument["trade_state"]["current_stop"] == 99.0
        assert instrument["signals"][0]["direction"] == "LONG"
        assert instrument["intelligence"]["aggregate"]["score"] == 0.72
        assert instrument["chart"]["5min"]["state"]["trailing_stop"] == 99.0
        assert len(instrument["trade_candidates"]) == 20
        assert instrument["trade_candidates_total"] == 150
        assert instrument["trade_candidates_truncated_for_ws"] is True
        assert instrument["trade_candidates"][0]["current_stop"] == 99.0


# ── Stale-while-busy ─────────────────────────────────────────────────────────


def test_busy_scanner_not_critical_stale():
    from engine.scanner_telemetry import ScannerTelemetryMixin
    from datetime import timedelta
    import pytz

    class Stub(ScannerTelemetryMixin):
        pass

    st = Stub.__new__(Stub)
    st.mode = "HISTORICAL"
    st.is_calculating = True
    st._calculation_lock = True
    st.last_scan_time = datetime.now(pytz.timezone("Asia/Kolkata")) - timedelta(seconds=120)
    st._active_scan_progress_at = datetime.now(pytz.timezone("Asia/Kolkata")) - timedelta(seconds=30)
    st._recalculation_status = {"status": "worker_running", "last_progress_at": None}
    st.dashboard_cache = None
    st._diagnostics = {}
    st.latest_results = {}
    st.data = SimpleNamespace(get_source_health=lambda: {})
    # Isolate the busy-vs-stale classification under test from live host resource
    # pressure: _system_performance_payload folds real RAM/disk/CPU metrics into its
    # WARN status, which made this assertion flaky (host load flipped BUSY->WARN).
    st._get_system_metrics_payload = lambda: {}
    # freshness while busy uses raised threshold
    fresh = st._scanner_freshness_payload()
    assert fresh["stale_after_seconds"] >= 60
    assert fresh["is_stale"] is False
    health = st._system_performance_payload()
    assert health["status"] == "BUSY"
    assert any(i["key"] == "scanner_busy" and i["severity"] == "info" for i in health["issues"])
    assert not any(i["key"] == "scanner_stale" for i in health["issues"])

    st.mode = "REAL"
    st._recalculation_status = {"status": "idle", "last_progress_at": None}
    st._active_scan_progress_at = None
    live_fresh = st._scanner_freshness_payload()
    assert live_fresh["stale_after_seconds"] < 120
    assert live_fresh["is_stale"] is True


def test_numba_listed_in_requirements():
    from pathlib import Path

    req = Path("requirements.txt").read_text(encoding="utf-8").lower()
    assert "numba" in req
