import asyncio
import time
from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from engine.recalculation_queue import RecalculationQueue
from engine.scanner_recalculation import ScannerRecalculationMixin
from engine.recalculation_worker import _cached_candle_manifest
from intelligence.regime_detector import RegimeDetector
from scanner import Scanner


def test_settings_refresh_coalesces_without_cancelling_active_runner():
    async def scenario():
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0
        cancellations = 0

        async def runner():
            nonlocal calls, cancellations
            calls += 1
            if calls == 1:
                first_started.set()
                try:
                    await release_first.wait()
                except asyncio.CancelledError:
                    cancellations += 1
                    raise

        queue = RecalculationQueue(runner, debounce_seconds=0)
        assert (await queue.request("settings-refresh"))["status"] == "started"
        await first_started.wait()
        assert (await queue.request("settings-refresh"))["status"] == "superseded"
        assert (await queue.request("settings-refresh"))["status"] == "superseded"
        release_first.set()
        await queue.drain()

        assert calls == 2
        assert cancellations == 0
        assert queue.superseded_count == 2
        assert queue.last_status == "idle"

    asyncio.run(scenario())


def test_recalculation_request_defers_while_historical_scan_owns_lock():
    class DummyScanner(ScannerRecalculationMixin):
        def __init__(self):
            self.mode = "HISTORICAL"
            self._calculation_lock = True
            self.is_calculating = True
            self._lock_time = time.time()
            self._active_scan_started_at = datetime.now()
            self._recalculation_status = {
                "status": "scan_running",
                "reason": "historical-scan-running",
            }
            self.recalculation_queue = SimpleNamespace(last_reason="settings-refresh")
            self.queued = []

        def queue_full_recalculation(self, reason="manual"):
            self.queued.append(reason)

    async def scenario():
        scanner = DummyScanner()
        result = await scanner._perform_full_recalculation_impl()

        assert result is False
        assert scanner.queued == []
        assert scanner._deferred_recalculation_reason == "settings-refresh"
        assert scanner._recalculation_status["status"] == "scan_running"
        assert scanner._recalculation_status["deferred_reason"] == "settings-refresh"

        assert scanner._flush_deferred_recalculation_after_scan() is True
        assert scanner.queued == ["settings-refresh"]
        assert scanner._flush_deferred_recalculation_after_scan() is False

    asyncio.run(scenario())


def test_calculation_lock_retry_is_not_requeued_while_scan_runs():
    class DummyScanner(ScannerRecalculationMixin):
        def __init__(self):
            self.mode = "HISTORICAL"
            self._calculation_lock = True
            self.is_calculating = True
            self._lock_time = time.time()
            self._active_scan_started_at = datetime.now()
            self._recalculation_status = {"status": "scan_running"}
            self.recalculation_queue = SimpleNamespace(last_reason="calculation-lock-retry")
            self.queued = []

        def queue_full_recalculation(self, reason="manual"):
            self.queued.append(reason)

    async def scenario():
        scanner = DummyScanner()
        result = await scanner._perform_full_recalculation_impl()

        assert result is False
        assert scanner.queued == []
        assert not getattr(scanner, "_deferred_recalculation_reason", "")

    asyncio.run(scenario())


def test_historical_rebuild_blocks_scans_until_recalculation_is_applied():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner._historical_rebuild_pending = True

    for status in ("queued", "waiting_initial_setup", "worker_running", "applying"):
        scanner._recalculation_status = {"status": status}
        assert scanner._historical_recalculation_blocks_scan() is True

    scanner._recalculation_status = {"status": "awaiting_scan"}
    assert scanner._historical_recalculation_blocks_scan() is False

    scanner._recalculation_status = {"status": "scan_running"}
    assert scanner._historical_recalculation_blocks_scan() is False

    scanner.mode = "REAL"
    scanner._recalculation_status = {"status": "queued"}
    assert scanner._historical_recalculation_blocks_scan() is False


def test_historical_scan_can_complete_while_scan_running():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner._historical_rebuild_pending = True
    scanner._initial_setup_completed = True
    scanner._recalculation_status = {"status": "scan_running"}
    scanner.last_scan_time = datetime(2026, 6, 25, 9, 35)

    assert scanner._historical_scan_can_complete() is True


