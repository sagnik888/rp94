"""Scanner diagnostics, telemetry, and health reporting."""

from __future__ import annotations

import asyncio
import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor

_telemetry_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='telemetry-metrics')

_EMPTY_SYSTEM_METRICS: Dict[str, Any] = {
    "pid": 0,
    "process_ram_mb": 0.0,
    "process_cpu_pct": 0.0,
    "process_cpu_single_core_pct": 0.0,
    "logical_cpu_count": 0.0,
    "threads": 0,
    "ram": {"total_mb": 0.0, "used_mb": 0.0, "used_pct": 0.0},
    "disk": {"total_gb": 0.0, "free_gb": 0.0, "used_pct": 0.0},
    "gpu": {"available": False, "label": "Warming up"},
    "warming_up": True,
}


from config.settings import get_settings
from engine.settings_persistence import settings_persistence_status
from trading.trade_manager import IST


class ScannerTelemetryMixin:
    """Runtime diagnostics kept separate from the trading pipeline."""

    def _diag_inc(self, category: str, key: str, amount: int = 1) -> None:
        diagnostics = getattr(self, "_diagnostics", None)
        if diagnostics is None:
            self._diagnostics = {}
            diagnostics = self._diagnostics
        bucket = diagnostics.setdefault(category, {})
        bucket[key] = int(bucket.get(key, 0) or 0) + amount

    def _diag_inc_unique(self, category: str, key: str, identity: str) -> None:
        unique_events = getattr(self, "_diagnostic_unique_events", None)
        if unique_events is None:
            self._diagnostic_unique_events = set()
            unique_events = self._diagnostic_unique_events
        event_key = (str(category), str(key), str(identity))
        if event_key in unique_events:
            return
        unique_events.add(event_key)
        self._diag_inc(category, key)

    def _reset_simulation_diagnostics(self) -> None:
        """Start strategy counters for the newly requested backtest window."""
        previous = getattr(self, "_diagnostics", {}) or {}
        previous_latency = previous.get("latency", {}) or {}
        for cache_name in (
            "_historical_intel_at_cache",
            "_historical_confluence_at_cache",
            "_historical_confluence_index_cache",
            "_historical_vix_cache",
            "_historical_vix_day_cache",
            "_vix_percentile_cache",
        ):
            cache = getattr(self, cache_name, None)
            if isinstance(cache, dict):
                cache.clear()
        self._dir_census_date = None
        self._dir_census = {"LONG": 0, "SHORT": 0}
        self._dir_census_seen = set()
        regime_reset = getattr(
            getattr(getattr(self, "intel", None), "regime", None),
            "reset_history",
            None,
        )
        if callable(regime_reset):
            regime_reset()
        self._diagnostic_unique_events = set()
        self._diagnostics = {
            "rejects": {},
            "option_history": {"attempts": 0, "hits": 0, "misses": 0, "synthetic": 0, "fallback_to_fut": 0},
            "latency": {
                "last_ms": 0,
                "high_latency_events": int(previous_latency.get("high_latency_events", 0) or 0),
            },
            "stabilization": {
                "buffered": 0,
                "waiting_stability": 0,
                "waiting_candle_close": 0,
                "discarded_repaint": 0,
                "discarded_future": 0,
                "discarded_stale": 0,
                "matured": 0,
            },
            "timeouts": {"skipped_inflight": 0},
            "repaint_guard": {"checked": 0, "aborted": 0, "passed": 0},
            "exit_reasons": {},
            "instrument_selection": {"OPT": 0, "FUT": 0},
            "source_fallback": {"delayed_entry_blocked": 0},
            "simulation": {
                "id": int(getattr(self, "simulation_id", 0) or 0),
                "backtest_days": int(getattr(self, "backtest_days", 0) or 0),
                "started_at": datetime.now(IST).isoformat(),
            },
        }

    def _stamp_simulation_diagnostics(self) -> None:
        diagnostics = getattr(self, "_diagnostics", None)
        if not isinstance(diagnostics, dict):
            return
        simulation = dict(diagnostics.get("simulation") or {})
        simulation.update({
            "id": int(getattr(self, "simulation_id", 0) or 0),
            "backtest_days": int(getattr(self, "backtest_days", 0) or 0),
        })
        simulation.setdefault("started_at", datetime.now(IST).isoformat())
        diagnostics["simulation"] = simulation

    def _next_simulation_id(self) -> int:
        """Return a strictly increasing ID even for multiple changes in one second."""
        current = int(getattr(self, "simulation_id", 0) or 0)
        candidate = time.time_ns() // 1_000_000
        self.simulation_id = max(current + 1, candidate)
        return self.simulation_id

    def _historical_scan_can_complete(self) -> bool:
        status = str(
            (getattr(self, "_recalculation_status", {}) or {}).get("status", "")
            or ""
        ).lower()
        if self.mode != "HISTORICAL" or not bool(getattr(self, "_historical_rebuild_pending", False)):
            return False
        if status == "scan_running":
            return bool(getattr(self, "last_scan_time", None))
        return status == "awaiting_scan" and bool(getattr(self, "_initial_setup_completed", False))

    def _historical_recalculation_blocks_scan(self) -> bool:
        """Keep normal scans from racing an accepted historical rebuild."""
        if self.mode != "HISTORICAL" or not bool(
            getattr(self, "_historical_rebuild_pending", False)
        ):
            return False
        status = str(
            (getattr(self, "_recalculation_status", {}) or {}).get("status", "")
            or ""
        ).lower()
        return status in {
            "queued",
            "waiting_initial_setup",
            "worker_running",
            "applying",
            "running",
        }

    def _scan_generation_is_current(self, simulation_id: int) -> bool:
        return int(simulation_id) == int(getattr(self, "simulation_id", 0) or 0)

    def _get_system_metrics_payload(self) -> Dict[str, Any]:
        """Return cached local hardware/process metrics for the UT1 state panel."""
        now = time.time()
        cached = getattr(self, "_system_metrics_cache", None)
        cache_age = now - float(cached.get("_sampled_at", 0.0) or 0.0) if isinstance(cached, dict) else float('inf')
        if cache_age < 5.0:
            return {k: v for k, v in cached.items() if k != "_sampled_at"}
        try:
            asyncio.get_running_loop()
            on_event_loop = True
        except RuntimeError:
            on_event_loop = False
        # Synchronous diagnostics/tests outside asyncio may request an immediate
        # sample. The live scanner/dashboard path always receives a non-blocking
        # warming snapshot and refreshes in the dedicated telemetry worker.
        if not isinstance(cached, dict) and not on_event_loop:
            return self._compute_system_metrics(now)
        # Stale-while-revalidate: return stale cache and refresh in background
        # to avoid blocking the event loop with subprocess calls.
        if not isinstance(cached, dict):
            cached = {
                **_EMPTY_SYSTEM_METRICS,
                "pid": os.getpid(),
                "threads": threading.active_count(),
                "_sampled_at": 0.0,
            }
            self._system_metrics_cache = cached
        if not getattr(self, "_metrics_refresh_pending", False):
            self._metrics_refresh_pending = True
            def _refresh():
                try:
                    self._compute_system_metrics(time.time())
                finally:
                    self._metrics_refresh_pending = False
            _telemetry_executor.submit(_refresh)
            return {k: v for k, v in cached.items() if k != "_sampled_at"}
        return {k: v for k, v in cached.items() if k != "_sampled_at"}

    def _compute_system_metrics(self, now: float) -> Dict[str, Any]:

        def process_rss_mb() -> float:
            try:
                import psutil
                return round(float(psutil.Process(os.getpid()).memory_info().rss) / (1024 * 1024), 1)
            except Exception:
                pass
            if os.name == "nt":
                try:
                    proc = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {os.getpid()}", "/FO", "CSV", "/NH"],
                        capture_output=True,
                        text=True,
                        timeout=0.3,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    row = (proc.stdout or "").splitlines()[0] if proc.returncode == 0 and proc.stdout else ""
                    if row:
                        parts = [part.strip().strip('"') for part in row.split('","')]
                        mem_text = parts[-1] if parts else ""
                        mem_kb = float("".join(ch for ch in mem_text if ch.isdigit()) or 0.0)
                        if mem_kb > 0:
                            return round(mem_kb / 1024.0, 1)
                except Exception:
                    pass
            if os.name != "nt":
                return 0.0
            try:
                class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                    _fields_ = [
                        ("cb", ctypes.c_ulong),
                        ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                    ]

                counters = PROCESS_MEMORY_COUNTERS()
                counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
                handle = ctypes.windll.kernel32.GetCurrentProcess()
                ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
                return round(float(counters.WorkingSetSize) / (1024 * 1024), 1) if ok else 0.0
            except Exception:
                return 0.0

        def system_memory() -> Dict[str, float]:
            if os.name != "nt":
                return {"total_mb": 0.0, "used_mb": 0.0, "used_pct": 0.0}
            try:
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                total = float(stat.ullTotalPhys)
                used = total - float(stat.ullAvailPhys)
                return {
                    "total_mb": round(total / (1024 * 1024), 0),
                    "used_mb": round(used / (1024 * 1024), 0),
                    "used_pct": round(float(stat.dwMemoryLoad), 1),
                }
            except Exception:
                return {"total_mb": 0.0, "used_mb": 0.0, "used_pct": 0.0}

        def process_cpu_metrics() -> Dict[str, float]:
            cpu_now = time.process_time()
            previous = getattr(self, "_system_cpu_sample", None)
            self._system_cpu_sample = (now, cpu_now)
            logical_cpu_count = max(1, int(os.cpu_count() or 1))
            if not previous:
                return {
                    "process_cpu_pct": 0.0,
                    "process_cpu_single_core_pct": 0.0,
                    "logical_cpu_count": float(logical_cpu_count),
                }
            prev_wall, prev_cpu = previous
            wall_delta = max(0.001, now - float(prev_wall or now))
            single_core_pct = max(0.0, ((cpu_now - float(prev_cpu or cpu_now)) / wall_delta) * 100.0)
            return {
                "process_cpu_pct": round(single_core_pct / logical_cpu_count, 1),
                "process_cpu_single_core_pct": round(single_core_pct, 1),
                "logical_cpu_count": float(logical_cpu_count),
            }

        def disk_metrics() -> Dict[str, float]:
            try:
                usage = shutil.disk_usage(Path.cwd())
                used = usage.total - usage.free
                return {
                    "total_gb": round(usage.total / (1024 ** 3), 1),
                    "free_gb": round(usage.free / (1024 ** 3), 1),
                    "used_pct": round((used / usage.total) * 100.0, 1) if usage.total else 0.0,
                }
            except Exception:
                return {"total_gb": 0.0, "free_gb": 0.0, "used_pct": 0.0}

        def gpu_metrics() -> Dict[str, Any]:
            gpu_cache = getattr(self, "_system_gpu_cache", None)
            if isinstance(gpu_cache, dict) and now - float(gpu_cache.get("_sampled_at", 0.0) or 0.0) < 20.0:
                return {k: v for k, v in gpu_cache.items() if k != "_sampled_at"}
            try:
                from engine.gpu_accelerator import get_gpu_accelerator
                accel_payload = get_gpu_accelerator().status()
                bench_payload = get_gpu_accelerator().benchmark(n=100_000)
            except Exception:
                accel_payload = {
                    "compute_available": False,
                    "compute_backend": "none",
                    "acceleration_mode": "telemetry_only",
                    "error": "",
                }
                bench_payload = {"available": False, "backend": "none"}

            payload: Dict[str, Any] = {
                "available": False,
                "label": "N/A",
                "used_mb": 0.0,
                "total_mb": 0.0,
                "used_pct": 0.0,
                **accel_payload,
                "benchmark": bench_payload,
            }
            try:
                proc = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=0.3,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                first = (proc.stdout or "").splitlines()[0].strip() if proc.returncode == 0 and proc.stdout else ""
                if first:
                    parts = [p.strip() for p in first.split(",")]
                    used = float(parts[1]) if len(parts) > 1 else 0.0
                    total = float(parts[2]) if len(parts) > 2 else 0.0
                    payload = {
                        "available": True,
                        "label": parts[0] if parts else "GPU",
                        "used_mb": round(used, 0),
                        "total_mb": round(total, 0),
                        "used_pct": round((used / total) * 100.0, 1) if total > 0 else 0.0,
                        "util_pct": round(float(parts[3]), 1) if len(parts) > 3 and parts[3] else 0.0,
                        **accel_payload,
                        "benchmark": bench_payload,
                    }
            except Exception:
                pass
            self._system_gpu_cache = {**payload, "_sampled_at": now}
            return payload

        cpu_metrics = process_cpu_metrics()
        payload = {
            "pid": os.getpid(),
            "process_ram_mb": process_rss_mb(),
            **cpu_metrics,
            "threads": threading.active_count(),
            "ram": system_memory(),
            "disk": disk_metrics(),
            "gpu": gpu_metrics(),
        }
        self._system_metrics_cache = {**payload, "_sampled_at": now}
        return payload

    @staticmethod
    def shutdown_telemetry_executor(wait: bool = False) -> None:
        """Stop accepting telemetry work during process shutdown."""
        try:
            _telemetry_executor.shutdown(wait=wait, cancel_futures=True)
        except Exception:
            pass

    def _estimated_default_executor_max_workers(self) -> int:
        """Mirror Python's default ThreadPoolExecutor sizing formula."""
        return min(32, int(os.cpu_count() or 1) + 4)

    def _default_executor_max_workers(self) -> Optional[int]:
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            executor = getattr(loop, "_default_executor", None) if loop else None
            max_workers = getattr(executor, "_max_workers", None)
            return int(max_workers) if max_workers is not None else None
        except Exception:
            return None

    def _async_execution_queue_payload(self) -> Dict[str, Any]:
        module = sys.modules.get("engine.async_queue")
        execution_queue = getattr(module, "execution_queue", None) if module else None
        if execution_queue is None:
            return {
                "available": False,
                "lazy_until_first_submit": True,
                "configured_workers": 4,
                "queue_capacity": 500,
            }
        task_queue = getattr(execution_queue, "task_queue", None)
        worker_threads = list(getattr(execution_queue, "worker_threads", []) or [])
        queue_size = None
        queue_capacity = None
        try:
            queue_size = int(task_queue.qsize()) if task_queue is not None else None
            queue_capacity = int(getattr(task_queue, "maxsize", 0) or 0) if task_queue is not None else None
        except Exception:
            queue_size = None
            queue_capacity = None
        payload: Dict[str, Any] = {
            "available": True,
            "workers": int(getattr(execution_queue, "worker_count", 0) or len(worker_threads) or 0),
            "alive_workers": sum(1 for worker in worker_threads if getattr(worker, "is_alive", lambda: False)()),
            "queue_size": queue_size,
            "queue_capacity": queue_capacity,
        }
        if queue_size is not None and queue_capacity:
            payload["queue_remaining"] = max(0, queue_capacity - queue_size)
            payload["queue_fill_pct"] = round((queue_size / queue_capacity) * 100.0, 1)
        return payload

    def _broker_executor_payload(self) -> Dict[str, Any]:
        broker = getattr(getattr(self, "trades", None), "broker", None)
        executor = getattr(broker, "_executor", None) if broker is not None else None
        if executor is None:
            return {"available": False}
        work_queue = getattr(executor, "_work_queue", None)
        queue_size = None
        try:
            queue_size = int(work_queue.qsize()) if work_queue is not None else None
        except Exception:
            queue_size = None
        return {
            "available": True,
            "max_workers": int(getattr(executor, "_max_workers", 0) or 0),
            "queued_tasks": queue_size,
        }

    def _worker_resources_payload(self) -> Dict[str, Any]:
        active_indices = len(list(getattr(self, "active_indices", []) or []))
        estimated_executor_workers = self._estimated_default_executor_max_workers()
        default_executor_workers = self._default_executor_max_workers()
        live_inflight = len(getattr(self, "_live_processing_futures", {}) or {})
        return {
            "logical_cpu_count": int(os.cpu_count() or 1),
            "active_threads": threading.active_count(),
            "active_indices": active_indices,
            "live_inflight": live_inflight,
            "historical_rebuild_pending": bool(getattr(self, "_historical_rebuild_pending", False)),
            "estimated_default_executor_max_workers": estimated_executor_workers,
            "default_executor_max_workers": default_executor_workers,
            "live_index_parallelism_needed": active_indices,
            "live_processing_capacity_ratio": round(
                estimated_executor_workers / max(1, active_indices),
                2,
            ),
            "async_execution_queue": self._async_execution_queue_payload(),
            "broker_executor": self._broker_executor_payload(),
            "recalculation_worker_timeout_seconds": float(
                getattr(getattr(self, "recalculation_worker", None), "timeout_seconds", 0.0) or 0.0
            ),
        }

    def _get_diagnostics_payload(self) -> Dict[str, Any]:
        diagnostics = getattr(self, "_diagnostics", {}) or {}
        rejects = diagnostics.get("rejects", {}) or {}

        source_health = {}
        try:
            source_health = self.data.get_source_health()
        except Exception:
            source_health = {}

        # ── Data Freshness ──
        # In live mode the operator wants to know whether the broker feed is
        # fresh. A forming 5-minute candle can legitimately be 0-300 seconds
        # old, so keep candle age separately and publish broker tick age as the
        # main data freshness signal.
        candle_freshness = {}
        now_fresh = datetime.now(IST)
        for inst_name in list(getattr(self, "active_indices", [])):
            try:
                df = self.candles.get_candles(inst_name, "5min")
                if df is not None and len(df) > 0:
                    last_ts = df.index[-1]
                    if getattr(last_ts, "tzinfo", None) is None:
                        last_ts = last_ts.replace(tzinfo=IST)
                    else:
                        last_ts = last_ts.astimezone(IST)
                    age_s = (now_fresh - last_ts).total_seconds()
                    candle_freshness[inst_name] = round(age_s, 0)
                else:
                    candle_freshness[inst_name] = -1
            except Exception:
                candle_freshness[inst_name] = -1

        data_freshness = dict(candle_freshness)
        if str(getattr(self, "mode", "") or "").upper() == "REAL":
            live_source_ages: List[float] = []
            for source_name in ("angel", "fyers"):
                age_value = source_health.get(f"{source_name}_tick_age_seconds")
                if source_health.get(source_name) and age_value is not None:
                    try:
                        live_source_ages.append(float(age_value))
                    except (TypeError, ValueError):
                        pass
            live_age = round(min(live_source_ages), 1) if live_source_ages else -1
            data_freshness = {
                inst_name: live_age for inst_name in list(getattr(self, "active_indices", []))
            }

        # ── Session Stats: signals generated vs trades taken ──
        session_candidates = getattr(self, "_session_trade_candidates", {}) or {}
        total_entries = 0
        total_exits = 0
        for inst_book in list(session_candidates.values()):
            for cand in list(inst_book.values()):
                action = getattr(cand, "action", "ENTRY")
                if action == "EXIT":
                    total_exits += 1
                elif action == "ENTRY":
                    total_entries += 1

        # ── Active trade peak PnL ──
        def _candidate_value(candidate, name: str, default: Any = "") -> Any:
            if isinstance(candidate, dict):
                return candidate.get(name, default)
            return getattr(candidate, name, default)

        def _candidate_lifecycle_identity(candidate) -> str:
            lifecycle_id = str(_candidate_value(candidate, "lifecycle_id", "") or "")
            if lifecycle_id:
                return lifecycle_id
            signal_ts = (
                _candidate_value(candidate, "source_signal_timestamp", None)
                or _candidate_value(candidate, "signal_timestamp", None)
                or ""
            )
            if isinstance(signal_ts, datetime):
                signal_ts = signal_ts.replace(tzinfo=None).isoformat()
            return "|".join(
                str(part or "")
                for part in (
                    _candidate_value(candidate, "instrument", ""),
                    _candidate_value(candidate, "timeframe", ""),
                    _candidate_value(candidate, "direction", ""),
                    signal_ts,
                )
            )

        active_peak = {}
        closed_lifecycles = set()
        for inst_book in list(session_candidates.values()):
            for cand in list(inst_book.values()):
                action = str(_candidate_value(cand, "action", "") or "").upper()
                exit_reason = str(_candidate_value(cand, "exit_reason", "") or "").upper()
                lifecycle_id = _candidate_lifecycle_identity(cand)
                if lifecycle_id and (
                    action == "EXIT"
                    or bool(_candidate_value(cand, "is_exit", False))
                    or exit_reason == "QUOTE_INVALID"
                ):
                    closed_lifecycles.add(lifecycle_id)
        for inst_name, book in list(session_candidates.items()):
            for cand in list(book.values()):
                action = str(_candidate_value(cand, "action", "") or "").upper()
                lifecycle_id = _candidate_lifecycle_identity(cand)
                if (
                    action == "ENTRY"
                    and not _candidate_value(cand, "is_exit", False)
                    and lifecycle_id not in closed_lifecycles
                ):
                    peak = float(_candidate_value(cand, "peak_pnl", 0.0) or 0.0)
                    current = float(_candidate_value(cand, "pnl", 0.0) or 0.0)
                    active_peak[inst_name] = {"peak": round(peak, 0), "current": round(current, 0)}

        option_history = dict(diagnostics.get("option_history", {}) or {})
        real_option_hits = int(option_history.get("hits", 0) or 0) + int(option_history.get("archived_hits", 0) or 0)
        option_misses = int(option_history.get("misses", 0) or 0)
        option_fallbacks = int(option_history.get("fallback_to_fut", 0) or 0)
        option_total = real_option_hits + option_misses + int(option_history.get("synthetic", 0) or 0)
        fallback_rate = (option_fallbacks / option_total * 100.0) if option_total else 0.0
        option_history_quality = {
            "status": "WATCH" if option_fallbacks else "OK",
            "real_option_hits": real_option_hits,
            "misses": option_misses,
            "fallback_to_fut": option_fallbacks,
            "fallback_rate_pct": round(fallback_rate, 2),
            "note": (
                "Some historical option trades used FUT fallback because verified option candles were unavailable."
                if option_fallbacks
                else "Historical option candle coverage is sufficient for accepted option rows."
            ),
        }

        return {
            "rejects": dict(sorted(rejects.items(), key=lambda item: item[1], reverse=True)[:12]),
            "risk_configuration": get_settings().risk_configuration_summary(),
            "option_history": option_history,
            "option_history_quality": option_history_quality,
            "option_history_bundle": dict(
                getattr(self, "_option_history_bundle_status", {}) or {}
            ),
            "scanner_freshness": self._scanner_freshness_payload(),
            "recalculation": self._recalculation_status_payload(),
            "settings_persistence": settings_persistence_status(),
            "latency": {
                **(diagnostics.get("latency", {}) or {}),
                "last_ms": (diagnostics.get("latency", {}) or {}).get(
                    "last_ms",
                    self.latest_results.get("latency", 0) if isinstance(self.latest_results, dict) else 0,
                ),
                "scan_count": self.scan_count,
            },
            "stabilization": {
                **(diagnostics.get("stabilization", {}) or {}),
                "pending": len(getattr(self, "_pending_live_signals", {}) or {}),
            },
            "recovery_checkpoint": {
                "enabled": bool(getattr(self, "warm_memory", None) and self._warm_memory_allowed()),
                "count": int(getattr(self, "_recovery_checkpoint_count", 0) or 0),
                "last_at": getattr(self, "_last_recovery_checkpoint_at", None),
                "last_error": getattr(self, "_last_recovery_checkpoint_error", ""),
                "path": str(getattr(getattr(self, "warm_memory", None), "path", "")),
                "interval_seconds": float(getattr(getattr(self, "warm_memory", None), "save_interval_seconds", 0.0) or 0.0),
                "ttl_minutes": int(getattr(getattr(self, "warm_memory", None), "ttl", timedelta()).total_seconds() // 60),
            },
            "timeouts": diagnostics.get("timeouts", {}),
            "index_processing_ms": diagnostics.get("index_processing_ms", {}),
            "worker_resources": self._worker_resources_payload(),
            "sources": source_health,
            "mode": self.mode,
            "repaint_guard": diagnostics.get("repaint_guard", {}),
            "exit_reasons": diagnostics.get("exit_reasons", {}),
            "instrument_selection": diagnostics.get("instrument_selection", {}),
            "source_fallback": diagnostics.get("source_fallback", {}),
            "pre_candle_prediction": diagnostics.get("pre_candle_prediction", {}),
            "index_tick_volume": (
                self.data.get_index_tick_volume_diagnostics()
                if hasattr(getattr(self, "data", None), "get_index_tick_volume_diagnostics")
                else {}
            ),
            "simulation": diagnostics.get("simulation", {}),
            "data_freshness": data_freshness,
            "candle_freshness": candle_freshness,
            "session_stats": {
                "entries": total_entries,
                "exits": total_exits,
            },
            "active_peak": active_peak,
            "system_metrics": self._get_system_metrics_payload(),
            "system_performance": self._system_performance_payload(),
        }

    def _system_performance_payload(self, latency_ms: Optional[float] = None) -> Dict[str, Any]:
        """Summarise runtime health for the dashboard SYS tape and diagnostics."""
        diagnostics = getattr(self, "_diagnostics", {}) or {}
        latency_diag = diagnostics.get("latency", {}) or {}
        last_latency = float(
            latency_ms
            if latency_ms is not None
            else latency_diag.get("last_ms")
            or (self.latest_results.get("latency", 0) if isinstance(self.latest_results, dict) else 0)
            or 0
        )
        freshness = self._scanner_freshness_payload()
        recalculation = self._recalculation_status_payload()
        metrics = self._get_system_metrics_payload()
        sources = {}
        try:
            sources = self.data.get_source_health()
        except Exception:
            sources = {}

        issues: List[Dict[str, Any]] = []

        def add_issue(key: str, label: str, severity: str = "warn", value: Any = None) -> None:
            issues.append({"key": key, "label": label, "severity": severity, "value": value})

        mode = str(getattr(self, "mode", "") or "").upper()
        recalc_status = str(recalculation.get("status") or "").lower()
        busy = bool(
            getattr(self, "is_calculating", False)
            or getattr(self, "_calculation_lock", False)
            or recalc_status
            not in ("", "idle", "none", "complete", "completed", "done")
        )
        historical_busy = mode == "HISTORICAL" and busy
        if historical_busy:
            add_issue(
                "scanner_busy",
                "Scanner busy (recalc/scan)",
                "info",
                freshness.get("effective_age_seconds") or freshness.get("age_seconds"),
            )
        if freshness.get("is_stale"):
            historical_idle = (
                mode == "HISTORICAL"
                and recalc_status in ("", "idle")
                and not bool(getattr(self, "_historical_rebuild_pending", False))
            )
            # Long historical rebuilds (minutes) are expected — do not page as critical.
            if not historical_busy and not historical_idle:
                add_issue("scanner_stale", "Scanner stale", "critical", freshness.get("age_seconds"))
        if recalculation.get("status") not in (None, "", "idle"):
            add_issue("recalculation_running", f"Recalculation {recalculation.get('status')}", "info")
        if last_latency >= 5000:
            add_issue("scan_latency_high", "Scan latency high", "warn", round(last_latency, 0))
        if bool(sources.get("broker_degraded")):
            add_issue("broker_degraded", "Broker degraded", "warn")
        if bool(sources.get("all_brokers_unavailable")):
            add_issue("brokers_down", "All brokers unavailable", "critical")

        ram_pct = float(((metrics.get("ram") or {}).get("used_pct")) or 0.0)
        disk_pct = float(((metrics.get("disk") or {}).get("used_pct")) or 0.0)
        cpu_pct = float(metrics.get("process_cpu_pct") or 0.0)
        single_core_cpu_pct = float(metrics.get("process_cpu_single_core_pct") or 0.0)
        if ram_pct >= 88.0:
            add_issue("ram_pressure", "System RAM pressure", "warn", round(ram_pct, 1))
        if disk_pct >= 92.0:
            add_issue("disk_pressure", "Storage pressure", "warn", round(disk_pct, 1))
        if cpu_pct >= 70.0 or (single_core_cpu_pct >= 180.0 and last_latency >= 2000):
            add_issue("process_cpu_pressure", "UT1 CPU pressure", "warn", round(cpu_pct, 1))

        status = "OK"
        if any(issue["severity"] == "critical" for issue in issues):
            status = "CRITICAL"
        elif any(issue["severity"] == "warn" for issue in issues):
            status = "WARN"
        elif any(issue["severity"] == "info" for issue in issues):
            status = "BUSY"

        payload = {
            "status": status,
            "issues": issues[:8],
            "latency_ms": round(last_latency, 0),
            "scan_count": int(getattr(self, "scan_count", 0) or 0),
            "scanner_stale": bool(freshness.get("is_stale")),
            "recalculation_status": recalculation.get("status", "idle"),
            "sampled_at": datetime.now(IST).isoformat(),
        }
        self._system_perf_snapshot = payload
        return payload

    def _publish_system_performance_alerts(self, latency_ms: Optional[float] = None) -> None:
        """Rate-limit system-performance findings into the activity SYS tape."""
        payload = self._system_performance_payload(latency_ms=latency_ms)
        now_ts = time.time()
        last_alert = getattr(self, "_system_perf_last_alert", {})
        for issue in payload.get("issues", []):
            if issue.get("severity") == "info":
                continue
            key = str(issue.get("key") or issue.get("label") or "system_perf")
            raw_value = issue.get("value")
            value = float(raw_value or 0.0)
            if key == "scanner_stale" and value < 30.0:
                continue
            if key == "scan_latency_high" and value < 30000.0:
                continue
            if key == "process_cpu_pressure" and value < 95.0:
                continue
            if now_ts - float(last_alert.get(key, 0.0) or 0.0) < 60.0:
                continue
            last_alert[key] = now_ts
            suffix = f" ({raw_value})" if raw_value not in (None, "") else ""
            self.log_event(f"SYS PERF: {issue.get('label')}{suffix}", "system")
        self._system_perf_last_alert = last_alert

    def _scanner_freshness_payload(self) -> Dict[str, Any]:
        settings = get_settings()
        threshold = max(1.0, float(getattr(settings, "scanner_stale_after_seconds", 5.0) or 5.0))
        last_scan = getattr(self, "last_scan_time", None)
        age = None
        if last_scan is not None:
            try:
                age = max(0.0, (datetime.now(IST) - last_scan).total_seconds())
            except Exception:
                age = None
        progress_at = getattr(self, "_active_scan_progress_at", None)
        if progress_at is None:
            progress_text = (getattr(self, "_recalculation_status", {}) or {}).get("last_progress_at")
            if progress_text:
                try:
                    progress_at = datetime.fromisoformat(str(progress_text))
                    if progress_at.tzinfo is None:
                        progress_at = IST.localize(progress_at)
                    else:
                        progress_at = progress_at.astimezone(IST)
                except Exception:
                    progress_at = None
        progress_age = None
        if progress_at is not None:
            try:
                progress_age = max(0.0, (datetime.now(IST) - progress_at).total_seconds())
            except Exception:
                progress_age = None
        effective_age = age
        stale_basis = "last_scan"
        busy = bool(
            getattr(self, "is_calculating", False)
            or getattr(self, "_calculation_lock", False)
        )
        historical_busy = (
            str(getattr(self, "mode", "") or "").upper() == "HISTORICAL"
            and busy
        )
        if (
            historical_busy
            and progress_age is not None
            and (effective_age is None or progress_age < effective_age)
        ):
            effective_age = progress_age
            stale_basis = "scan_progress"
        # Multi-minute historical rebuilds are normal; raise threshold while busy.
        if historical_busy:
            busy_thr = float(getattr(settings, "scanner_stale_while_busy_seconds", 600.0) or 600.0)
            threshold = max(threshold, max(60.0, busy_thr))
        cache_age_ms = None
        cache = getattr(self, "dashboard_cache", None)
        if cache is not None and hasattr(cache, "cache_age_ms"):
            cache_age_ms = cache.cache_age_ms()
        return {
            "last_scan": last_scan.isoformat() if last_scan else None,
            "age_seconds": round(age, 2) if age is not None else None,
            "last_progress": progress_at.isoformat() if progress_at else None,
            "progress_age_seconds": round(progress_age, 2) if progress_age is not None else None,
            "effective_age_seconds": round(effective_age, 2) if effective_age is not None else None,
            "stale_basis": stale_basis,
            "stale_after_seconds": threshold,
            "is_stale": bool(effective_age is not None and effective_age > threshold),
            "is_calculating": bool(getattr(self, "is_calculating", False)),
            "calculation_lock": bool(getattr(self, "_calculation_lock", False)),
            "cache_age_ms": cache_age_ms,
        }

    def _recalculation_status_payload(self) -> Dict[str, Any]:
        status = dict(getattr(self, "_recalculation_status", {}) or {})
        if str(getattr(self, "mode", "") or "").upper() != "HISTORICAL":
            current_status = str(status.get("status") or "").lower()
            reason = str(status.get("reason") or "").lower()
            if current_status == "awaiting_scan" and "scan-pending" in reason:
                status["status"] = "idle"
                status["reason"] = "live-mode"
                status["finished_at"] = datetime.now(IST).isoformat()
        queue = getattr(self, "recalculation_queue", None)
        if queue is not None:
            status["queue_status"] = getattr(queue, "last_status", "unknown")
            status["queue_reason"] = getattr(queue, "last_reason", "")
            status["queue_superseded_count"] = int(getattr(queue, "superseded_count", 0) or 0)
        return status
