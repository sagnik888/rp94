"""
Candle Builder — OHLCV Aggregation & Resampling
═══════════════════════════════════════════════════════════════

Builds and maintains candle data from tick updates.
Resamples 1min candles to 5min and 15min.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Optional
from loguru import logger


class CandleBuilder:
    """
    Builds and maintains OHLCV candles.

    Stores 1min candles and resamples to 5min/15min on demand.
    Handles market hours filtering and partial candle management.
    """

    def __init__(self, max_candles: int = 2000):
        self.max_candles = max_candles
        import threading
        self._lock = threading.Lock()

        # Store 1min base candles per instrument
        self._candles_1min: Dict[str, pd.DataFrame] = {}

        # Cached resampled candles
        self._candles_5min: Dict[str, pd.DataFrame] = {}
        self._candles_15min: Dict[str, pd.DataFrame] = {}

        # Current building candle (for tick-by-tick updates)
        self._current_candle: Dict[str, Dict] = {}
        
        # Real-time LTP tracking
        self._latest_prices: Dict[str, float] = {}

    # Relative cross-index leak guard. Replaces hardcoded absolute price ceilings
    # (e.g. BANKNIFTY>65000, NIFTY>35000) that become stale time-bombs as indices
    # drift. Rejects a price that deviates implausibly from the instrument's own last
    # known price (e.g. a SENSEX tick leaking onto BANKNIFTY); bootstraps cleanly.
    _MAX_PRICE_DEVIATION = 0.30

    def _price_plausible(self, instrument: str, price: float) -> bool:
        if price is None or price <= 0:
            return False
        last = self._latest_prices.get(instrument, 0.0)
        if last and last > 0:
            return abs(float(price) - last) / last <= self._MAX_PRICE_DEVIATION
        return True

    def update_latest_price(self, instrument: str, price: float):
        """Update the real-time LTP and inject it into the latest candle for zero-lag charts"""
        if not self._price_plausible(instrument, price):
            if price and price > 0:
                logger.warning(f"⚠️ Ignoring implausible price {price} for {instrument} (>{int(self._MAX_PRICE_DEVIATION*100)}% from last; likely cross-index leak)")
            return

        self._latest_prices[instrument] = price
        
        # Inject LTP into the 'current' candle row of all timeframes
        # This makes the dashboard feel ALIVE and ensures analysis uses latest tick
        with self._lock:
            for tf_dict in [self._candles_1min, self._candles_5min, self._candles_15min]:
                if instrument in tf_dict and not tf_dict[instrument].empty:
                    df = tf_dict[instrument]
                    # Update the last row's close, high, and low
                    last_idx = df.index[-1]
                    df.at[last_idx, 'close'] = price
                    if price > df.at[last_idx, 'high']:
                        df.at[last_idx, 'high'] = price
                    if price < df.at[last_idx, 'low']:
                        df.at[last_idx, 'low'] = price

    def get_latest_price(self, instrument: str) -> float:
        """Get the latest real-time LTP for an instrument"""
        return self._latest_prices.get(instrument, 0.0)

    def update_from_tick(self, instrument: str, price: float, volume: float, timestamp: datetime):
        """Build 1min candles from websocket tick updates."""
        if not self._price_plausible(instrument, price):
            if price and price > 0:
                logger.warning(f"⚠️ Ignoring implausible tick {price} for {instrument} "
                               f"(>{int(self._MAX_PRICE_DEVIATION*100)}% from last; likely cross-index leak)")
            return

        self._latest_prices[instrument] = price
        candle_time = timestamp.replace(second=0, microsecond=0)
        finalized = None
        partial = None

        with self._lock:
            current = self._current_candle.get(instrument)
            if current is None:
                existing = self._candles_1min.get(instrument)
                existing_row = existing.loc[candle_time] if existing is not None and candle_time in existing.index else None
                existing_ticks = 0
                if existing_row is not None:
                    try:
                        existing_ticks = int(float(existing_row.get("tick_count", 0) or 0))
                    except Exception:
                        existing_ticks = 0
                current = {
                    "timestamp": candle_time,
                    "open": float(existing_row["open"]) if existing_row is not None else price,
                    "high": max(float(existing_row["high"]), price) if existing_row is not None else price,
                    "low": min(float(existing_row["low"]), price) if existing_row is not None else price,
                    "close": price,
                    "start_volume": volume,
                    "volume": float(existing_row["volume"]) if existing_row is not None else 0,
                    "tick_count": existing_ticks + 1,
                }
                self._current_candle[instrument] = current

            elif candle_time == current["timestamp"]:
                current["close"] = price
                current["high"] = max(current["high"], price)
                current["low"] = min(current["low"], price)
                current["volume"] = max(float(current["volume"]), max(0, volume - current["start_volume"]))
                current["tick_count"] = int(current.get("tick_count", 0) or 0) + 1

            else:
                if current["volume"] > 0 or current["high"] > current["low"]:
                    finalized = pd.DataFrame([{
                        "open": current["open"],
                        "high": current["high"],
                        "low": current["low"],
                        "close": current["close"],
                        "volume": current["volume"],
                        "tick_count": int(current.get("tick_count", 0) or 0),
                    }], index=[current["timestamp"]])

                current = {
                    "timestamp": candle_time,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "start_volume": volume,
                    "volume": 0,
                    "tick_count": 1,
                }
                self._current_candle[instrument] = current

            partial = pd.DataFrame([{
                "open": current["open"],
                "high": current["high"],
                "low": current["low"],
                "close": current["close"],
                "volume": current["volume"],
                "tick_count": int(current.get("tick_count", 0) or 0),
            }], index=[current["timestamp"]])

        if finalized is not None:
            self.update_candles(instrument, finalized, "1min")
            logger.debug(f"📊 Built 1min candle for {instrument} at {finalized.index[0]}")

        if partial is not None:
            self.update_candles(instrument, partial, "1min")

    def update_candles(self, instrument: str, new_data: pd.DataFrame, timeframe: str = "1min"):
        """
        Update candle data for an instrument.
        """
        if new_data is None or new_data.empty:
            return

        # ── Mandatory Normalization ──
        # Ensure all columns are lowercase to prevent 'KeyError: close'
        new_data.columns = [c.lower() for c in new_data.columns]

        # Relative cross-index leak guard for batch candles (replaces hardcoded
        # absolute thresholds). Drop rows whose close deviates implausibly from the
        # instrument's last known price, or from the batch median when none exists.
        if 'close' in new_data.columns and len(new_data) > 0:
            ref = self._latest_prices.get(instrument, 0.0)
            if not ref or ref <= 0:
                try:
                    ref = float(new_data['close'].median())
                except Exception:
                    ref = 0.0
            if ref and ref > 0:
                valid_rows = (new_data['close'] - ref).abs() / ref <= self._MAX_PRICE_DEVIATION
                if not valid_rows.all():
                    logger.warning(f"⚠️ Filtering {int((~valid_rows).sum())} implausible rows for {instrument} (cross-index leak guard)")
                    new_data = new_data[valid_rows]

        if new_data.empty:
            return
            
        key = instrument
        with self._lock:
            if timeframe == "1min":
                if key in self._candles_1min and len(self._candles_1min[key]) > 0:
                    existing = self._candles_1min[key]
                    combined = pd.concat([existing, new_data])
                    combined = combined[~combined.index.duplicated(keep='last')]
                    combined = combined.sort_index()
                    if len(combined) > self.max_candles:
                        combined = combined.iloc[-self.max_candles:]
                    self._candles_1min[key] = combined
                else:
                    self._candles_1min[key] = new_data.iloc[-self.max_candles:]
                
                # Resample to higher timeframes (Merge instead of Overwrite)
                self._resample_and_merge(instrument)

            elif timeframe == "5min":
                self._candles_5min[key] = self._merge_candles(self._candles_5min.get(key), new_data)
            elif timeframe == "15min":
                self._candles_15min[key] = self._merge_candles(self._candles_15min.get(key), new_data)

    def _merge_candles(self, existing, new_data):
        """Merge new candle data with existing, deduplicate, trim"""
        # Force same timezone (IST Naive) to avoid comparison and display errors
        # '03:45 UTC' must become '09:15 Naive'
        def to_ist_naive(df):
            if df is None or len(df) == 0: return df
            if df.index.tz is not None:
                return df.tz_convert('Asia/Kolkata').tz_localize(None)
            return df

        existing = to_ist_naive(existing)
        new_data = to_ist_naive(new_data)

        if existing is not None and len(existing) > 0:
            combined = pd.concat([existing, new_data])
            combined = combined[~combined.index.duplicated(keep='last')]
            combined = combined.sort_index()
            if len(combined) > self.max_candles:
                combined = combined.iloc[-self.max_candles:]
            return combined
        
        return new_data.iloc[-self.max_candles:] if new_data is not None else None

    def _resample_and_merge(self, instrument: str):
        """Resample 1min to higher TFs and merge with existing data to preserve history"""
        key = instrument
        base = self._candles_1min.get(key)
        if base is None or len(base) < 5: return

        try:
            # Resample and merge for 5min
            new_5m = self._resample_ohlcv(base, '5min')
            self._candles_5min[key] = self._merge_candles(self._candles_5min.get(key), new_5m)

            # Resample and merge for 15min
            new_15m = self._resample_ohlcv(base, '15min')
            self._candles_15min[key] = self._merge_candles(self._candles_15min.get(key), new_15m)
        except Exception as e:
            logger.error(f"Resample merge error for {instrument}: {e}")

    def _resample(self, instrument: str):
        """Old resample - kept for backward compatibility if needed internally"""
        self._resample_and_merge(instrument)

    @staticmethod
    def _resample_ohlcv(df: pd.DataFrame, freq: str) -> pd.DataFrame:
        """Resample OHLCV data to a higher timeframe"""
        # Map freq strings to pandas offset aliases
        freq_map = {"5min": "5min", "15min": "15min"}
        pd_freq = freq_map.get(freq, freq)

        resampled = df.resample(pd_freq, label='left', closed='left').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum',
        }).dropna()

        return resampled

    def get_candles(self, instrument: str, timeframe: str) -> Optional[pd.DataFrame]:
        """Get candles for instrument at given timeframe"""
        key = instrument
        with self._lock:
            if timeframe == "1min":
                df = self._candles_1min.get(key)
            elif timeframe == "5min":
                df = self._candles_5min.get(key)
            elif timeframe == "15min":
                df = self._candles_15min.get(key)
            else:
                df = None
            return df.copy() if df is not None else None

    def get_candle_count(self, instrument: str, timeframe: str) -> int:
        """Get number of candles available"""
        df = self.get_candles(instrument, timeframe)
        return len(df) if df is not None else 0

    def get_max_timestamp(self) -> datetime:
        """Get the latest market timestamp from any candle dataframe"""
        max_ts = datetime.min
        with self._lock:
            for tf_dict in [self._candles_1min, self._candles_5min, self._candles_15min]:
                for df in tf_dict.values():
                    if not df.empty:
                        last_ts = df.index[-1]
                        if hasattr(last_ts, 'to_pydatetime'):
                            last_ts = last_ts.to_pydatetime()
                        # Ensure naive for comparison
                        if hasattr(last_ts, 'tzinfo') and last_ts.tzinfo:
                            last_ts = last_ts.replace(tzinfo=None)
                        if last_ts > max_ts:
                            max_ts = last_ts
        
        # If no candles, fallback to real time
        if max_ts == datetime.min:
            return datetime.now()
        return max_ts

    def get_status(self) -> Dict:
        """Get status of all candle data"""
        status = {}
        with self._lock:
            for inst in set(list(self._candles_1min.keys()) +
                           list(self._candles_5min.keys()) +
                           list(self._candles_15min.keys())):
                status[inst] = {
                    "1min": len(self._candles_1min.get(inst, [])),
                    "5min": len(self._candles_5min.get(inst, [])),
                    "15min": len(self._candles_15min.get(inst, [])),
                }
        return status
