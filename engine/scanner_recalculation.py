"""Historical recalculation orchestration for Scanner."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pandas as pd
from loguru import logger

from config.settings import get_settings
from engine.recalculation_worker import ProcessRecalculationWorker, candle_rows_from_manifest
from trading.trade_manager import IST, Trade


class ScannerRecalculationMixin:
    """Own background recalculation without owning Scanner construction."""

    def _active_recalculation_queue_reason(self, default: str = "manual") -> str:
        queue = getattr(self, "recalculation_queue", None)
        reason = getattr(queue, "last_reason", "") if queue is not None else ""
        return str(reason or default or "manual")

    def _defer_recalculation_until_scan_release(
        self,
        reason: str,
        *,
        lock_age_seconds: float | None = None,
    ) -> bool:
        """Remember a real recalculation request while an active scan owns the lock."""
        reason = str(reason or "manual")
        if reason == "calculation-lock-retry":
            return False
        self._deferred_recalculation_reason = reason
        payload = dict(getattr(self, "_recalculation_status", {}) or {})
        payload["deferred_reason"] = reason
        payload["deferred_at"] = datetime.now(IST).isoformat()
        if lock_age_seconds is not None:
            payload["lock_age_seconds"] = round(max(0.0, float(lock_age_seconds)), 2)
        self._recalculation_status = payload
        return True

    def _flush_deferred_recalculation_after_scan(self) -> bool:
        reason = str(getattr(self, "_deferred_recalculation_reason", "") or "")
        if not reason:
            return False
        self._deferred_recalculation_reason = ""
        if reason == "calculation-lock-retry":
            return False
        self.queue_full_recalculation(reason)
        return True

    def queue_full_recalculation(self, reason: str = "manual") -> None:
        """Request a background recalculation without blocking the caller."""
        request_recompute = getattr(self, "request_analysis_recompute", None)
        if callable(request_recompute):
            request_recompute(reason)
        try:
            queue = getattr(self, "recalculation_queue", None)
            scanner_loop = getattr(self, "_event_loop", None)
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if queue is not None:
                coro = queue.request(reason)
                if scanner_loop is not None and scanner_loop.is_running() and scanner_loop is not current_loop:
                    asyncio.run_coroutine_threadsafe(coro, scanner_loop)
                else:
                    asyncio.create_task(coro)
            else:
                coro = self._perform_full_recalculation()
                if scanner_loop is not None and scanner_loop.is_running() and scanner_loop is not current_loop:
                    asyncio.run_coroutine_threadsafe(coro, scanner_loop)
                else:
                    asyncio.create_task(coro)
        except RuntimeError:
            logger.warning("Unable to queue recalculation: no active event loop")

    async def _perform_full_recalculation(self):
        """Compatibility path: direct awaits still run the recalculation now."""
        return await self._perform_full_recalculation_impl()

    def _set_recalculation_status(self, status: str, reason: str = "", **extra) -> None:
        payload = dict(getattr(self, "_recalculation_status", {}) or {})
        payload.update({"status": status, "reason": reason or payload.get("reason", ""), **extra})
        if status in {
            "queued",
            "waiting_initial_setup",
            "worker_running",
            "applying",
            "running",
            "scan_running",
            "awaiting_scan",
        }:
            if not payload.get("started_at"):
                payload["started_at"] = datetime.now(IST).isoformat()
            payload["finished_at"] = None
        if status in {"idle", "failed", "timeout"}:
            payload["finished_at"] = datetime.now(IST).isoformat()
        self._recalculation_status = payload

    async def _run_recalculation_worker(self, indices: Dict[str, Any]) -> Dict[str, Any]:
        settings = get_settings()
        if not getattr(settings, "recalculation_worker_enabled", True):
            return {"status": "disabled", "files": [], "errors": []}
        worker = getattr(self, "recalculation_worker", None) or ProcessRecalculationWorker(
            timeout_seconds=float(getattr(settings, "recalculation_worker_timeout_seconds", 120.0) or 120.0)
        )
        job = {
            "backtest_days": int(getattr(self, "backtest_days", 1) or 1),
            "indices": indices,
            "timeframes": ["1min", "5min", "15min"],
            "refresh_data": bool(getattr(settings, "backtest_refresh_data_on_recalc", False)),
            "offline_only": str(getattr(self, "mode", "") or "").upper() == "HISTORICAL",
        }
        loop = asyncio.get_running_loop()
        self._set_recalculation_status("worker_running", "historical-cache-warm")
        return await loop.run_in_executor(None, lambda: worker.run(job))

    def _set_candle_history_capacity(self, days_back_override=None) -> None:
        """Reserve replay rows only in HISTORICAL; keep the live tick limit."""
        candles = getattr(self, "candles", None)
        current_limit = getattr(candles, "max_candles", None)
        if not isinstance(current_limit, int) or current_limit <= 0:
            return
        if not hasattr(self, "_live_candle_capacity"):
            self._live_candle_capacity = current_limit
        capacity = self._live_candle_capacity
        if str(getattr(self, "mode", "") or "").upper() == "HISTORICAL":
            days = days_back_override if days_back_override is not None else getattr(self, "backtest_days", 1)
            # One NSE/BSE session has 375 minute bars. Retain indicator warmup
            # in addition to every requested session; never create missing bars.
            capacity += max(1, min(30, int(days or 1))) * 375
        candles.max_candles = capacity

    def _load_worker_candles(self, worker_result: Dict[str, Any]) -> int:
        self._set_candle_history_capacity()
        loaded = 0
        for item in worker_result.get("files", []) or []:
            try:
                path = Path(item.get("path", ""))
                if not path.exists():
                    continue
                df = pd.read_csv(path, index_col=0, parse_dates=True)
                if df is None or df.empty:
                    continue
                instrument = item.get("instrument", "")
                timeframe = item.get("timeframe", "1min")
                try:
                    self.candles.update_candles(
                        instrument,
                        df,
                        timeframe,
                        mark_receipt=False,
                    )
                except TypeError as exc:
                    if "mark_receipt" not in str(exc):
                        raise
                    self.candles.update_candles(instrument, df, timeframe)
                retained = self.candles.get_candles(instrument, timeframe)
                if retained is not None and not retained.empty:
                    # CSV duplicates, validation and live retention can all reduce
                    # the rows actually available to the scan after this update.
                    loaded += len(df.index.intersection(retained.index))
            except Exception as exc:
                logger.debug(f"Worker candle load skipped: {exc}")
        return loaded

    async def _perform_full_recalculation_impl(self):
        """Sequential sequence to ensure re-simulation uses NEW data"""
        if getattr(self, "_calculation_lock", False):
            lock_age = max(0.0, time.time() - float(getattr(self, "_lock_time", time.time()) or time.time()))
            status = str((getattr(self, "_recalculation_status", {}) or {}).get("status", "") or "").lower()
            active_scan_owner = bool(getattr(self, "_active_scan_started_at", None)) or status == "scan_running"
            request_reason = self._active_recalculation_queue_reason("manual")
            if active_scan_owner:
                deferred = self._defer_recalculation_until_scan_release(
                    request_reason,
                    lock_age_seconds=lock_age,
                )
                if deferred:
                    logger.info(
                        "Re-simulation deferred until active scan releases the lock "
                        f"(reason={request_reason}, lock_age={lock_age:.1f}s)."
                    )
                return False

            logger.warning("🚫 Re-simulation delayed: Calculation lock active.")
            settings = get_settings()
            live_wait = max(
                2.0,
                float(getattr(settings, "live_calculation_lock_timeout_seconds", 12.0) or 12.0),
            )
            wait_budget = max(75.0, live_wait) if self.mode == "HISTORICAL" else live_wait
            deadline = time.monotonic() + wait_budget
            while getattr(self, "_calculation_lock", False) and time.monotonic() < deadline:
                await asyncio.sleep(0.25)
            if getattr(self, "_calculation_lock", False):
                if not bool(getattr(self, "is_calculating", False)):
                    logger.error(
                        f"Re-simulation found stale lock after waiting {wait_budget:.1f}s; "
                        "no calculation flag is active, so the stale lock will be released."
                    )
                    self._calculation_lock = False
                else:
                    self._defer_recalculation_until_scan_release(
                        request_reason,
                        lock_age_seconds=max(
                            0.0,
                            time.time() - float(getattr(self, "_lock_time", time.time()) or time.time()),
                        ),
                    )
                    logger.error(
                        f"Re-simulation skipped after waiting {wait_budget:.1f}s; "
                        "the active calculation remains owner of the lock. "
                        "Request deferred until the lock releases."
                    )
                    return False

        self._calculation_lock = True
        self._lock_time = time.time()
        started_at = time.monotonic()
        active_label = ", ".join(getattr(self, "active_indices", []) or []) or "none"
        self.log_event(
            f"Backtest started: {self.backtest_days} day(s), indices={active_label}",
            "system",
        )
        all_indices = self.config.get("indices", {})
        indices = {k: v for k, v in all_indices.items() if k in self.active_indices}
        worker_result: Dict[str, Any] = {"status": "not_started", "files": [], "errors": []}
        loaded_rows = 0
        try:
            initial_setup_task = getattr(self, "_initial_setup_task", None)
            if (
                initial_setup_task is not None
                and initial_setup_task is not asyncio.current_task()
                and not initial_setup_task.done()
            ):
                self._set_recalculation_status(
                    "waiting_initial_setup",
                    "startup-hydration",
                )
                logger.info("Waiting for startup hydration before historical recalculation.")
                await asyncio.shield(initial_setup_task)
            if self.mode == "HISTORICAL":
                try:
                    worker_result = await self._run_recalculation_worker(indices)
                except Exception as exc:
                    worker_result = {"status": "error", "files": [], "errors": [str(exc)]}
                    logger.warning(f"Process recalculation worker failed; falling back in-process: {exc}")
            self._set_recalculation_status("applying", "apply-worker-result", worker=worker_result)
            self._reset_simulation_diagnostics()
            logger.info("🔄 RE-SIMULATION SEQUENCE: Fetching deep history...")

            # CLEAR TRADES: Preserve real session trades, only remove simulated historical ones.
            # Real trades (not starting with H_ or EOD_) come from actual live sessions and
            # should be kept for accurate historical playback. Simulated trades are rebuilt
            # from candle data and may diverge from real execution.
            real_trades = [ct for ct in self.trades.closed_trades if not ct.id.startswith("H_") and not ct.id.startswith("EOD_")]
            self.trades.closed_trades = real_trades
            self.trades.open_trades = {}

            # ── CONVERT SESSION CANDIDATES TO TRADES FOR HISTORICAL MODE ──
            # In HISTORICAL mode, if session candidates exist (from a real live session),
            # convert them to Trade objects so the trades panel shows real data.
            if self.mode == "HISTORICAL":
                self._load_session_trade_candidates()
                session_candidates = getattr(self, "_session_trade_candidates", {})
                if session_candidates:
                    import pytz
                    ist = pytz.timezone('Asia/Kolkata')

                    # First, collect all ENTRY and EXIT rows by their match key
                    entry_map = {}  # (instrument, direction, timeframe, timestamp) -> candidate
                    exit_map = {}   # (instrument, direction, timeframe, timestamp) -> candidate

                    for instrument, book in session_candidates.items():
                        for key, cand in book.items():
                            action = getattr(cand, 'action', 'ENTRY')
                            signal_ts = getattr(cand, 'signal_timestamp', None)
                            signal_key = signal_ts.isoformat() if signal_ts else ""
                            match_key = (
                                getattr(cand, 'instrument', instrument),
                                getattr(cand, 'direction', 'LONG'),
                                getattr(cand, 'timeframe', '5min'),
                                signal_key
                            )
                            if action == 'ENTRY':
                                entry_map[match_key] = cand
                            elif action == 'EXIT':
                                exit_map[match_key] = cand

                    # Now pair ENTRY with EXIT rows to create complete Trade objects
                    trades_created = 0
                    for match_key, entry_cand in entry_map.items():
                        # Skip if already in closed_trades (avoid duplicates)
                        cand_id = getattr(entry_cand, 'trade_id', None) or f"SESSION_{match_key[0]}_{match_key[3]}"
                        if any(ct.id == cand_id for ct in self.trades.closed_trades):
                            continue

                        signal_ts = getattr(entry_cand, 'signal_timestamp', None)
                        if not signal_ts:
                            try:
                                signal_ts = datetime.fromisoformat(getattr(entry_cand, 'timestamp', ''))
                                if signal_ts.tzinfo is None:
                                    signal_ts = IST.localize(signal_ts)
                            except Exception:
                                continue

                        saved_entry_ts = getattr(entry_cand, "entry_timestamp", None)
                        if saved_entry_ts:
                            entry_time = (
                                saved_entry_ts
                                if isinstance(saved_entry_ts, datetime)
                                else datetime.fromisoformat(str(saved_entry_ts))
                            )
                        else:
                            entry_time = signal_ts
                        if entry_time.tzinfo is None:
                            entry_time = IST.localize(entry_time)
                        else:
                            entry_time = entry_time.astimezone(IST)

                        # Find matching EXIT row
                        exit_cand = exit_map.get(match_key)
                        exit_time = None
                        exit_price = 0.0
                        exit_reason = 'SESSION_END'
                        pnl = float(getattr(entry_cand, 'pnl', 0.0))

                        if exit_cand:
                            exit_ts_val = getattr(exit_cand, 'exit_timestamp', None)
                            if exit_ts_val:
                                if isinstance(exit_ts_val, datetime):
                                    exit_time = exit_ts_val
                                elif isinstance(exit_ts_val, str) and exit_ts_val.strip():
                                    try:
                                        exit_time = datetime.fromisoformat(exit_ts_val)
                                    except Exception:
                                        pass
                                if exit_time and exit_time.tzinfo is None:
                                    exit_time = IST.localize(exit_time)
                            exit_price = float(getattr(exit_cand, 'exit_price', 0.0)) if getattr(exit_cand, 'exit_price', None) else 0.0
                            if exit_price == 0:
                                exit_price = float(getattr(exit_cand, 'current_price', 0.0))
                            exit_reason = getattr(exit_cand, 'exit_reason', 'OPPOSITE_SIGNAL')
                            pnl = self._candidate_net_pnl(exit_cand, exit_price) if exit_price > 0 else 0.0

                        trade = Trade(
                            id=cand_id,
                            instrument=getattr(entry_cand, 'instrument', match_key[0]),
                            timeframe=getattr(entry_cand, 'timeframe', '5min'),
                            direction=getattr(entry_cand, 'direction', 'LONG'),
                            entry_price=float(getattr(entry_cand, 'price', 0.0)),
                            entry_time=entry_time,
                            trailing_stop=float(getattr(entry_cand, 'stop', 0.0)),
                            current_stop=float(getattr(entry_cand, 'stop', 0.0)),
                            lots=int(getattr(entry_cand, 'lots', 1)),
                            lot_size=int(getattr(entry_cand, 'lot_size', 1)),
                            grade=getattr(entry_cand, 'grade', 'B'),
                            atm_strike=float(getattr(entry_cand, 'atm_strike', 0.0)),
                            option_type=getattr(entry_cand, 'option_type', ''),
                            target=float(getattr(entry_cand, 'target', 0.0)),
                            rr_ratio=float(getattr(entry_cand, 'rr', 1.5)),
                            confidence=float(getattr(entry_cand, 'confidence', 0.0)),
                            inst_type=getattr(entry_cand, 'inst_type', 'FUT'),
                            instrument_multiplier=1.0,
                            exec_type=getattr(entry_cand, 'exec_type', 'A'),
                            current_price=float(getattr(entry_cand, 'current_price', getattr(entry_cand, 'price', 0.0))),
                            # Never fall back to the candidate PRICE: on an OPT that is the premium,
                            # and entry_spot is a SPOT-space field every converter keys off.
                            entry_spot=float(getattr(entry_cand, 'entry_spot', 0.0) or 0.0),
                            status="CLOSED" if exit_price > 0 else "OPEN",
                            broker_order_id=getattr(entry_cand, 'trading_symbol', ''),
                            trading_symbol=getattr(entry_cand, 'trading_symbol', ''),
                            symbol_token=getattr(entry_cand, 'symbol_token', ''),
                            peak_pnl=0.0,
                            max_drawdown=0.0,
                            charges=(
                                float(get_settings().fut_cost)
                                if getattr(entry_cand, 'inst_type') == 'FUT'
                                else float(get_settings().opt_cost)
                            ),
                        )

                        if exit_price > 0 and exit_time:
                            trade.exit_price = exit_price
                            trade.exit_time = exit_time
                            trade.exit_reason = exit_reason
                            trade.pnl = pnl
                            trade.status = "CLOSED"

                        self.trades.closed_trades.append(trade)
                        trades_created += 1

                    logger.info(f"📊 Converted {trades_created} session candidates to trades for HISTORICAL mode")

            if worker_result.get("status") in {"ok", "incomplete"} and candle_rows_from_manifest(worker_result.get("files", [])) > 0:
                loaded_rows = self._load_worker_candles(worker_result)
                logger.info(f"Loaded {loaded_rows} candle row(s) from process worker cache.")
            if loaded_rows <= 0:
                if self.mode == "HISTORICAL":
                    missing = list(worker_result.get("errors", []) or [])
                    self._historical_offline_missing = missing or [
                        "historical candle cache unavailable"
                    ]
                    self._set_recalculation_status(
                        "failed",
                        "offline-historical-data-unavailable",
                        worker=worker_result,
                        missing=self._historical_offline_missing,
                    )
                    self.log_event(
                        "Backtest abstained: offline historical candle cache unavailable",
                        "error",
                    )
                    return False
                logger.info("Process worker cache unavailable; using in-process history fetch fallback.")
                await self._fetch_all_data(indices, force=True)

            logger.info("🔄 RE-SIMULATION SEQUENCE: Recalculating signals and trades...")
            # An older in-flight scan may have repopulated these after configure()
            # cleared them. Invalidate every generation-sensitive cache only after
            # we own the calculation lock and the replacement candles are loaded.
            self._cached_results.clear()
            self._latest_filtered_candidates.clear()
            for attr in list(self.__dict__):
                if attr.startswith("chart_cache_"):
                    delattr(self, attr)
            if hasattr(self.mtf, "_result_cache"):
                self.mtf._result_cache.clear()
            if hasattr(self.mtf, "_data_hash"):
                self.mtf._data_hash.clear()
            self._last_signal_time.clear() # Clear here ONLY after data is ready
            self._last_signal_identity.clear()
            self._next_simulation_id()
            self._stamp_simulation_diagnostics()
            # The next _scan_cycle will now pick up the new days_back and empty last_signal_time
            logger.info("🧹 System state RESET with FORCED history fetch.")
            elapsed = time.monotonic() - started_at
            self.log_event(
                f"Backtest finished: {self.backtest_days} day(s) recalculated in {elapsed:.1f}s",
                "success",
            )
            if self.mode == "HISTORICAL":
                self._historical_rebuild_pending = True
                self._set_recalculation_status(
                    "awaiting_scan",
                    "historical-scan-pending",
                    worker=worker_result,
                    loaded_rows=loaded_rows,
                    duration_seconds=round(elapsed, 2),
                )
            else:
                self._set_recalculation_status(
                    "idle",
                    "complete",
                    worker=worker_result,
                    loaded_rows=loaded_rows,
                    duration_seconds=round(elapsed, 2),
                )
            return True
        except Exception as exc:
            elapsed = time.monotonic() - started_at
            self.log_event(
                f"Backtest failed after {elapsed:.1f}s: {exc}",
                "error",
            )
            self._set_recalculation_status(
                "failed",
                "exception",
                worker=worker_result,
                error=str(exc),
                duration_seconds=round(elapsed, 2),
            )
            raise
        finally:
            self._calculation_lock = False
