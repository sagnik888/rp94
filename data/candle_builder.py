"""
Candle Builder — OHLCV Aggregation & Resampling
═══════════════════════════════════════════════════════════════

Builds and maintains candle data from tick updates.
Resamples 1min candles to 5min and 15min.
"""

import time

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Optional
from loguru import logger


def _basis_of_bucket(values) -> str:
    """The single volume unit in this bucket, "" if unknown, MIXED if more than one."""
    seen = {str(v).strip() for v in values if str(v).strip() and str(v).strip().lower() != "nan"}
    if not seen:
        return ""
    return seen.pop() if len(seen) == 1 else "MIXED"


class CandleBuilder:
    """
    Builds and maintains OHLCV candles.

    Stores 1min candles and resamples to 5min/15min on demand.
    Handles market hours filtering and partial candle management.
    """

    def __init__(
        self,
        max_candles: int = 2000,
        resample_min_interval_seconds: float = 0.4,
    ):
        self.max_candles = max_candles
        import threading
        self._lock = threading.Lock()

        # Floor on how often a FORMING bar is re-aggregated to 5min/15min.
        #
        # A closing bar always resamples immediately; this only rate-limits the partial
        # bar that every tick rewrites. Ticks arrive ~10/s per index and each one
        # recomputed the same forming bucket, at a cost that is almost entirely fixed
        # pandas overhead rather than data volume: 2.1ms to aggregate 5 rows into 1, and
        # 2.5ms to merge that 1 row into a 400-row cache, twice over for 5min and 15min.
        #
        # Measured 2026-08-31: the critical section was 14.3ms per tick, 10.5ms of it
        # this resample, holding CandleBuilder._lock -- 587ms of lock-held time per
        # second across four indices. py-spy showed 57.5 thread-seconds of blocking in a
        # 55s capture, 58.6% of ALL scan-path samples queued on this one lock, which is
        # what made scan latency bimodal: 331-535ms when ticks were sparse, 1.8-2.7s when
        # they were dense.
        self._resample_min_interval = max(0.0, float(resample_min_interval_seconds))
        self._last_resample_at: Dict[str, float] = {}
        self._pending_resample_touched: Dict[str, pd.Index] = {}

        # Store 1min base candles per instrument
        self._candles_1min: Dict[str, pd.DataFrame] = {}

        # Cached resampled candles
        self._candles_5min: Dict[str, pd.DataFrame] = {}
        self._candles_15min: Dict[str, pd.DataFrame] = {}

        # Current building candle (for tick-by-tick updates)
        self._current_candle: Dict[str, Dict] = {}
        
        # Real-time LTP tracking
        self._latest_prices: Dict[str, float] = {}
        # Receipt clocks for candle data.  These are deliberately separate from
        # quote/LTP clocks: a fresh quote must never make a stale OHLC frame appear
        # fresh to analytical consumers.
        self._last_candle_receipt: Dict[str, Dict[str, datetime]] = {}
        self._last_quote_receipt: Dict[str, datetime] = {}
        # Which feed last supplied the cumulative volume total, per instrument. A change
        # means the unit changed and the running baseline must be rebased, not subtracted.
        self._volume_source: Dict[str, str] = {}

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
        """Update quote-only LTP state.

        Quotes are not candle observations.  In particular, do not rewrite the
        latest OHLC row here: doing so let a stalled candle feed masquerade as fresh
        analytical data merely because the quote lane was alive.
        """
        if not self._price_plausible(instrument, price):
            if price and price > 0:
                logger.warning(f"⚠️ Ignoring implausible price {price} for {instrument} (>{int(self._MAX_PRICE_DEVIATION*100)}% from last; likely cross-index leak)")
            return

        with self._lock:
            self._latest_prices[instrument] = price
            self._last_quote_receipt[instrument] = datetime.now()

    def get_latest_price(self, instrument: str) -> float:
        """Get the latest real-time LTP for an instrument"""
        return self._latest_prices.get(instrument, 0.0)

    def update_from_tick(
        self,
        instrument: str,
        price: float,
        volume: float,
        timestamp: datetime,
        volume_source: str = "",
    ):
        """Build 1min candles from websocket tick updates.

        ``volume`` is a CUMULATIVE day total; the per-candle figure is the delta since
        the candle opened. ``volume_source`` names which feed produced that total, and
        it matters: index volume can switch between Fyers cash-market shares and the
        broker's near-month FUTURES contracts, which differ by ~1000x. Subtracting a
        shares baseline from a contracts total (or the reverse) invents a spike or a
        hole, and a fabricated spike is worse than no volume — it is exactly what the
        2.5x surge gate reads as a real breakout.
        """
        if not self._price_plausible(instrument, price):
            if price and price > 0:
                logger.warning(f"⚠️ Ignoring implausible tick {price} for {instrument} "
                               f"(>{int(self._MAX_PRICE_DEVIATION*100)}% from last; likely cross-index leak)")
            return

        # The tick is also a quote observation. Keep its receipt clock separate from
        # the OHLC mutation below; the candle receipt is stamped only after the store
        # accepts the bar, while this clock records the arrival of the market tick.
        with self._lock:
            self._latest_prices[instrument] = price
            self._last_quote_receipt[instrument] = datetime.now()
        candle_time = timestamp.replace(second=0, microsecond=0)
        source = str(volume_source or "")
        source_changed = bool(
            source and self._volume_source.get(instrument, source) != source
        )
        if source:
            self._volume_source[instrument] = source
        finalized = None
        partial = None
        receipt_at = datetime.now()

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
                    # None = "no baseline yet". A price-only tick (volume 0) carries no
                    # volume information, so it must NOT establish a baseline of 0 — the
                    # next tick's full cumulative total would then be recorded as this
                    # bar's delta.
                    "start_volume": volume if volume > 0 else None,
                    "volume_basis": source if (source and volume > 0) else "",
                    "source": "live_tick",
                    "volume": float(existing_row["volume"]) if existing_row is not None else 0,
                    "tick_count": existing_ticks + 1,
                }
                self._current_candle[instrument] = current

            elif candle_time == current["timestamp"]:
                current["close"] = price
                current["high"] = max(current["high"], price)
                current["low"] = min(current["low"], price)
                # A tick with no volume is a PRICE tick. Both feeds publish these, and
                # the vol-less case is the steady state of the very outage this fallback
                # exists for. It must not touch the baseline: rebasing to 0 here made the
                # next tick's whole day-cumulative total land as this bar's delta — a
                # ~4000x fabricated spike that clears the 2.5x surge gate and admits a
                # 1min entry on fiction. Leave the running delta exactly as it was.
                if volume > 0:
                    baseline = current.get("start_volume")
                    # Rebase rather than subtract across a discontinuity: the feed
                    # switched unit (shares <-> contracts), or the broker's cumulative
                    # counter went backwards (daily reset / reconnect). A first positive
                    # total after price-only ticks simply establishes the baseline.
                    if source:
                        current["volume_basis"] = source
                    if baseline is None:
                        current["start_volume"] = volume
                    elif source_changed or volume < baseline:
                        if source_changed:
                            logger.info(
                                f"📊 {instrument} volume source -> {source}; rebasing the "
                                "candle baseline so no cross-unit delta is recorded."
                            )
                            # The delta accumulated so far was measured in the OLD unit.
                            # Relabelling the bar while keeping that figure hands the
                            # surge guard a bar carrying shares volume stamped as
                            # contracts — and `max()` below would preserve the larger old
                            # number forever. Restart the measurement in the new unit:
                            # this bar undercounts once, which can only fail SAFE, where
                            # a mislabelled cross-unit figure fabricates a breakout.
                            current["volume"] = 0
                        current["start_volume"] = volume
                    else:
                        current["volume"] = max(
                            float(current["volume"]), max(0, volume - baseline)
                        )
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
                        "volume_basis": current.get("volume_basis", ""),
                        "source": current.get("source", "live_tick"),
                        "provider_time": current["timestamp"],
                        "receipt_at": receipt_at,
                    }], index=[current["timestamp"]])

                current = {
                    "timestamp": candle_time,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "start_volume": volume if volume > 0 else None,
                    "volume_basis": source if (source and volume > 0) else "",
                    "source": "live_tick",
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
                "volume_basis": current.get("volume_basis", ""),
                "source": current.get("source", "live_tick"),
                "provider_time": current["timestamp"],
                "receipt_at": receipt_at,
            }], index=[current["timestamp"]])

        if finalized is not None:
            self.update_candles(instrument, finalized, "1min")
            logger.debug(f"📊 Built 1min candle for {instrument} at {finalized.index[0]}")

        if partial is not None:
            # The forming bar only -- a closing bar went through the branch above with
            # throttling off, so closed buckets are never deferred.
            self.update_candles(instrument, partial, "1min", throttle_resample=True)

    def update_candles(
        self,
        instrument: str,
        new_data: pd.DataFrame,
        timeframe: str = "1min",
        *,
        received_at: Optional[datetime] = None,
        mark_receipt: bool = True,
        throttle_resample: bool = False,
    ):
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
                    median_val = new_data['close'].median()
                    # NOTE: no local `import pandas as pd` here. pandas is already
                    # imported at module scope, and re-importing inside this function
                    # made `pd` a LOCAL name for the whole function: whenever a valid
                    # `ref` existed this branch was skipped, the import never ran, and
                    # the `pd.isna(ref)` / `pd.concat` calls below raised
                    # "cannot access local variable 'pd'" — which failed every candle
                    # update and therefore every historical fetch.
                    ref = float(median_val) if not pd.isna(median_val) else 0.0
                except Exception:
                    ref = 0.0
            if ref and ref > 0 and not pd.isna(ref):
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
                    # Only rows supplied by this update can have been overwritten by
                    # the keep-last merge.  Scanning every cached candle here made a
                    # one-row websocket tick do O(history) work while holding _lock.
                    touched = new_data.index
                    combined = self._restore_zeroed_volume(combined, existing, touched)
                    combined = self._restore_candle_telemetry(combined, existing, touched)
                    if len(combined) > self.max_candles:
                        combined = combined.iloc[-self.max_candles:]
                    self._candles_1min[key] = combined
                else:
                    self._candles_1min[key] = new_data.iloc[-self.max_candles:]
                
                # Resample to higher timeframes (Merge instead of Overwrite).
                #
                # Deferring NEVER drops a row: the labels skipped here are accumulated
                # and handed to the next resample, and a closing bar always passes
                # throttle_resample=False, so every finished 1-minute bar flushes the
                # backlog before anything can read a stale closed bucket. Only the
                # forming bucket can lag, by at most _resample_min_interval.
                touched = new_data.index
                pending = self._pending_resample_touched.get(key)
                if pending is not None and len(pending):
                    touched = pending.union(touched)
                now_mono = time.monotonic()
                # Never defer the FIRST build of a higher-timeframe frame.
                #
                # _resample_and_merge returns early until the 1min frame holds 5 rows, so
                # the update that crosses that threshold is the one that creates the 5min
                # and 15min frames at all. Throttling that one leaves get_candles("5min")
                # returning None until the next bar close -- caught by
                # tests/test_index_volume_fallback_wiring_2026_08_02.py, which drove five
                # minutes of ticks and then read an empty frame. Deferring an UPDATE to an
                # existing aggregate is the optimisation; deferring its construction is a
                # bug.
                cold = (
                    self._candles_5min.get(key) is None
                    or self._candles_15min.get(key) is None
                )
                due = cold or (not throttle_resample) or (
                    now_mono - self._last_resample_at.get(key, 0.0)
                    >= self._resample_min_interval
                )
                if due:
                    self._last_resample_at[key] = now_mono
                    self._pending_resample_touched.pop(key, None)
                    self._resample_and_merge(instrument, touched_index=touched)
                else:
                    self._pending_resample_touched[key] = touched
                if mark_receipt:
                    self._note_candle_receipt(instrument, "1min", received_at)
                    if instrument in self._candles_5min and self._candles_5min[instrument] is not None:
                        self._note_candle_receipt(instrument, "5min", received_at)
                    if instrument in self._candles_15min and self._candles_15min[instrument] is not None:
                        self._note_candle_receipt(instrument, "15min", received_at)

            elif timeframe == "5min":
                self._candles_5min[key] = self._merge_candles(self._candles_5min.get(key), new_data)
                if mark_receipt:
                    self._note_candle_receipt(instrument, "5min", received_at)
            elif timeframe == "15min":
                self._candles_15min[key] = self._merge_candles(self._candles_15min.get(key), new_data)
                if mark_receipt:
                    self._note_candle_receipt(instrument, "15min", received_at)

    def _note_candle_receipt(
        self,
        instrument: str,
        timeframe: str,
        received_at: Optional[datetime] = None,
    ) -> None:
        """Record a successful candle-store mutation (caller holds ``_lock``)."""
        if received_at is None:
            receipt = datetime.now()
        elif isinstance(received_at, datetime):
            receipt = received_at
        else:
            # Accept pandas/ISO timestamps at transport seams, but never replace a
            # malformed explicit receipt with "now"—that would turn a bad clock into
            # false freshness and let stale OHLC drive a live exit or entry.
            try:
                parsed = pd.Timestamp(received_at)
                if pd.isna(parsed):
                    return
                receipt = parsed.to_pydatetime()
            except Exception:
                return
        self._last_candle_receipt.setdefault(instrument, {})[timeframe] = receipt

    def latest_candle_receipt(self, instrument: str, timeframe: str):
        """Return the local receipt time of the latest accepted candle mutation."""
        with self._lock:
            return (self._last_candle_receipt.get(instrument, {}) or {}).get(timeframe)

    def latest_quote_receipt(self, instrument: str):
        """Return the quote-lane receipt time without treating it as candle freshness."""
        with self._lock:
            return self._last_quote_receipt.get(instrument)

    @staticmethod
    def _restore_zeroed_volume(combined, existing, touched_index=None):
        """Never let a zero-volume bar overwrite a bar that already had real volume.

        The Yahoo fallback returns volume=0 for Indian index tickers (verified: ^NSEI /
        ^NSEBANK report 0 on every bar), and AngelOne history rate-limiting forces that
        fallback constantly. Because the merge keeps the LAST duplicate, those zeros were
        overwriting genuine AngelOne volume — every index showed vol_sum=0 from
        2026-07-27, which silently disabled every volume-gated entry path
        (volume_spike_entry, reclaim/cascade volume votes, volume intelligence).
        """
        try:
            if combined is None or existing is None:
                return combined
            if "volume" not in getattr(combined, "columns", []) or "volume" not in getattr(existing, "columns", []):
                return combined
            prior = existing[~existing.index.duplicated(keep="last")]["volume"]
            candidates = combined.index if touched_index is None else pd.Index(touched_index)
            overlap = candidates.intersection(combined.index).intersection(prior.index)
            if len(overlap) == 0:
                return combined
            current = pd.to_numeric(combined.loc[overlap, "volume"], errors="coerce").fillna(0.0)
            previous = pd.to_numeric(prior.loc[overlap], errors="coerce").fillna(0.0)
            restore = (current <= 0) & (previous > 0)
            if bool(restore.any()):
                combined.loc[overlap[restore], "volume"] = previous[restore]
        except Exception as exc:
            logger.debug(f"Volume preservation skipped: {exc}")
        return combined

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
            touched = new_data.index if new_data is not None else None
            combined = self._restore_zeroed_volume(combined, existing, touched)
            combined = self._restore_candle_telemetry(combined, existing, touched)
            if len(combined) > self.max_candles:
                combined = combined.iloc[-self.max_candles:]
            return combined
        
        return new_data.iloc[-self.max_candles:] if new_data is not None else None

    @staticmethod
    def _restore_candle_telemetry(combined, existing, touched_index=None):
        """Keep live telemetry when a historical refresh overlaps a candle.

        Historical providers generally return OHLCV only.  Their duplicate row must
        not erase tick counts, volume units, or receipt/provenance attached by the
        websocket builder.  Zero/blank values are treated as missing telemetry; a
        genuinely populated incoming value still wins.
        """
        if combined is None or existing is None or len(combined) == 0 or len(existing) == 0:
            return combined
        fields = ("tick_count", "volume_basis", "source", "receipt_at", "provider_time")
        candidates = combined.index if touched_index is None else pd.Index(touched_index)
        overlap = candidates.intersection(combined.index).intersection(existing.index)
        if len(overlap) == 0:
            return combined

        # ONE write per FIELD, not one per (row, field).
        #
        # pandas 3.0 made the default string dtype PyArrow-backed, and an Arrow array is
        # IMMUTABLE: every `combined.loc[idx, field] = prior` on a string column such as
        # `source` rebuilds that entire column. This function runs on the Kite tick path
        # (on_ticks -> update_from_tick -> _resample_and_merge -> _merge_candles), so the
        # cost was paid per tick, holding the GIL, which starves every asyncio loop in
        # the process.
        #
        # Measured on 2026-08-31 with pandas 3.0.2, a history refresh overlapping the
        # cache: 35ms at 50 overlapping rows, 130ms at 200, 327ms at 500, 670ms at 1000.
        # The live symptom was a scan cycle of 75.7s, five completed cycles in eleven
        # minutes, and "Error in periodic trades updater: TimeoutError" every six seconds
        # as the dashboard's 5s budget was never serviceable. py-spy showed exactly one
        # active thread in the whole process, GIL-held, inside this call.
        #
        # Duplicate index labels make `.loc[idx]` return a FRAME rather than a row, which
        # this logic does not model, so that case keeps the original per-cell path.
        indexes_unique = bool(combined.index.is_unique and existing.index.is_unique)

        usable = [f for f in fields if f in combined.columns and f in existing.columns]
        if not usable:
            return combined

        def _gather(frame, columns):
            """{label: {column: value}} for the overlapping labels only.

            `frame.loc[overlap, columns].to_dict("index")` is the obvious spelling and is
            right when the overlap is large, but it rebuilds a DataFrame of Arrow columns
            to read a handful of cells: 0.90ms on a 2000-row frame to fetch ONE row, paid
            twice, on every tick, holding _lock. Below the crossover a positional scalar
            gather is ~20x cheaper; above it the vectorised slice wins, so keep both.
            """
            if len(overlap) > 64:
                return frame.loc[overlap, columns].to_dict("index")
            positions = frame.index.get_indexer(overlap)
            series = {column: frame[column] for column in columns}
            return {
                label: {column: values.iloc[position] for column, values in series.items()}
                for label, position in zip(overlap, positions)
                if position >= 0
            }

        old_columns = usable + (["source"] if "source" not in usable and "source" in existing.columns else [])
        new_columns = usable + (["source"] if "source" not in usable and "source" in combined.columns else [])
        old_map = _gather(existing, old_columns) if indexes_unique else None
        new_map = _gather(combined, new_columns) if indexes_unique else None

        pending: dict = {field: {} for field in usable}
        for idx in overlap:
            old = old_map[idx] if indexes_unique else existing.loc[idx]
            new = new_map[idx] if indexes_unique else combined.loc[idx]
            incoming_history = str(new.get("source", "") or "").lower() in {
                "fyers_history", "history"
            }
            prior_source = str(old.get("source", "") or "").lower()
            for field in usable:
                value = new.get(field)
                missing = value is None or (isinstance(value, str) and not value.strip())
                if not missing:
                    try:
                        missing = bool(pd.isna(value))
                    except (TypeError, ValueError):
                        pass
                if incoming_history and prior_source not in {"", "history", "fyers_history"}:
                    missing = True
                if field == "tick_count" and not missing:
                    try:
                        missing = float(value or 0) <= 0
                    except (TypeError, ValueError):
                        missing = True
                if missing:
                    prior = old.get(field)
                    if prior is not None:
                        pending[field][idx] = prior

        for field, updates in pending.items():
            if not updates:
                continue
            # Rebuild the column in Python, then assign it ONCE.
            #
            # Writing through `.loc[keys, field]` instead looks equivalent but is not:
            # pandas resolves the target dtype once for the whole array, so restoring a
            # NaN into an int64 tick_count raises "Invalid value '[nan]' for dtype
            # 'int64'" where the old per-cell path silently upcast the column. Letting
            # the Series constructor infer from the merged values reproduces exactly
            # that upcast, and costs one column rebuild rather than one per cell.
            #
            # Equivalence with the old per-cell implementation -- values AND dtypes,
            # including the duplicate-index path -- is pinned by
            # tests/test_candle_telemetry_restore_is_not_quadratic_2026_08_31.py.
            column = combined[field]
            values = column.tolist()
            for position, key in zip(combined.index.get_indexer(list(updates)), updates):
                if position >= 0:
                    values[position] = updates[key]
            try:
                # Keep the column's own dtype. Left to infer, an all-None object column
                # becomes float64 and an object column of strings becomes str, which the
                # per-cell writes never did.
                rebuilt = pd.Series(values, index=combined.index, dtype=column.dtype)
            except (TypeError, ValueError):
                # The dtype genuinely cannot hold a restored value -- a NaN going back
                # into an int64 tick_count. Inference then reproduces the upcast the old
                # per-cell path got from pandas for free.
                rebuilt = pd.Series(values, index=combined.index)
            combined[field] = rebuilt
        return combined

    @staticmethod
    def _resample_window(base: pd.DataFrame, touched_index, freq: str) -> pd.DataFrame:
        """Return complete higher-timeframe buckets affected by ``touched_index``.

        A websocket update normally touches one 1-minute row.  Resampling the entire
        2,000-5,000 row cache for that row made the tick thread continuously own the
        GIL and candle lock.  The aggregate can only change in the bucket(s) containing
        the supplied timestamps, so select those complete buckets with two index
        searches.  Callers fall back to the full frame for unusual indexes.
        """
        if base is None or base.empty or touched_index is None:
            return base
        try:
            if not isinstance(base.index, pd.DatetimeIndex) or not base.index.is_monotonic_increasing:
                return base
            touched = pd.DatetimeIndex(touched_index)
            if touched.empty:
                return base.iloc[0:0]
            starts = touched.floor(freq)
            start = starts.min()
            end = starts.max() + pd.tseries.frequencies.to_offset(freq)
            left = int(base.index.searchsorted(start, side="left"))
            right = int(base.index.searchsorted(end, side="left"))
            return base.iloc[left:right]
        except (TypeError, ValueError):
            # Mixed timezone/index shapes are handled correctly by the established
            # full resample path; they are history-refresh cases, not the tick hot path.
            return base

    def _resample_and_merge(self, instrument: str, touched_index=None):
        """Resample affected 1min buckets and merge them into cached higher frames."""
        key = instrument
        base = self._candles_1min.get(key)
        if base is None or len(base) < 5: return

        try:
            # Resample and merge for 5min
            existing_5m = self._candles_5min.get(key)
            source_5m = (
                base
                if existing_5m is None or len(existing_5m) == 0
                else self._resample_window(base, touched_index, "5min")
            )
            new_5m = self._resample_ohlcv(source_5m, '5min', basis_source=base)
            self._candles_5min[key] = self._merge_candles(self._candles_5min.get(key), new_5m)

            # Resample and merge for 15min
            existing_15m = self._candles_15min.get(key)
            source_15m = (
                base
                if existing_15m is None or len(existing_15m) == 0
                else self._resample_window(base, touched_index, "15min")
            )
            new_15m = self._resample_ohlcv(source_15m, '15min', basis_source=base)
            self._candles_15min[key] = self._merge_candles(self._candles_15min.get(key), new_15m)
        except Exception as e:
            logger.error(f"Resample merge error for {instrument}: {e}")

    def _resample(self, instrument: str):
        """Old resample - kept for backward compatibility if needed internally"""
        self._resample_and_merge(instrument)

    @staticmethod
    def _resample_ohlcv(
        df: pd.DataFrame, freq: str, basis_source: Optional[pd.DataFrame] = None
    ) -> pd.DataFrame:
        """Resample OHLCV data to a higher timeframe.

        ``basis_source`` supplies the frame the single-unit volume_basis decision is
        taken over, when that differs from the rows being aggregated.
        """
        # Map freq strings to pandas offset aliases
        freq_map = {"5min": "5min", "15min": "15min"}
        pd_freq = freq_map.get(freq, freq)

        agg = {
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum',
        }
        # Carry the volume unit up with the volume. Dropping it left 5min/15min frames
        # with no volume_basis at all, and the mixed-unit surge guard treats a missing
        # column as "one unit" — so the guard was inert on exactly the timeframes
        # _detect_power_move runs on.
        #
        # A python-level agg function here costs ~3.7x the whole resample at 2000 rows,
        # and this runs on EVERY tick for two timeframes. The overwhelmingly common case
        # is one unit for the entire frame, so detect that with a vectorised nunique()
        # and assign a scalar; only a frame that actually spans a failover pays for the
        # per-bucket function.
        basis_col = None
        if 'volume_basis' in df.columns:
            basis_col = df['volume_basis']
            # The single-unit decision is taken over basis_source -- the WHOLE 1min
            # cache -- and NOT over df, which since 2026-08-31 may be a narrow window of
            # only the buckets a tick touched.
            #
            # This branch stamps one scalar onto EVERY bucket, including buckets whose
            # own rows carry no label. So the label set of the input decides what an
            # unlabelled bucket inherits, and narrowing the input silently changed it:
            # a 5min bucket with no labelled row came out '' from a window where the
            # full frame gave it 'ticks'. Caught by a 25-replay tick-stream equivalence
            # harness, 1 divergence in 25.
            #
            # Consumers treat '' as "unknown is not a unit" and skip it, so the effect
            # was benign -- but the narrowing is a PERFORMANCE change and must not move
            # a gate input at all. Pinned by
            # tests/test_candle_restore_is_scoped_to_touched_rows_2026_08_31.py.
            reference = df
            if basis_source is not None and 'volume_basis' in getattr(
                basis_source, 'columns', []
            ):
                reference = basis_source
            reference_col = reference['volume_basis']
            labelled = reference_col[reference_col.astype(str).str.strip().ne('')
                                     & reference_col.notna()]
            if labelled.nunique() <= 1:
                single = str(labelled.iloc[0]) if len(labelled) else ''
            else:
                single = None
                agg['volume_basis'] = _basis_of_bucket
        resampled = df.resample(pd_freq, label='left', closed='left').agg(agg)
        if basis_col is not None and 'volume_basis' not in resampled.columns:
            resampled['volume_basis'] = single
        # dropna() must not discard a bar merely because its basis is unknown.
        return resampled.dropna(subset=[c for c in ('open', 'high', 'low', 'close') if c in resampled])

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
        # Writers publish by replacing the dict entry; they never mutate an already
        # published frame.  Taking the stable reference under the lock is sufficient,
        # and copying after release keeps readers from extending writer lock holds.
        return df.copy() if df is not None else None

    def latest_candle_time(self, instrument: str, timeframe: str):
        """Timestamp of the newest bar, without copying the frame.

        get_candles() copies everything it returns, which is right for callers that
        read the data but wasteful for one that only needs to know whether anything
        has changed since it last looked.
        """
        with self._lock:
            if timeframe == "1min":
                df = self._candles_1min.get(instrument)
            elif timeframe == "5min":
                df = self._candles_5min.get(instrument)
            elif timeframe == "15min":
                df = self._candles_15min.get(instrument)
            else:
                return None
            if df is None or len(df) == 0:
                return None
            return df.index[-1]

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
