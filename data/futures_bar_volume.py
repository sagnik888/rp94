"""Signal-bar participation from near-month index futures.

Fyers supplies the cash-index volume every threshold here was calibrated on, but only
through history polls that land a minute or more after a bar closes. At the open the
signal bar therefore has no cash volume when the entry gate decides, the profiler
reports the bar unavailable, and the "measured volume" confirmation fails for lack of
data rather than lack of participation (measured live on 2026-10-09: seven grade-A
opening signals refused on that leg alone).

Kite's futures candles are current at bar close. This module compares the closed
futures bar with the previous futures bars of the same timeframe, the same shape as the
cash profiler's rolling mean. Contracts are only ever compared with contracts; the
result is never written into an index candle.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Optional

import pandas as pd

BASIS = "KITE_FUTURES_VOLUME"
_TF_MINUTES = {"1min": 1, "3min": 3, "5min": 5, "15min": 15}
_EXCHANGE = {"SENSEX": "BFO", "BANKEX": "BFO"}


def _naive_ist(value) -> Optional[pd.Timestamp]:
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if ts is pd.NaT:
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
    return ts


class FuturesBarVolume:
    """Closed-bar futures volume ratio for an index, cached per index and timeframe."""

    def __init__(self, kite, *, cache_seconds: float = 20.0, error_cooldown: float = 60.0,
                 baseline_bars: int = 20, min_baseline_bars: int = 10, rollover_days: int = 2,
                 days_back: int = 5, clock: Callable[[], float] = time.time):
        self.kite = kite
        self.cache_seconds = float(cache_seconds)
        self.error_cooldown = float(error_cooldown)
        self.baseline_bars = int(baseline_bars)
        self.min_baseline_bars = int(min_baseline_bars)
        self.rollover_days = int(rollover_days)
        self.days_back = int(days_back)
        self.clock = clock
        self._lock = threading.Lock()
        self._frames: Dict[tuple, tuple] = {}
        self._failed_at: Dict[tuple, float] = {}
        self._pool = None

    def contract(self, index: str, today: date) -> Optional[Dict[str, Any]]:
        """Near-month future, or the next month once expiry is within rollover_days."""
        index = str(index or "").upper()
        exchange = _EXCHANGE.get(index, "NFO")
        kite = self.kite
        lock = getattr(kite, "_lock", None)
        try:
            if lock is not None:
                with lock:
                    rows = list((getattr(kite, "_instruments", {}) or {}).get(exchange, []))
            else:
                rows = list((getattr(kite, "_instruments", {}) or {}).get(exchange, []))
        except Exception:
            return None
        futures = sorted(
            (row for row in rows
             if row.get("name") == index and row.get("instrument_type") == "FUT"
             and row.get("expiry") and row["expiry"] >= today
             and int(row.get("instrument_token") or 0) > 0),
            key=lambda row: row["expiry"],
        )
        if not futures:
            return None
        chosen = futures[0]
        if len(futures) > 1 and (chosen["expiry"] - today).days < self.rollover_days:
            chosen = futures[1]
        return {"exchange": exchange, "tradingsymbol": chosen["tradingsymbol"],
                "instrument_token": int(chosen["instrument_token"]), "expiry": chosen["expiry"]}

    def _frame(self, index: str, timeframe: str, contract: Dict[str, Any], now: float) -> pd.DataFrame:
        key = (index, timeframe, contract["instrument_token"])
        with self._lock:
            cached = self._frames.get(key)
            if cached and now - cached[0] <= self.cache_seconds:
                return cached[1]
            failed = self._failed_at.get(key, 0.0)
            if failed and now - failed < self.error_cooldown:
                return pd.DataFrame()
        frame = self.kite.get_historical(contract["instrument_token"], timeframe,
                                         days_back=self.days_back, record_health=False)
        frame = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        if not frame.empty:
            frame = frame.copy()
            frame.index = pd.DatetimeIndex([_naive_ist(t) for t in frame.index])
            frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        with self._lock:
            if frame.empty or "volume" not in frame.columns:
                self._failed_at[key] = now
                return pd.DataFrame()
            self._frames[key] = (now, frame)
            self._failed_at.pop(key, None)
        return frame

    def evidence(self, index: str, timeframe: str, signal_time, *, now: Optional[float] = None) -> Dict[str, Any]:
        """Volume ratio of the closed futures bar holding `signal_time`.

        `signal_time` may be the bar's open or its close; a bar that has not closed yet
        is never measured.
        """
        now = float(self.clock() if now is None else now)
        result: Dict[str, Any] = {"available": False, "basis": BASIS, "unit": "contracts",
                                  "ratio": 0.0, "bar_volume": 0.0, "baseline_volume": 0.0,
                                  "baseline_bars": 0, "tradingsymbol": "", "bar_start": "",
                                  "reason": ""}
        minutes = _TF_MINUTES.get(str(timeframe or ""))
        ts = _naive_ist(signal_time)
        if not minutes or ts is None:
            result["reason"] = "unsupported timeframe or signal time"
            return result
        now_ist = _naive_ist(pd.Timestamp(now, unit="s", tz="UTC"))
        step = pd.Timedelta(minutes=minutes)
        bar_start = ts.floor(f"{minutes}min")
        if bar_start + step > now_ist and bar_start == ts:
            bar_start -= step  # signal_time named the bar's close
        if bar_start + step > now_ist:
            result["reason"] = "signal bar has not closed"
            return result
        if now_ist - (bar_start + step) > 2 * step:
            result["reason"] = "signal bar is stale"
            return result
        contract = self.contract(index, now_ist.date())
        if not contract:
            result["reason"] = "no live futures contract"
            return result
        result["tradingsymbol"] = contract["tradingsymbol"]
        try:
            frame = self._frame(str(index).upper(), str(timeframe), contract, now)
        except Exception as exc:
            with self._lock:
                self._failed_at[(str(index).upper(), str(timeframe), contract["instrument_token"])] = now
            result["reason"] = f"futures history failed: {exc}"
            return result
        if frame.empty or bar_start not in frame.index:
            result["reason"] = "futures bar unavailable"
            return result
        volume = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)
        bar_volume = float(volume.loc[bar_start])
        prior = volume.loc[volume.index < bar_start]
        prior = prior[prior > 0].tail(self.baseline_bars)
        result.update(bar_volume=bar_volume, baseline_bars=int(len(prior)),
                      bar_start=bar_start.isoformat())
        if bar_volume <= 0 or len(prior) < self.min_baseline_bars:
            result["reason"] = "futures bar or baseline too thin"
            return result
        baseline = float(prior.mean())
        result.update(available=True, baseline_volume=round(baseline, 2),
                      ratio=round(bar_volume / baseline, 4) if baseline > 0 else 0.0)
        return result

    def evidence_within(self, index: str, timeframe: str, signal_time, *, timeout: float = 2.0) -> Dict[str, Any]:
        """`evidence` with a wall-clock cap, so a slow broker never stalls the scan.

        A late answer still lands in the cache for the next scan.
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="futures-volume")
            pool = self._pool
        future = pool.submit(self.evidence, index, timeframe, signal_time)
        try:
            return future.result(timeout=max(0.1, float(timeout)))
        except FutureTimeout:
            return {"available": False, "basis": BASIS, "reason": "futures history timed out"}
