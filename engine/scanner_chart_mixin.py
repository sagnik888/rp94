"""Chart-building mixin extracted from scanner.py (monolith slice #1).

Display-only functionality: chart cache building, disk hydration, display
frames, chart cache invalidation, dual-lane chart overlays, and snapshots.
Methods are byte-identical to their previous in-scanner versions; they run on
the Scanner instance via mixin inheritance (same pattern as
ScannerTelemetryMixin / ScannerRecalculationMixin).

Rollback: `git revert` of the extraction commit restores the monolith.
"""

from __future__ import annotations

import threading
import time
import math
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
from loguru import logger

from config.settings import get_settings
from trading.trade_manager import IST


class ScannerChartMixin:
    """Chart cache/display methods for Scanner (display-only, no trade logic)."""
    def _build_chart_from_cache(self, instrument: str, force: bool = False) -> Dict:
        if not getattr(self, "chart_stream_enabled", True):
            return {}
        # An unselected instrument has no display work. Do not let its live
        # analysis queue behind another instrument's private chart calculation.
        if instrument != getattr(self, "active_chart_instrument", "NIFTY"):
            return {}

        # Serialised because two callers can now reach this at once: the scan cycle on the
        # scanner event loop, and a dashboard chart request on the chart executor. Both
        # write self.chart_cache_<inst>_<tf> and self._chart_metadata_cache, and a build
        # interleaved with another build can publish a payload whose fingerprint belongs
        # to the other one — the cache would then look current while holding the wrong
        # bars. Builds are ~10-20ms, so contention is not a concern; correctness is.
        if not hasattr(self, "_chart_build_lock"):
            self._chart_build_lock = threading.RLock()
        with self._chart_build_lock:
            return self._build_chart_from_cache_locked(instrument, force)

    def _build_chart_from_cache_locked(self, instrument: str, force: bool = False) -> Dict:

        chart = {}
        active_tf = getattr(self, "active_chart_tf", "5min")
        active_inst = getattr(self, "active_chart_instrument", "NIFTY")

        if getattr(self, "_chart_metadata_cache", None) is None:
            self._chart_metadata_cache = {}

        for tf in ["1min", "5min", "15min"]:
            key = f"{instrument}_{tf}"

            # Optimization: Only build chart for the active instrument and timeframe!
            if instrument != active_inst or tf != active_tf:
                continue

            result = self._cached_results.get(key)
            df = self.candles.get_candles(instrument, tf)

            if df is None or len(df) == 0:
                df = self._hydrate_chart_frame_from_disk(instrument, tf, df)
            if df is None or len(df) == 0:
                continue
            if result is None:
                result = {}

            settings = get_settings()
            chart_engine = str(getattr(settings, "scanner_engine", "") or "").lower()
            if chart_engine == "ut_ema":
                # Read the native display view before fingerprinting: an intrabar
                # exit can add a marker without changing candle or signal counts.
                result = self._chart_result_for_display(instrument, tf, df, result)
            # S1+S2 can publish a new lifecycle snapshot without a new candle.
            # Track its result revision so a candle-current cache cannot hide markers.
            source_result_revision = (
                self._s12_chart_result_revision(result) if chart_engine == "s1s2_dual" else None
            )
            if chart_engine in ("ut_smart", "ut_smart_s1"):
                df = self._chart_display_frame(df)
                if df is None or df.empty:
                    continue
            last_row = df.iloc[-1]
            last_ts = df.index[-1]
            sig_count = len(result.get("signals", []))
            open_count = len(self.trades.open_trades) if hasattr(self, "trades") else 0
            dual_lane_enabled = bool(getattr(settings, "dual_lane_enabled", True))
            s1_signature = ()
            s12_signature = ()
            utema_signature = ()
            combined_forming_signature = ()
            display_as_of = None
            if chart_engine in ("s1", "quad_prime_s1", "ut_smart_s1"):
                from engine.s1_engine import params_from_settings

                # An existing entry can acquire an exit without changing either
                # candle count or signal count. Treat that as a structural change.
                s1_signature = (
                    params_from_settings(settings, instrument),
                    tuple(sorted(settings.get_ut_engine_params().items())) if chart_engine.endswith("_s1") else (),
                    tuple(
                        (str(sig.timestamp), str(sig.signal_type), float(sig.price),
                         str(getattr(sig, "s1_exit_time", "") or ""),
                         str(getattr(sig, "s1_exit_reason", "") or ""),
                         getattr(sig, "s1_exit_price", None))
                        for sig in result.get("signals", [])
                    ),
                )
            if chart_engine == "s1s2_dual":
                evaluation = result.get("s12_evaluation") or {}
                s12_signature = (
                    evaluation.get("signature"), evaluation.get("config_hash"),
                    bool(evaluation.get("current")),
                    tuple((str(getattr(sig, "s12_event_id", "") or ""),
                           str(getattr(sig, "s12_lane", "") or ""),
                           str(getattr(sig, "s12_exit_reason", "") or ""),
                           str(getattr(sig, "s12_exit_time", "") or ""))
                          for sig in result.get("signals", [])),
                )
            if chart_engine == "ut_ema":
                columns = [name for name in ("open", "high", "low", "close") if name in df]
                utema_signature = (
                    int(pd.util.hash_pandas_object(df[columns], index=True).sum()),
                    tuple((item.get("time"), item.get("engine"), item.get("action"),
                           item.get("text"), item.get("price"), item.get("color"))
                          for item in result.get("native_markers", []) or []),
                    tuple((item.get("name"), item.get("color"), tuple(item.get("values") or []))
                          for item in result.get("indicator_overlays", []) or []),
                )
            if chart_engine in ("ut_smart", "ut_smart_s1"):
                from engine import ut_smart_engine as uts
                from engine.uts_lane import intrabar_enabled

                # A confirmed terminal candle can appear without another candle
                # arriving. Parameter changes and historical candle corrections also
                # alter UTS markers while the newest timestamp remains unchanged.
                p = uts.params_from_settings(settings, tf)
                display_as_of = uts.evaluation_clock()
                session = uts.drop_off_session_bars(df, p.session_end)
                closed = session.loc[uts.closed_bar_mask(session, tf, display_as_of)]
                columns = [name for name in ("open", "high", "low", "close", "volume") if name in closed]
                closed_hash = int(pd.util.hash_pandas_object(closed[columns], index=True).sum())
                s1_signature = s1_signature + (
                    p, intrabar_enabled(settings), closed_hash,
                    closed.index[-1] if len(closed) else None,
                )
            
            if chart_engine in {'quad_prime_s1', 'ut_smart_s1'}:
                from engine.ut_smart_engine import evaluation_clock, closed_bar_mask

                dependencies = [(tf, df), *((dep, self.candles.get_candles(instrument, dep))
                                           for dep in ('5min', '15min') if dep != tf)]
                chart_clock = display_as_of if display_as_of is not None else evaluation_clock()
                closed_signatures, forming_signatures = [], []
                for dep, frame in dependencies:
                    if frame is None or frame.empty:
                        closed_signatures.append((dep, None))
                        forming_signatures.append((dep, None))
                        continue
                    columns = [column for column in ('open', 'high', 'low', 'close', 'volume')
                               if column in frame]
                    closed_mask = closed_bar_mask(frame, dep, chart_clock)
                    closed_hash = int(pd.util.hash_pandas_object(frame.loc[closed_mask, columns], index=True).sum())
                    forming_hash = int(pd.util.hash_pandas_object(frame.loc[~closed_mask, columns], index=True).sum())
                    closed_signatures.append((dep, len(frame), frame.index[-1], closed_hash))
                    forming_signatures.append((dep, forming_hash))
                # Closed corrections, new bars and dependency closes are immediate.
                # Forming ticks use the existing display floor and never alter the
                # native entry/exit caches or their freshness observations.
                s1_signature += (tuple(closed_signatures),)
                combined_forming_signature = tuple(forming_signatures)

            # Split the fingerprint. The structural half changes when the chart's SHAPE
            # changes -- a new candle, a new signal, a trade opening or closing -- and must
            # never be delayed. The volatile half is the forming bar's close and volume,
            # which move on every tick, so including them in one flat fingerprint meant the
            # cache could not hit while the market moved: NIFTY 5min rebuilt all 300
            # candles 3,011 times in the 2026-09-16 session, 3.0 rebuilds per scan cycle,
            # for a payload that was structurally identical.
            structural = (
                len(df),
                last_ts,
                sig_count,
                open_count,
                chart_engine,
                dual_lane_enabled,
                s1_signature,
                s12_signature,
                utema_signature,
            )
            volatile = (
                float(last_row['close']),
                float(last_row['volume']) if 'volume' in last_row else 0,
            )
            if chart_engine in ("ut_smart", "ut_smart_s1", "ut_ema"):
                # A changed wick moves ATR/exit EMA overlays even if the last close
                # and volume did not move. Keep the existing tick rebuild floor.
                volatile += (float(last_row['open']), float(last_row['high']), float(last_row['low']))
            if combined_forming_signature:
                volatile += (combined_forming_signature,)
            fingerprint = structural + volatile

            cache_attr = f"chart_cache_{instrument}_{tf}"
            cached = getattr(self, cache_attr, None) if hasattr(self, cache_attr) else None
            if not force and cached is not None:
                previous = self._chart_metadata_cache.get(key)
                if previous == fingerprint:
                    chart[tf] = cached.get("data", {})
                    continue
                # Nothing structural moved, only the forming bar ticked. Serve the cached
                # payload until the rebuild floor elapses so the chart still tracks price
                # without paying a full rebuild per tick. A structural change skips this
                # branch entirely and rebuilds at once.
                if isinstance(previous, tuple) and previous[:len(structural)] == structural:
                    try:
                        floor = float(getattr(
                            get_settings(), "chart_rebuild_min_interval_seconds", 2.0
                        ) or 0.0)
                    except (TypeError, ValueError):
                        floor = 2.0
                    built_at = float(cached.get("built_at", 0.0) or 0.0)
                    if floor > 0.0 and (time.time() - built_at) < floor:
                        chart[tf] = cached.get("data", {})
                        continue

            self._chart_metadata_cache[key] = fingerprint

            volume_sum = float(df["volume"].sum()) if "volume" in df.columns else 0.0
            if len(df) <= 5 or volume_sum <= 0:
                df = self._hydrate_chart_frame_from_disk(instrument, tf, df)

            df = self._chart_display_frame(df)
            if df is None or len(df) == 0:
                continue
            if chart_engine != "ut_ema":
                result = self._chart_result_for_display(instrument, tf, df, result)
            result = self._chart_result_with_adaptive_dual_lanes(instrument, tf, df, result)

            candle_type = "standard"
            if chart_engine == "s1":
                from engine.s1_engine import params_from_settings, s1_candle_frame

                candle_type = params_from_settings(settings, instrument).candle_type
                # Transform before cropping so HA open retains the full warmup.
                # The stored candles and order references remain ordinary OHLC.
                df = s1_candle_frame(df, candle_type)

            # Optimization: REAL mode only needs a compact live chart window; historical/backtest keeps depth.
            chart_limit = 300 if self.mode == "REAL" else 1000
            df = df.tail(chart_limit)
            if "trailing_stop" in result:
                result["trailing_stop"] = result["trailing_stop"][-chart_limit:]
            if "trailing_stop_colors" in result:
                result["trailing_stop_colors"] = result["trailing_stop_colors"][-chart_limit:]

            def chart_epoch_seconds(value) -> int:
                ts = pd.Timestamp(value)
                if ts.tzinfo is None:
                    ts = ts.tz_localize(IST)
                else:
                    ts = ts.tz_convert(IST)
                return int(ts.timestamp())

            # UNIX timestamp conversion for chart display.
            # Candle timestamps are stored as naive IST; convert them to true epoch seconds.
            if isinstance(df.index, pd.DatetimeIndex):
                timestamps = [chart_epoch_seconds(ts) for ts in df.index]
            else:
                timestamps = (df.index.astype('int64') // 10**9).tolist() if hasattr(df.index, 'astype') else list(range(len(df)))
            opens = df['open'].round(2).tolist()
            highs = df['high'].round(2).tolist()
            lows = df['low'].round(2).tolist()
            closes = df['close'].round(2).tolist()
            volumes = df['volume'].astype(int).tolist()

            candles_list = [
                {"time": int(t), "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": int(v)}
                for t, o, h, l, c, v in zip(timestamps, opens, highs, lows, closes, volumes)
            ]

            ts_data = result.get("trailing_stop", [])
            ts_colors = result.get("trailing_stop_colors", [])
            logger.info(f"📈 Chart Build: {instrument} {tf} -> Candles: {len(candles_list)}, TS Points: {len(ts_data)}")
            # Build ts_line efficiently without per-element Python loop
            n = len(candles_list)
            last_val = ts_data[-1] if ts_data else 0
            last_color = ts_colors[-1] if ts_colors else "gray"
            if len(ts_data) == 0:
                ts_line = []
            else:
                padded_vals = list(ts_data) + [last_val] * max(0, n - len(ts_data))
                padded_colors = list(ts_colors) + [last_color] * max(0, n - len(ts_colors))
                ts_line = [
                    {"time": candles_list[i]["time"], "value": round(float(padded_vals[i]), 2), "color": padded_colors[i]}
                    if padded_vals[i] is not None and math.isfinite(float(padded_vals[i]))
                    else {"time": candles_list[i]["time"]}
                    for i in range(n)
                ]

            # Engine-specific indicator overlays (EMA / Supertrend lines). The engine
            # computes them over the FULL frame, but the chart shows only the last
            # `n` candles — so tail-align the values to the visible candles (the
            # trailing-stop line is sliced the same way). Without this the oldest
            # values get pinned to recent timestamps and the line renders flat/off-screen.
            overlays_out = []
            for ov in (result.get("indicator_overlays") or []):
                vals = ov.get("values") or []
                if not vals:
                    continue
                vals_tail = list(vals)[-n:]
                cand_tail = candles_list[-len(vals_tail):]
                data = [
                    {"time": cand_tail[i]["time"], "value": vals_tail[i]}
                    for i in range(len(vals_tail))
                    if vals_tail[i] is not None
                ]
                if data:
                    overlays_out.append({
                        "name": str(ov.get("name", "")),
                        "color": str(ov.get("color", "#888888")),
                        "data": data[-1000:],
                    })

            # Signal markers
            markers = []

            # Use signal timestamp directly instead of bar_index (Fixes markers disappearing on sliced data)
            for sig in ([] if chart_engine == "ut_ema" else result.get("signals", [])):
                try:
                    ts_obj = sig.timestamp
                    sig_time = chart_epoch_seconds(ts_obj)
                except Exception:
                    sig_time = int(sig.timestamp.timestamp())
                risk_pts = round(sig.stop_distance, 2)

                # Combo engines tag each flip with its source. Adaptive Dual
                # uses explicit lane tags so the chart proves Lane A and APEX
                # Lane B are both running instead of hiding behind UT/EMA text.
                src = getattr(sig, "source_engine", "") or ""
                lane = str(getattr(sig, "source_lane", "") or "").upper()
                label_price = getattr(sig, "s1_chart_signal_close", None) if src == "s1" else None
                label_price = float(label_price) if label_price is not None else float(sig.price)
                tag_map = {"utbot": " UT", "ema": " EMA", "supertrend": " ST", "consensus": " CONS", "both": " ★", "multi": " ★", "auto": " AUTO", "s1": " S1", "ut_smart": " UTS", "s1s2_dual": f" {lane or 'D1'}"}
                if lane == "A":
                    src_tag = " L1 AH"
                    buy_color = "#06b6d4"
                    sell_color = "#fb7185"
                elif lane == "B":
                    src_tag = " L2 APEX"
                    buy_color = "#a3e635"
                    sell_color = "#f97316"
                else:
                    src_tag = tag_map.get(src, "")
                    # "both"/agreement gets a brighter accent so confluence stands out.
                    if src == "s1s2_dual":
                        buy_color = sell_color = "#ff9900" if lane == "D1" else "#cc66ff"
                    else:
                        buy_color = "#16a34a" if src in {"both", "multi"} else "#22c55e"
                        sell_color = "#dc2626" if src in {"both", "multi"} else "#ef4444"

                if sig.signal_type == "BUY":
                    tgt = round(sig.price + sig.stop_distance * 1.5, 2)
                    label = f"BUY{src_tag} @ {label_price:.0f}"
                    markers.append({
                        "time": sig_time,
                        "position": "belowBar", "color": buy_color,
                        "shape": "arrowUp", "text": label,
                    })
                else:
                    tgt = round(sig.price - sig.stop_distance * 1.5, 2)
                    label = f"SELL{src_tag} @ {label_price:.0f}"
                    markers.append({
                        "time": sig_time,
                        "position": "aboveBar", "color": sell_color,
                        "shape": "arrowDown", "text": label,
                    })

                from engine.s1_lane import lane_exit_fields

                exit_reason, exit_time, _exit_price = lane_exit_fields(sig)
                if src in {"s1", "ut_smart"} and exit_time is not None and exit_reason:
                    direction = "LONG" if sig.signal_type == "BUY" else "SHORT"
                    markers.append({
                        "time": chart_epoch_seconds(exit_time),
                        "position": "aboveBar" if direction == "LONG" else "belowBar",
                        "color": "#ff9800", "shape": "circle",
                        "text": f"EXIT {direction} {'S1' if src == 's1' else 'UTS'} | {exit_reason}",
                    })

            # Native SETUP/ENTRY/FILL/T1-T3/EXIT events are kept separate from
            # signal arrows so both independent lanes and every lifecycle event show.
            for item in result.get("s12_markers") or []:
                try:
                    stamp = chart_epoch_seconds(item.get("time"))
                    lane = str(item.get("lane") or "").upper()
                    event = str(item.get("event") or "")
                    side = str(item.get("side") or "LONG").upper()
                    color = "#ff9900" if lane == "D1" else "#cc66ff"
                    is_long = side in {"LONG", "BUY"}
                    markers.append({
                        "time": stamp,
                        "position": "belowBar" if is_long else "aboveBar",
                        "color": color,
                        "shape": "circle" if event in {"EXIT", "CANCEL", "T1", "T2", "T3"} else "square",
                        "text": f"{lane} {event}" + (f" | {item.get('reason')}" if item.get("reason") else ""),
                    })
                except (TypeError, ValueError, AttributeError):
                    continue

            if chart_engine == "ut_ema":
                # Core times are already UTC epoch seconds. Treating those numbers
                # as pandas timestamps would reinterpret them as nanoseconds.
                for item in result.get("native_markers", []) or []:
                    try:
                        marker = dict(item)
                        marker["time"] = int(marker["time"])
                        if marker.get("engine") not in {"UT", "EMA"}:
                            continue
                        markers.append(marker)
                    except (TypeError, ValueError, KeyError):
                        continue

            # Truncate lists safely to max 1000 candles
            sliced_candles = candles_list[-1000:]
            sliced_ts = ts_line[-1000:]

            # Filter markers to only those that fall within our VISIBLE candle data range
            if sliced_candles:
                first_ts = sliced_candles[0]["time"]
                last_ts = sliced_candles[-1]["time"]
                markers = [m for m in markers if first_ts <= m["time"] <= last_ts]
            markers.sort(key=lambda marker: marker["time"])

            intel_history = self.intel_snapshots.history(
                instrument,
                tf,
                times=[c["time"] for c in sliced_candles],
                limit=len(sliced_candles),
            )
            res = {
                "engine": chart_engine,
                "candle_type": candle_type,
                "candles": sliced_candles,
                "trailing_stop": sliced_ts,
                "indicator_overlays": overlays_out,
                "markers": markers[-160:] if chart_engine == "ut_ema" else markers[-100:],
                "state": result.get("state", {}),
                "adaptive_dual_chart": result.get("adaptive_dual_chart", {}),
                "intel_history": intel_history,
            }
            if len(res["candles"]) > 0:
                logger.info(f"Sending chart data for {key}: first_ts={res['candles'][0]['time']}, last_ts={res['candles'][-1]['time']}")
            chart[tf] = res
            # Save to optimization cache (required for get_latest_results and
            # low-latency chart REST refreshes).
            setattr(
                self,
                f"chart_cache_{instrument}_{tf}",
                {
                    "data": res,
                    "simulation_id": self.simulation_id,
                    "built_at": time.time(),
                    "result_revision": source_result_revision,
                },
            )
        return chart

    def _hydrate_chart_frame_from_disk(
        self,
        instrument: str,
        timeframe: str,
        current_df: Optional[pd.DataFrame] = None,
    ) -> Optional[pd.DataFrame]:
        """Load chart candles from the local cache when memory is empty after restart."""
        current_len = len(current_df) if current_df is not None else 0
        current_volume = 0.0
        if current_df is not None and not current_df.empty and "volume" in current_df.columns:
            try:
                current_volume = float(current_df["volume"].sum())
            except Exception:
                current_volume = 0.0

        disk_path = Path("data_store") / "candles" / f"{instrument}_{timeframe}.csv"
        if not disk_path.exists():
            return current_df

        try:
            disk_df = pd.read_csv(disk_path, parse_dates=["timestamp"])
            if disk_df.empty or "timestamp" not in disk_df.columns:
                return current_df
            disk_volume = float(disk_df["volume"].sum()) if "volume" in disk_df.columns else 0.0
            if current_len and len(disk_df) <= current_len and disk_volume <= current_volume:
                return current_df
            disk_df = disk_df.set_index("timestamp").sort_index()
            disk_df.columns = [str(c).lower() for c in disk_df.columns]
            try:
                self.candles.update_candles(
                    instrument, disk_df, timeframe, mark_receipt=False
                )
            except TypeError as exc:
                # Lightweight/legacy candle facades may predate the receipt-clock
                # keyword. They cannot record receipt provenance, so falling back to
                # their three-argument API cannot accidentally freshen that clock.
                if "mark_receipt" not in str(exc):
                    raise
                self.candles.update_candles(instrument, disk_df, timeframe)
            return disk_df
        except Exception as e:
            logger.debug(f"Chart disk hydration skipped for {instrument}_{timeframe}: {e}")
            return current_df

    def _chart_display_frame(self, df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        """Return chart-safe candles without pre/post-market zero-volume placeholders."""
        if df is None or df.empty:
            return df
        if (str(getattr(get_settings(), "scanner_engine", "") or "").lower() in {"ut_smart", "ut_smart_s1"}
                and isinstance(df.index, pd.DatetimeIndex)):
            from engine import ut_smart_engine as uts
            frame = df.copy()
            if frame.index.tz is not None:
                frame.index = frame.index.tz_convert(IST).tz_localize(None)
            # Display the same source/session as the UTS engine. This excludes the
            # verified FYERS frozen final-session tail from bars as well as markers.
            return uts.drop_off_session_bars(frame, uts.params_from_settings(get_settings()).session_end)
        if getattr(self, "mode", "") != "REAL" or not isinstance(df.index, pd.DatetimeIndex):
            return df

        try:
            frame = df.copy()
            index = frame.index
            if index.tz is not None:
                index = index.tz_convert(IST).tz_localize(None)
                frame.index = index

            required = {"open", "high", "low", "close"}
            if not required.issubset(set(frame.columns)):
                return frame

            in_session = (index.time >= dtime(9, 15)) & (index.time <= dtime(15, 30))
            volume = (
                pd.to_numeric(frame.get("volume", 0), errors="coerce")
                .fillna(0)
                .astype(float)
            )
            keep = in_session | volume.gt(0).to_numpy()
            filtered = frame.loc[keep]
            if not filtered.empty:
                return filtered
        except Exception as exc:
            logger.debug(f"Chart display candle filter skipped: {exc}")
        return df

    def invalidate_chart_caches(self) -> None:
        """Drop chart/result caches so the next build reflects a changed signal engine.
        Called when the scanner_engine is switched, so the chart's entry/exit markers
        immediately re-render from the new engine instead of serving a stale payload."""
        try:
            self._cached_results.clear()
        except Exception:
            pass
        try:
            self._chart_metadata_cache.clear()
        except Exception:
            pass
        for attr in list(self.__dict__.keys()):
            if attr.startswith("chart_cache_"):
                try:
                    delattr(self, attr)
                except Exception:
                    pass

    def _chart_result_for_display(
        self,
        instrument: str,
        timeframe: str,
        df: pd.DataFrame,
        result: Optional[Dict],
    ) -> Dict:
        """Ensure chart snapshots have results for the selected candle source."""
        result = dict(result or {})
        settings = get_settings()
        selected_engine = str(getattr(settings, "scanner_engine", "") or "").lower()

        if selected_engine == "ut_ema":
            # No trailing-stop curve is required: the uploaded Pine deliberately
            # exposes only its active protection/target lines and native markers.
            def native_view(candidate):
                return (isinstance(candidate, dict)
                        and candidate.get("source_engine") == "ut_ema"
                        and isinstance(candidate.get("state"), dict)
                        and isinstance(candidate.get("native_markers"), (list, tuple)))

            key = f"{instrument}_{timeframe}"
            engine = getattr(getattr(self, "mtf", None), "engines", {}).get(key)
            chart_reader = getattr(engine, "chart_result", None)
            if (getattr(engine, "signal_source", "") == "ut_ema" and callable(chart_reader)
                    and df is not None and len(df) >= 2):
                try:
                    displayed = chart_reader(df=df, instrument=instrument, timeframe=timeframe,
                                             as_of=datetime.now(IST))
                    if native_view(displayed):
                        return displayed
                except Exception as exc:
                    logger.debug("UT/EMA private chart view skipped: {}", type(exc).__name__)
            if native_view(result):
                return result
            cached = getattr(getattr(self, "mtf", None), "_result_cache", {}).get(key)
            if native_view(cached):
                return dict(cached)
            if df is None or len(df) < 2:
                return {}
            try:
                from engine.ut_ema_engine import UTEmaEngine

                displayed = UTEmaEngine().process(
                    df=df, instrument=instrument, timeframe=timeframe,
                    as_of=datetime.now(IST), realtime=False,
                )
                return displayed if native_view(displayed) else {}
            except Exception as exc:
                logger.debug("UT/EMA private chart replay skipped: {}", type(exc).__name__)
                return {}

        if selected_engine in {'quad_prime_s1', 'ut_smart_s1'}:
            from engine.combined_scanner import CombinedSignalEngine
            from engine.ut_smart_engine import evaluation_clock
            try:
                params = settings.get_ut_engine_params()
                params = {key: value for key, value in params.items() if key in {
                    'key_value', 'atr_period', 'use_heikin_ashi', 'signal_mode',
                    'adx_filter', 'adx_period', 'adx_threshold', 'strict_adx',
                    'session_filter', 'session_start', 'session_end'}}
                if timeframe == '1min':
                    params['signal_mode'] = 'realtime'
                engine = CombinedSignalEngine(selected_engine, auto_trend_adx=float(
                    getattr(settings, 'scanner_auto_trend_adx', 25.) or 25.), **params)
                extra = {f'df_{dep}': self.candles.get_candles(instrument, dep)
                         for dep in ('5min', '15min') if dep != timeframe}
                return engine.process(df=df, instrument=instrument, timeframe=timeframe,
                                      as_of=evaluation_clock(), **extra)
            except Exception as exc:
                logger.debug("Combined private chart calculation skipped: {}", type(exc).__name__)
                return {}

        if selected_engine == "ut_smart":
            # Display calculations cannot reuse or mutate the live engine/result
            # caches: those are admission/exit authority on the scanner thread.
            # Recompute only when the chart fingerprint actually calls for a build.
            from engine.alt_scanners import AltSignalEngine
            from engine.ut_smart_engine import evaluation_clock
            try:
                displayed = AltSignalEngine(signal_source="ut_smart").process(
                    df=df, instrument=instrument, timeframe=timeframe,
                    as_of=evaluation_clock(),
                )
                return displayed if isinstance(displayed, dict) else {}
            except Exception as exc:
                logger.debug("UTS private chart calculation skipped: {}", type(exc).__name__)
                return {}

        def source_matches(candidate):
            if selected_engine in {'quad_prime_s1', 'ut_smart_s1'}:
                return candidate.get('source_engine') == selected_engine and 'native_results' in candidate
            if selected_engine == "ut_smart":
                return candidate.get("source_engine") == "ut_smart"
            if selected_engine != "s1":
                return True
            from engine.s1_engine import params_from_settings

            return (candidate.get("source_engine") == "s1"
                    and candidate.get("candle_type") == params_from_settings(settings, instrument).candle_type)

        def chart_result_ready(candidate):
            if selected_engine != "s1s2_dual":
                return True
            evaluation = candidate.get("s12_evaluation") or {}
            return not isinstance(evaluation, dict) or evaluation.get("current", True) is not False

        if not source_matches(result):
            result = {}
        # Keep chart calculation independent from _prepare_live_signal_data(). That
        # method owns entry readiness and clears state before the first session bar;
        # applying it here removes every display overlay during closed/pre-open hours.
        if (result.get("trailing_stop") and result.get("state") and source_matches(result)
                and chart_result_ready(result)):
            return result

        key = f"{instrument}_{timeframe}"
        cached = getattr(getattr(self, "mtf", None), "_result_cache", {}).get(key)
        if (isinstance(cached, dict) and cached.get("trailing_stop") and cached.get("state")
                and source_matches(cached) and chart_result_ready(cached)):
            self._cached_results[key] = cached
            return dict(cached)

        if df is None or len(df) < 15:
            return result

        try:
            engine = getattr(getattr(self, "mtf", None), "engines", {}).get(key)
            if engine is None:
                return result
            cfg = self.config.get("indices", {}).get(instrument, {}) if hasattr(self, "config") else {}
            lots_map = getattr(self, "lots", {}) or {}
            extra = {}
            if str(getattr(engine, "signal_source", "") or "") in {
                "s1", "quad_prime_s1", "ut_smart_s1", "s1s2_dual"
            }:
                for dep in ("5min", "15min"):
                    if dep == timeframe:
                        continue
                    frame = getattr(getattr(self, "mtf", None), "_data_cache", {}).get(f"{instrument}_{dep}")
                    if frame is None or frame.empty:
                        frame = self.candles.get_candles(instrument, dep)
                    if frame is None or frame.empty:
                        frame = self._hydrate_chart_frame_from_disk(instrument, dep, frame)
                    extra[f"df_{dep}"] = self._chart_display_frame(frame)
            result = engine.process(
                df=df,
                instrument=instrument,
                timeframe=timeframe,
                capital=float(getattr(self, "capital_total", 100000.0) or 100000.0),
                risk_pct=float(getattr(self, "risk_fut_pct", 1.0) or 1.0),
                lots=int(lots_map.get(instrument, 1) or 1),
                lot_size=int(cfg.get("lot_size", 1) or 1),
                **extra,
            )
            if isinstance(result, dict) and source_matches(result):
                self._cached_results[key] = result
                return dict(result)
            result = {}
        except Exception as exc:
            logger.debug(f"Chart UT1 fallback processing skipped for {key}: {exc}")
        return result

    def _chart_result_with_adaptive_dual_lanes(
        self,
        instrument: str,
        timeframe: str,
        df: pd.DataFrame,
        result: Optional[Dict],
    ) -> Dict:
        """Add visible Lane A / Lane B signal streams to adaptive_dual charts."""
        result = dict(result or {})
        settings = get_settings()
        if str(getattr(settings, "scanner_engine", "") or "").lower() != "adaptive_dual":
            return result

        signals = list(result.get("signals") or [])
        for sig in signals:
            if not str(getattr(sig, "source_lane", "") or "").strip():
                try:
                    setattr(sig, "source_lane", "A")
                except Exception:
                    pass

        if (
            bool(getattr(settings, "dual_lane_enabled", True))
            and timeframe in {"5min", "15min"}
            and df is not None
            and len(df) >= 200
        ):
            try:
                from engine.apex_hybrid import build_apex_signals

                df_5m = df if timeframe == "5min" else self.candles.get_candles(instrument, "5min")
                df_15m = df if timeframe == "15min" else self.candles.get_candles(instrument, "15min")
                apex_signals = build_apex_signals(
                    df,
                    instrument=instrument,
                    timeframe=timeframe,
                    df_5m=df_5m,
                    df_15m=df_15m,
                    latest_only=False,
                )
            except Exception as exc:
                logger.debug(f"Adaptive Dual chart APEX lane skipped for {instrument} {timeframe}: {exc}")
                apex_signals = []

            seen = {
                (
                    str(getattr(sig, "source_lane", "") or "").upper(),
                    str(getattr(sig, "source_engine", "") or "").lower(),
                    str(getattr(sig, "signal_type", "") or "").upper(),
                    str(getattr(sig, "timestamp", "") or ""),
                    round(float(getattr(sig, "price", 0.0) or 0.0), 2),
                )
                for sig in signals
            }
            for sig in apex_signals:
                key = (
                    str(getattr(sig, "source_lane", "") or "").upper(),
                    str(getattr(sig, "source_engine", "") or "").lower(),
                    str(getattr(sig, "signal_type", "") or "").upper(),
                    str(getattr(sig, "timestamp", "") or ""),
                    round(float(getattr(sig, "price", 0.0) or 0.0), 2),
                )
                if key in seen:
                    continue
                seen.add(key)
                signals.append(sig)

        def sig_sort_key(sig):
            ts = getattr(sig, "timestamp", None)
            try:
                return pd.Timestamp(ts).timestamp()
            except Exception:
                return 0.0

        signals.sort(key=sig_sort_key)
        result["signals"] = signals
        result["adaptive_dual_chart"] = {
            "lane_A_signals": sum(1 for sig in signals if str(getattr(sig, "source_lane", "") or "").upper() == "A"),
            "lane_B_signals": sum(1 for sig in signals if str(getattr(sig, "source_lane", "") or "").upper() == "B"),
        }
        return result

    @staticmethod
    def _s12_chart_result_revision(result: Optional[Dict]):
        """Stable chart revision for S1+S2, including same-candle worker publications."""
        if not isinstance(result, dict):
            return None
        evaluation = result.get("s12_evaluation") or {}
        signals = tuple(
            (
                str(getattr(sig, "s12_event_id", "") or ""),
                str(getattr(sig, "s12_lane", "") or ""),
                str(getattr(sig, "timestamp", "") or ""),
                str(getattr(sig, "s12_exit_reason", "") or ""),
                str(getattr(sig, "s12_exit_time", "") or ""),
            )
            for sig in result.get("signals", []) or []
        )
        markers = tuple(
            (
                str(item.get("event") or ""),
                str(item.get("lane") or ""),
                str(item.get("time") or ""),
                str(item.get("trade_id") or ""),
                str(item.get("reason") or ""),
            )
            for item in result.get("s12_markers", []) or []
            if isinstance(item, dict)
        )
        return (
            bool(result.get("s12_pending")),
            str(evaluation.get("signature") or ""),
            str(evaluation.get("config_hash") or ""),
            bool(evaluation.get("current", True)),
            signals,
            markers,
        )

    def _chart_cache_matches_s12_result(self, cached: Dict, instrument: str, timeframe: str) -> bool:
        """Reject a candle-current S1+S2 chart when its cached lane result has moved."""
        if str(getattr(get_settings(), "scanner_engine", "") or "").lower() != "s1s2_dual":
            return True
        result = (getattr(self, "_cached_results", {}) or {}).get(f"{instrument}_{timeframe}")
        return cached.get("result_revision") == self._s12_chart_result_revision(result)

    def _chart_cache_matches_candles(self, cached: Dict, instrument: str, timeframe: str) -> bool:
        """Does the cached payload still end on the newest bar we hold?

        _build_chart_from_cache rebuilds ONLY the active instrument+timeframe, so every
        other chart's cache freezes the moment the operator switches away from it.
        get_chart_snapshot's currency test asked whether a payload EXISTED and whether
        the simulation_id matched — never whether the data was still true — and then
        stamped the response `"timestamp": datetime.now()`. Switch to another index and
        back and the chart renders bars from before the switch, presented as current.

        The scan loop refreshes it on its next cycle, so this usually self-heals within
        one cadence. It does not heal at all when the loop is stalled or the instrument
        has dropped out of active_indices, which are exactly the moments an operator is
        staring at the chart to work out what is wrong.
        """
        candles = getattr(self, "candles", None)
        if candles is None:
            return True
        latest = None
        reader = getattr(candles, "latest_candle_time", None)
        if str(getattr(get_settings(), "scanner_engine", "") or "").lower() in {"ut_smart", "ut_smart_s1"}:
            df = self._chart_display_frame(candles.get_candles(instrument, timeframe))
            if df is not None and len(df):
                latest = df.index[-1]
        elif callable(reader):
            latest = reader(instrument, timeframe)
        else:
            # Partial candle stores are a real pattern here (tests, replay harnesses).
            df = candles.get_candles(instrument, timeframe)
            if df is not None and len(df):
                latest = df.index[-1]
        if latest is None:
            return True

        bars = (cached.get("data") or {}).get("candles") or []
        if not bars:
            return False
        try:
            newest_shown = int(bars[-1].get("time") or 0)
            ts = pd.Timestamp(latest)
            ts = ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)
            return newest_shown >= int(ts.timestamp())
        except (TypeError, ValueError, AttributeError):
            # Cannot compare: rebuilding is the cheap, safe answer.
            return False

    def get_chart_snapshot(self, instrument: str = "NIFTY", timeframe: str = "5min") -> Dict[str, Any]:
        """Switch and return the active chart payload for dashboard tab changes."""
        valid_instruments = set(self.config.get("indices", {}).keys())
        instrument = (instrument or "NIFTY").upper()
        if instrument not in valid_instruments:
            instrument = "NIFTY"

        timeframe = timeframe if timeframe in {"1min", "5min", "15min"} else "5min"
        self.active_chart_instrument = instrument
        self.active_chart_tf = timeframe

        if not getattr(self, "chart_stream_enabled", True):
            return {
                "status": "ok",
                "instrument": instrument,
                "tf": timeframe,
                "chart": {},
                "chart_enabled": False,
                "simulation_id": self.simulation_id,
                "timestamp": datetime.now(IST).isoformat(),
            }

        cached = getattr(self, f"chart_cache_{instrument}_{timeframe}", None)
        cache_is_current = (
            isinstance(cached, dict)
            and cached.get("data")
            and int(cached.get("simulation_id") or 0) == int(getattr(self, "simulation_id", 0) or 0)
            and self._chart_cache_matches_candles(cached, instrument, timeframe)
            and self._chart_cache_matches_s12_result(cached, instrument, timeframe)
        )

        if str(getattr(get_settings(), "scanner_engine", "") or "").lower() in {
            "ut_smart", "quad_prime_s1", "ut_smart_s1", "s1s2_dual", "ut_ema"
        }:
            # Timestamp currency alone misses same-bar settings/clock changes. Let
            # the UTS structural/volatile fingerprint decide whether a rebuild is due.
            chart = self._build_chart_from_cache(instrument, force=not cache_is_current)
        elif cache_is_current:
            chart = {timeframe: cached["data"]}
        else:
            chart = self._build_chart_from_cache(instrument, force=True)
            if not chart.get(timeframe):
                cached = getattr(self, f"chart_cache_{instrument}_{timeframe}", None)
                if cached and cached.get("data"):
                    chart[timeframe] = cached["data"]

        return {
            "status": "ok",
            "instrument": instrument,
            "tf": timeframe,
            "chart": chart,
            "chart_enabled": True,
            "simulation_id": self.simulation_id,
            "timestamp": datetime.now(IST).isoformat(),
        }