def test_historical_intel_at_uses_simulation_cache():
    scanner = Scanner.__new__(Scanner)
    scanner.simulation_id = 101
    scanner._historical_intel_at_cache = {}
    scanner._historical_vix_cache = {}
    scanner._historical_vix_day_cache = {}
    scanner._vix_percentile_cache = {}
    scanner._vix_data = {}

    idx = pd.date_range("2026-06-25 09:15", periods=8, freq="5min")
    candles_5m = pd.DataFrame(
        {
            "open": [100.0] * 8,
            "high": [101.0] * 8,
            "low": [99.0] * 8,
            "close": [100.5] * 8,
            "volume": [1000] * 8,
        },
        index=idx,
    )
    candles_1m = candles_5m.copy()
    scanner.candles = SimpleNamespace(
        get_candles=lambda _instrument, timeframe: candles_1m if timeframe == "1min" else candles_5m
    )
    calls = {"count": 0}

    def analyze(**_kwargs):
        calls["count"] += 1
        return {"aggregate": {"score": 0.4}, "regime": {"regime": "TRENDING_UP"}}

    scanner.intel = SimpleNamespace(analyze=analyze)

    first = scanner._compute_historical_intel_at("NIFTY", datetime(2026, 6, 25, 9, 35), "5min")
    second = scanner._compute_historical_intel_at("NIFTY", datetime(2026, 6, 25, 9, 39), "5min")

    assert first == second
    assert calls["count"] == 1

    scanner._compute_historical_intel_at("NIFTY", datetime(2026, 6, 25, 9, 40), "5min")
    assert calls["count"] == 2


def test_recalculation_worker_can_reuse_existing_candle_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    candle_dir = tmp_path / "data_store" / "candles"
    candle_dir.mkdir(parents=True)
    cache_file = candle_dir / "NIFTY_5min.csv"
    cache_file.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2026-06-25 09:15:00,1,2,1,2,100\n"
        "2026-06-25 09:20:00,2,3,2,3,120\n",
        encoding="utf-8",
    )

    manifest = _cached_candle_manifest("NIFTY", "5min", 20)

    assert manifest is not None
    assert manifest["path"] == str(candle_dir.relative_to(tmp_path) / "NIFTY_5min.csv")
    assert manifest["rows"] == 2
    assert manifest["source"] == "local_cache"


def test_simulation_reset_clears_stateful_historical_replay_memory():
    scanner = Scanner.__new__(Scanner)
    scanner.simulation_id = 42
    scanner.backtest_days = 1
    scanner._diagnostics = {}
    scanner._historical_intel_at_cache = {"old": (0.0, "UNKNOWN")}
    scanner._historical_confluence_at_cache = {"old": 0.0}
    scanner._historical_confluence_index_cache = {"old": ([], [])}
    scanner._historical_vix_cache = {"old": 12.0}
    scanner._historical_vix_day_cache = {"old": []}
    scanner._vix_percentile_cache = {"old": 50.0}
    scanner._dir_census_date = "2026-06-25"
    scanner._dir_census = {"LONG": 7, "SHORT": 2}
    scanner._dir_census_seen = {"NIFTY|5min|2026-06-25 10:00:00|LONG"}
    regime = RegimeDetector()
    regime._regime_history["NIFTY_5min"] = ["TRENDING_UP"]
    scanner.intel = SimpleNamespace(regime=regime)

    scanner._reset_simulation_diagnostics()

    assert scanner._historical_intel_at_cache == {}
    assert scanner._historical_confluence_at_cache == {}
    assert scanner._historical_confluence_index_cache == {}
    assert scanner._historical_vix_cache == {}
    assert scanner._historical_vix_day_cache == {}
    assert scanner._vix_percentile_cache == {}
    assert scanner._dir_census_date is None
    assert scanner._dir_census == {"LONG": 0, "SHORT": 0}
    assert scanner._dir_census_seen == set()
    assert regime._regime_history == {}
