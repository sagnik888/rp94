from datetime import datetime, timedelta
import asyncio
import json

from config.settings import get_settings
from dashboard import server
from engine.signal_processor import SignalProcessor
from scanner import Scanner
from trading.trade_manager import Trade, TradeManager, IST


def test_historical_state_uses_first_trade_page(monkeypatch):
    class DummyScanner:
        mode = "HISTORICAL"

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())
    payload = {
        "trades": {
            "closed": [{"id": f"c{i}"} for i in range(120)],
            "signals": [{"id": f"s{i}"} for i in range(120)],
        }
    }

    limited = server._limit_dashboard_trades(payload)

    assert len(limited["trades"]["closed"]) == 100
    assert len(limited["trades"]["signals"]) == 100
    assert limited["trades"]["window"]["limit"] == 100
    assert limited["trades"]["window"]["offset"] == 0
    assert limited["trades"]["window"]["page"] == 1
    assert limited["trades"]["window"]["page_count"] == 2
    assert limited["trades"]["window"]["total"] == 120
    assert limited["trades"]["closed_total"] == 120
    assert limited["trades"]["signals_total"] == 120


def test_live_state_still_caps_trade_rows(monkeypatch):
    class DummyScanner:
        mode = "REAL"

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())
    payload = {
        "trades": {
            "closed": [{"id": f"c{i}"} for i in range(120)],
            "signals": [{"id": f"s{i}"} for i in range(120)],
        }
    }

    limited = server._limit_dashboard_trades(payload)

    assert len(limited["trades"]["closed"]) == 80
    assert len(limited["trades"]["signals"]) == 80
    assert limited["trades"]["window"]["limit"] == 80


def test_trade_window_caps_requested_limit_to_2000(monkeypatch):
    class DummyScanner:
        mode = "HISTORICAL"

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())
    payload = {
        "closed": [{"id": f"c{i}"} for i in range(2500)],
        "signals": [{"id": f"s{i}"} for i in range(2500)],
        "summary": {"total_trades": 2500},
    }

    windowed = server._window_trades_payload(payload, limit=50000, offset=0)

    assert len(windowed["closed"]) == 2000
    assert len(windowed["signals"]) == 2000
    assert windowed["window"]["limit"] == 2000
    assert windowed["window"]["max_limit"] == 2000
    assert windowed["window"]["page_count"] == 2
    assert windowed["closed_total"] == 2500


def test_trade_window_clamps_offset_to_last_page(monkeypatch):
    class DummyScanner:
        mode = "HISTORICAL"

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())
    payload = {
        "closed": [{"id": f"c{i}"} for i in range(250)],
        "signals": [{"id": f"s{i}"} for i in range(250)],
        "summary": {"total_trades": 250},
    }

    windowed = server._window_trades_payload(payload, limit=100, offset=900)

    assert windowed["window"]["offset"] == 200
    assert windowed["window"]["page"] == 3
    assert windowed["window"]["page_count"] == 3
    assert len(windowed["closed"]) == 50
    assert windowed["closed"][0]["id"] == "c200"


def test_legacy_historical_trade_cache_without_context_is_rejected(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_concurrency_guard", False)
    monkeypatch.setattr(settings, "futures_shadow_enabled", False)
    monkeypatch.setattr(settings, "signal_grade_preference", "B")
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")
    monkeypatch.setattr(settings, "scanner_engine", "utbot_ema")

    class DummyScanner:
        mode = "HISTORICAL"
        backtest_days = 30
        inst_pref = "AUTO"

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    assert not server._cached_trades_match_current_context({
        "closed": [],
        "signals": [],
        "meta": {"mode": "HISTORICAL", "source": "historical_trade_manager"},
    })
    assert server._cached_trades_match_current_context({
        "closed": [],
        "signals": [],
        "meta": {
            "mode": "HISTORICAL",
            "backtest_days": 30,
            "complete": True,
            "inst_pref": "AUTO",
            "scanner_engine": "utbot_ema",
            "ut_concurrency_guard": False,
            "futures_shadow_enabled": "False",
            "grade_preference": "B",
            "timeframe_entry_policy": "INCLUDE_5MIN",
        },
    })

    assert not server._cached_trades_match_current_context({
        "closed": [],
        "signals": [],
        "meta": {
            "mode": "HISTORICAL",
            "backtest_days": 30,
            "complete": True,
            "inst_pref": "AUTO",
            "scanner_engine": "utbot_ema",
            "ut_concurrency_guard": False,
            "futures_shadow_enabled": "True",
            "grade_preference": "B",
            "timeframe_entry_policy": "INCLUDE_5MIN",
        },
    })
    assert not server._cached_trades_match_current_context({
        "closed": [],
        "signals": [],
        "meta": {
            "mode": "HISTORICAL",
            "backtest_days": 30,
            "complete": True,
            "inst_pref": "AUTO",
            "scanner_engine": "ema",
            "ut_concurrency_guard": False,
            "futures_shadow_enabled": "False",
            "grade_preference": "B",
            "timeframe_entry_policy": "INCLUDE_5MIN",
        },
    })


def test_truncated_historical_full_cache_is_rejected(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_concurrency_guard", False)
    monkeypatch.setattr(settings, "futures_shadow_enabled", False)
    monkeypatch.setattr(settings, "signal_grade_preference", "B")
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")
    monkeypatch.setattr(settings, "scanner_engine", "quad_prime")

    class DummyScanner:
        mode = "HISTORICAL"
        backtest_days = 30
        inst_pref = "AUTO"

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    cached = {
        "closed": [{"id": f"c{i}"} for i in range(100)],
        "signals": [],
        "summary": {"total_trades": 346},
        "meta": {
            "mode": "HISTORICAL",
            "backtest_days": 30,
            "complete": True,
            "inst_pref": "AUTO",
            "scanner_engine": "quad_prime",
            "ut_concurrency_guard": False,
            "futures_shadow_enabled": "False",
            "grade_preference": "B",
            "timeframe_entry_policy": "INCLUDE_5MIN",
        },
    }

    assert server._historical_trade_payload_rows_truncated(cached) is True
    assert server._cached_trades_match_current_context(cached) is False


def test_api_trades_prefers_latest_safe_state_over_matching_cache(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_concurrency_guard", False)
    monkeypatch.setattr(settings, "futures_shadow_enabled", False)
    monkeypatch.setattr(settings, "signal_grade_preference", "B")
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")
    monkeypatch.setattr(settings, "scanner_engine", "utbot_ema")

    meta = {
        "mode": "HISTORICAL",
        "backtest_days": 30,
        "complete": True,
        "inst_pref": "AUTO",
        "scanner_engine": "utbot_ema",
        "ut_concurrency_guard": False,
        "futures_shadow_enabled": "False",
        "grade_preference": "B",
        "timeframe_entry_policy": "INCLUDE_5MIN",
    }

    class DummyCache:
        def trades(self):
            return {
                "closed": [{"id": "cache"}],
                "signals": [{"id": "cache"}],
                "summary": {"total_trades": 1},
                "meta": meta,
            }

    class DummyScanner:
        mode = "HISTORICAL"
        backtest_days = 30
        inst_pref = "AUTO"
        dashboard_cache = DummyCache()

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())
    server.dashboard_state.latest_safe_state = {
        "mode": "HISTORICAL",
        "trades": {
            "closed": [{"id": "state-1"}, {"id": "state-2"}],
            "signals": [{"id": "state-1"}, {"id": "state-2"}],
            "summary": {"total_trades": 2},
            "meta": meta,
        },
    }

    payload = asyncio.run(server.api_trades(limit=50000, offset=0))

    assert payload["closed_total"] == 2
    assert [row["id"] for row in payload["closed"]] == ["state-1", "state-2"]


def test_api_trades_rebuilds_fresh_payload_in_real_mode(monkeypatch):
    class DummyCache:
        def trades(self):
            return {
                "closed": [{"id": "cache"}],
                "signals": [{"id": "cache"}],
                "summary": {"total_trades": 1},
                "meta": {"mode": "REAL"},
            }

    class DummyScanner:
        mode = "REAL"
        dashboard_cache = DummyCache()

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())
    monkeypatch.setattr(server, "_latest_full_trades_payload", {
        "closed": [{"id": "full-cache"}],
        "signals": [{"id": "full-cache"}],
        "summary": {"total_trades": 1},
        "meta": {"mode": "REAL"},
    })
    server.dashboard_state.latest_safe_state = {
        "mode": "REAL",
        "trades": {
            "closed": [{"id": "safe-cache"}],
            "signals": [{"id": "safe-cache"}],
            "summary": {"total_trades": 1},
            "meta": {"mode": "REAL"},
        },
    }
    fresh_calls = []

    def fresh_payload():
        fresh_calls.append(True)
        return {
            "closed": [{"id": "fresh"}],
            "signals": [{"id": "fresh"}],
            "summary": {"total_trades": 1},
            "meta": {"mode": "REAL"},
        }

    monkeypatch.setattr(server, "_build_fresh_trades_payload", fresh_payload)

    payload = asyncio.run(server.api_trades(limit=100, offset=0))

    assert fresh_calls
    assert [row["id"] for row in payload["closed"]] == ["fresh"]


def test_api_trades_skips_windowed_safe_state_for_later_pages(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_concurrency_guard", False)
    monkeypatch.setattr(settings, "futures_shadow_enabled", False)
    monkeypatch.setattr(settings, "signal_grade_preference", "B")
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")
    monkeypatch.setattr(settings, "scanner_engine", "utbot_ema")

    meta = {
        "mode": "HISTORICAL",
        "backtest_days": 30,
        "complete": True,
        "inst_pref": "AUTO",
        "scanner_engine": "utbot_ema",
        "ut_concurrency_guard": False,
        "futures_shadow_enabled": "False",
        "grade_preference": "B",
        "timeframe_entry_policy": "INCLUDE_5MIN",
    }
    full_rows = [{"id": f"full-{idx}"} for idx in range(300)]

    class DummyCache:
        def trades(self):
            return {
                "closed": full_rows,
                "signals": full_rows,
                "summary": {"total_trades": 300},
                "meta": meta,
            }

    class DummyScanner:
        mode = "HISTORICAL"
        backtest_days = 30
        inst_pref = "AUTO"
        dashboard_cache = DummyCache()

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())
    server.dashboard_state.latest_safe_state = {
        "mode": "HISTORICAL",
        "trades": {
            "closed": full_rows[:100],
            "signals": full_rows[:100],
            "closed_total": 300,
            "signals_total": 300,
            "summary": {"total_trades": 300},
            "meta": meta,
            "window": {"limit": 100, "offset": 0, "total": 300, "page_count": 3},
        },
    }

    payload = asyncio.run(server.api_trades(limit=100, offset=200))

    assert len(payload["closed"]) == 100
    assert payload["closed"][0]["id"] == "full-200"
    assert payload["window"]["offset"] == 200
    assert payload["window"]["page"] == 3


def test_api_trades_accepts_page_and_page_size_aliases(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_concurrency_guard", False)
    monkeypatch.setattr(settings, "futures_shadow_enabled", False)
    monkeypatch.setattr(settings, "signal_grade_preference", "B")
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")
    monkeypatch.setattr(settings, "scanner_engine", "utbot_ema")

    meta = {
        "mode": "HISTORICAL",
        "backtest_days": 30,
        "complete": True,
        "inst_pref": "AUTO",
        "scanner_engine": "utbot_ema",
        "ut_concurrency_guard": False,
        "futures_shadow_enabled": "False",
        "grade_preference": "B",
        "timeframe_entry_policy": "INCLUDE_5MIN",
    }
    full_rows = [{"id": f"full-{idx}"} for idx in range(300)]

    class DummyCache:
        def trades(self):
            return {
                "closed": full_rows,
                "signals": full_rows,
                "summary": {"total_trades": 300},
                "meta": meta,
            }

    class DummyScanner:
        mode = "HISTORICAL"
        backtest_days = 30
        inst_pref = "AUTO"
        dashboard_cache = DummyCache()

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())
    server.dashboard_state.latest_safe_state = {}

    payload = asyncio.run(server.api_trades(page=3, page_size=100))

    assert len(payload["closed"]) == 100
    assert payload["closed"][0]["id"] == "full-200"
    assert payload["window"]["limit"] == 100
    assert payload["window"]["offset"] == 200
    assert payload["window"]["page"] == 3


def test_incomplete_historical_trade_cache_is_rejected(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_concurrency_guard", False)
    monkeypatch.setattr(settings, "futures_shadow_enabled", False)
    monkeypatch.setattr(settings, "signal_grade_preference", "B")
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")
    monkeypatch.setattr(settings, "scanner_engine", "utbot_ema")

    class DummyScanner:
        mode = "HISTORICAL"
        backtest_days = 30
        inst_pref = "AUTO"

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    assert not server._cached_trades_match_current_context({
        "closed": [{"id": "partial"}],
        "signals": [{"id": "partial"}],
        "meta": {
            "mode": "HISTORICAL",
            "backtest_days": 30,
            "complete": False,
            "inst_pref": "AUTO",
            "scanner_engine": "utbot_ema",
            "ut_concurrency_guard": False,
            "futures_shadow_enabled": "False",
            "grade_preference": "B",
            "timeframe_entry_policy": "INCLUDE_5MIN",
        },
    })


def test_historical_building_guard_covers_scan_rebuild_gap(monkeypatch):
    class DummyScanner:
        mode = "HISTORICAL"
        scan_count = 5
        is_calculating = False
        _calculation_lock = False
        _historical_rebuild_pending = True
        _recalculation_status = {"status": "awaiting_scan"}

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    assert server._scanner_is_historical_building()


def test_historical_building_guard_covers_running_scan(monkeypatch):
    class DummyScanner:
        mode = "HISTORICAL"
        scan_count = 5
        is_calculating = True
        _calculation_lock = True
        _historical_rebuild_pending = True
        _recalculation_status = {"status": "scan_running"}

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    assert server._scanner_is_historical_building()


def test_historical_building_state_overlays_stale_completed_rows(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "scanner_engine", "ema")
    monkeypatch.setattr(settings, "ut_concurrency_guard", False)
    monkeypatch.setattr(settings, "futures_shadow_enabled", False)
    monkeypatch.setattr(settings, "signal_grade_preference", "B")
    monkeypatch.setattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN")

    class DummyScanner:
        mode = "HISTORICAL"
        backtest_days = 21
        inst_pref = "AUTO"
        scan_count = 5
        is_calculating = False
        _calculation_lock = False
        _historical_rebuild_pending = True
        _recalculation_status = {"status": "queued"}
        simulation_id = 4242

        def get_broadcast_config(self):
            return {"scanner_engine": "ema"}

        def _get_diagnostics_payload(self):
            return {"recalculation": {"status": "queued"}}

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    payload = server._historical_building_state({
        "mode": "HISTORICAL",
        "config": {"scanner_engine": "utbot"},
        "trades": {
            "closed": [{"id": "old-engine-row"}],
            "signals": [{"id": "old-engine-row"}],
            "meta": {"complete": True},
        },
    })

    assert payload["config"]["scanner_engine"] == "ema"
    assert payload["simulation_id"] == 4242
    assert payload["trades"]["closed"] == []
    assert payload["trades"]["signals"] == []
    assert payload["trades"]["meta"]["building"] is True
    assert payload["trades"]["meta"]["scanner_engine"] == "ema"


def test_diagnostics_bypasses_cache_during_historical_building(monkeypatch):
    class DummyCache:
        def diagnostics(self):
            return {
                "recalculation": {"status": "idle"},
                "simulation": {"backtest_days": 1},
            }

    class DummyScanner:
        mode = "HISTORICAL"
        scan_count = 5
        is_calculating = False
        _calculation_lock = False
        _historical_rebuild_pending = True
        _recalculation_status = {"status": "awaiting_scan"}
        dashboard_cache = DummyCache()

        def _get_diagnostics_payload(self):
            return {
                "recalculation": {"status": "awaiting_scan"},
                "simulation": {"backtest_days": 30},
            }

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    payload = asyncio.run(server.api_diagnostics())

    assert payload["recalculation"]["status"] == "awaiting_scan"
    assert payload["simulation"]["backtest_days"] == 30


def test_api_state_retargets_cached_engine_lab_active_row(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "scanner_engine", "utbot_consensus")
    monkeypatch.setattr(server.dashboard_state, "latest_safe_state", None)

    class DummyCache:
        def state(self):
            return {
                "mode": "HISTORICAL",
                "config": {"scanner_engine": "consensus"},
                "engine_lab": {
                    "active_engine": "consensus",
                    "rows": [
                        {"engine": "consensus", "active": True, "signals": 76, "total_money": 8645.9},
                        {
                            "engine": "utbot_consensus",
                            "active": False,
                            "signals": 171,
                            "open": 7,
                            "open_rows": [{"instrument": "NIFTY", "pnl_points": -12.5}],
                            "net_points": -1536.53,
                            "open_points": 189.04,
                            "total_points": -1347.49,
                            "gross_gain": 900.52,
                            "gross_loss": 2437.05,
                            "total_money": -1699.15,
                        },
                    ],
                },
                "trades": {
                    "meta": {"mode": "HISTORICAL"},
                    "summary": {
                        "total_trades": 55,
                        "open_count": 0,
                        "wins": 37,
                        "losses": 18,
                        "win_rate": 67.3,
                        "daily_pnl": 27641.44,
                        "profit_factor": 3.37,
                    },
                },
            }

    class DummyScanner:
        mode = "HISTORICAL"
        scan_count = 5
        is_calculating = False
        _calculation_lock = False
        _historical_rebuild_pending = False
        _recalculation_status = {"status": "idle"}
        chart_stream_enabled = True
        dashboard_cache = DummyCache()

        def _sync_engine_lab_active_row(self, payload, trade_summary=None):
            return Scanner._sync_engine_lab_active_row(self, payload, trade_summary)

    monkeypatch.setattr(server, "scanner_ref", DummyScanner())

    response = asyncio.run(server.api_state())
    payload = json.loads(response.body)

    rows = {row["engine"]: row for row in payload["engine_lab"]["rows"]}
    assert payload["engine_lab"]["active_engine"] == "utbot_consensus"
    assert rows["consensus"]["active"] is False
    assert rows["utbot_consensus"]["active"] is True
    assert rows["utbot_consensus"]["signals"] == 55
    assert rows["utbot_consensus"]["wins"] == 37
    assert rows["utbot_consensus"]["losses"] == 18
    assert rows["utbot_consensus"]["total_money"] == 27641.44
    assert rows["utbot_consensus"]["raw_net_points"] == -1536.53
    assert rows["utbot_consensus"]["raw_total_points"] == -1347.49
    assert rows["utbot_consensus"]["net_points"] is None
    assert rows["utbot_consensus"]["open_points"] is None
    assert rows["utbot_consensus"]["total_points"] is None
    assert rows["utbot_consensus"]["gross_gain"] is None
    assert rows["utbot_consensus"]["gross_loss"] is None
    assert rows["utbot_consensus"]["open_rows"] == []


def test_simulation_ids_are_strictly_increasing():
    scanner = Scanner.__new__(Scanner)
    scanner.simulation_id = 100

    first = scanner._next_simulation_id()
    second = scanner._next_simulation_id()

    assert first > 100
    assert second > first


def test_scanner_dashboard_payload_serializes_futures_shadow_cache_key(monkeypatch):
    scanner = Scanner.__new__(Scanner)
    monkeypatch.setattr(
        "scanner.build_dashboard_trade_payload",
        lambda _: {"meta": {"futures_shadow_enabled": False}},
    )

    payload = scanner._build_dashboard_trade_payload()

    assert payload["meta"]["futures_shadow_enabled"] == "False"


def test_historical_scan_only_completes_after_recalculation_release():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner._historical_rebuild_pending = True
    scanner._initial_setup_completed = True

    scanner._recalculation_status = {"status": "queued"}
    assert scanner._historical_scan_can_complete() is False

    scanner._recalculation_status = {"status": "worker_running"}
    assert scanner._historical_scan_can_complete() is False

    scanner._recalculation_status = {"status": "awaiting_scan"}
    assert scanner._historical_scan_can_complete() is True

    scanner._initial_setup_completed = False
    assert scanner._historical_scan_can_complete() is False


def test_obsolete_scan_generation_is_rejected():
    scanner = Scanner.__new__(Scanner)
    scanner.simulation_id = 200

    assert scanner._scan_generation_is_current(200) is True
    assert scanner._scan_generation_is_current(199) is False


def test_recalculation_status_records_start_and_finish_times():
    scanner = Scanner.__new__(Scanner)
    scanner._recalculation_status = {
        "status": "idle",
        "reason": "",
        "started_at": None,
        "finished_at": None,
    }

    scanner._set_recalculation_status("queued", "settings-refresh")
    assert scanner._recalculation_status["started_at"]
    assert scanner._recalculation_status["finished_at"] is None

    scanner._set_recalculation_status("idle", "complete")
    assert scanner._recalculation_status["finished_at"]


def test_concurrency_guard_false_string_does_not_filter_historical_rows(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_concurrency_guard", "False")
    manager = TradeManager.__new__(TradeManager)

    start = IST.localize(datetime(2026, 6, 10, 10, 0, 0))
    trades = [
        Trade(
            id=f"H_NIFTY_15min_{idx}",
            instrument="NIFTY",
            timeframe="15min",
            direction="LONG",
            entry_price=100.0 + idx,
            entry_time=start + timedelta(minutes=idx),
            trailing_stop=99.0,
            current_stop=99.0,
            lots=1,
            lot_size=50,
            grade="B+ (Hist)",
            status="CLOSED",
            exit_price=101.0 + idx,
            exit_time=start + timedelta(minutes=30),
            pnl=50.0,
        )
        for idx in range(3)
    ]

    filtered = manager._filter_concurrency_compliant_closed(trades)

    assert [trade.id for trade in filtered] == [
        "H_NIFTY_15min_2",
        "H_NIFTY_15min_1",
        "H_NIFTY_15min_0",
    ]


def test_adaptive_dual_concurrency_filter_keeps_lane_a_lane_b_pair(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_concurrency_guard", True)
    monkeypatch.setattr(settings, "scanner_engine", "adaptive_dual")
    monkeypatch.setattr(settings, "dual_lane_enabled", True)
    manager = TradeManager.__new__(TradeManager)

    start = IST.localize(datetime(2026, 7, 1, 10, 0, 0))
    common = dict(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        trailing_stop=95.0,
        current_stop=95.0,
        lots=1,
        lot_size=50,
        grade="A (Hist)",
        confidence=0.85,
        inst_type="OPT",
        option_type="CE",
        status="CLOSED",
        exit_price=110.0,
        exit_time=start + timedelta(minutes=30),
        pnl=500.0,
    )
    lane_a = Trade(
        id="H_NIFTY_5min_A_20260701100000",
        entry_time=start,
        source_engine="adaptive_dual",
        source_lane="A",
        **common,
    )
    lane_b = Trade(
        id="H_NIFTY_5min_B_20260701100100",
        entry_time=start + timedelta(minutes=1),
        source_engine="apex_hybrid",
        source_lane="B",
        **common,
    )

    filtered = manager._filter_concurrency_compliant_closed([lane_a, lane_b])

    assert {trade.source_lane for trade in filtered} == {"A", "B"}


def test_scanner_bool_parser_treats_false_string_as_disabled():
    assert Scanner._setting_enabled("False", True) is False
    assert Scanner._setting_enabled("0", True) is False
    assert Scanner._setting_enabled("true", False) is True


def test_signal_processor_false_string_disables_regime_gate(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ut_regime_adaptation", "False")
    monkeypatch.setattr(settings, "ut_backtest_more_results", True)
    monkeypatch.setattr(settings, "live_filter_leniency_pct", 0.0)

    class DummyScanner:
        mode = "HISTORICAL"

    processor = SignalProcessor.__new__(SignalProcessor)
    processor.scanner = DummyScanner()

    assert processor._passes_quality_gate(
        "15min",
        "B",
        0.55,
        "CHOPPY",
        "OPT",
        settings,
        adx_value=0.0,
    )
