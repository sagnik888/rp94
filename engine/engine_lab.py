"""Shadow comparison for all scanner engines.

The lab is deliberately read-only with respect to the live execution lane.  It
shares candle data, but keeps separate engine instances, caches, and virtual
spot-level trade books so every engine can be compared during live market scans
without mutating ``settings.scanner_engine`` or submitting broker orders.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, Optional
import math

import pandas as pd

from config.settings import get_settings
from engine.alt_scanners import AltSignalEngine
from engine.ut_bot_core import UTBotEngine, UTBotSignal


ENGINE_LAB_MODES = (
    "utbot",
    "ema",
    "consensus",
    "utbot_consensus",
    "quad_union",
    "quad_prime",
    "adaptive_dual",
    "s1",
    "ut_smart",
    "ut_ema",
    "quad_prime_s1",
    "ut_smart_s1",
    "s1s2_dual",
)


def build_shadow_engine(mode: str, params: Dict[str, Any]) -> Any:
    """Build one engine without reading or changing the global active mode."""
    mode = str(mode or "utbot").lower()
    if mode == "utbot":
        return UTBotEngine(**params)
    if mode == "ut_ema":
        from engine.alt_scanners import UTEmaSignalEngine

        return UTEmaSignalEngine(synchronous=True)
    if mode == "s1s2_dual":
        from engine.alt_scanners import S12SignalEngine

        return S12SignalEngine(synchronous=True, **params)
    settings = get_settings()
    from engine.scanner_modes import COMBINED_MODES
    if mode in COMBINED_MODES:
        from engine.combined_scanner import CombinedSignalEngine
        return CombinedSignalEngine(mode, auto_trend_adx=float(
            getattr(settings, 'scanner_auto_trend_adx', 25.) or 25.), **params)
    return AltSignalEngine(
        signal_source=mode,
        auto_trend_adx=float(getattr(settings, "scanner_auto_trend_adx", 25.0) or 25.0),
        **params,
    )


@dataclass
class ShadowTrade:
    engine: str
    instrument: str
    timeframe: str
    direction: str
    entry_price: float
    entry_time: str
    stop: float
    target: float
    source_engine: str = ""
    source_engines: list[str] = field(default_factory=list)
    source_lane: str = ""
    signal_key: str = ""
    peak_points: float = 0.0
    trough_points: float = 0.0
    native_exit_time: Any = None
    native_exit_reason: str = ""
    native_stop_updates: list = field(default_factory=list)


class EngineLab:
    """Run all scanner engines in shadow and maintain virtual outcomes."""

    def __init__(self, engine_params: Dict[str, Any] | None = None) -> None:
        self._engine_params = self._sanitize_params(engine_params or {})
        self._engines: Dict[str, Any] = {}
        self._result_cache: Dict[str, Dict[str, Any]] = {}
        self._data_hash: Dict[str, int] = {}
        self._open: Dict[str, ShadowTrade] = {}
        self._closed: list[Dict[str, Any]] = []
        self._seen_signals: set[str] = set()
        self._last_snapshot: Dict[str, Any] = self._empty_snapshot()
        self._build_all()

    @staticmethod
    def _sanitize_params(params: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "key_value",
            "atr_period",
            "use_heikin_ashi",
            "signal_mode",
            "adx_filter",
            "adx_period",
            "adx_threshold",
            "strict_adx",
            "session_filter",
            "session_start",
            "session_end",
        }
        return {key: value for key, value in dict(params or {}).items() if key in allowed}

    def rebuild_if_params_changed(self, params: Dict[str, Any] | None) -> None:
        clean = self._sanitize_params(params or {})
        if clean != self._engine_params:
            self._engine_params = clean
            self._build_all()
            self._result_cache.clear()
            self._data_hash.clear()

    def _build_all(self) -> None:
        self._engines = {}
        for mode in ENGINE_LAB_MODES:
            for inst in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"):
                for tf in (("5min", "15min") if mode == "ut_ema" else ("1min", "5min", "15min")):
                    params = dict(self._engine_params)
                    if tf == "1min":
                        params["signal_mode"] = "realtime"
                    self._engines[f"{mode}|{inst}|{tf}"] = build_shadow_engine(mode, params)

    @staticmethod
    def _hash_df(df: Optional[pd.DataFrame], signal_mode: str = "realtime") -> int:
        if df is None or len(df) == 0:
            return 0
        try:
            ref_idx = -2 if signal_mode == "confirmed" and len(df) > 1 else -1
            row = df.iloc[ref_idx]
            idx = df.index[ref_idx]
            ts = idx.timestamp() if hasattr(idx, "timestamp") else hash(str(idx))
            return hash((
                len(df),
                ts,
                float(row.get("open", 0.0)),
                float(row.get("high", 0.0)),
                float(row.get("low", 0.0)),
                float(row.get("close", 0.0)),
                float(row.get("volume", 0.0)),
            ))
        except Exception:
            return hash(len(df))

    @staticmethod
    def _empty_snapshot() -> Dict[str, Any]:
        return {
            "enabled": True,
            "modes": list(ENGINE_LAB_MODES),
            "rows": [],
            "summary": {"engines": len(ENGINE_LAB_MODES), "signals": 0, "open": 0, "closed": 0},
            "updated_at": "",
        }

    @staticmethod
    def _signal_key(mode: str, instrument: str, tf: str, sig: UTBotSignal) -> str:
        if mode == "ut_ema" and getattr(sig, "utema_event_id", ""):
            return str(sig.utema_event_id)
        ts = getattr(sig, "timestamp", None)
        ts_key = ts.isoformat() if hasattr(ts, "isoformat") else str(ts or "")
        lane_suffix = "|" + EngineLab._source_lane(sig, mode) if mode in {"quad_prime_s1", "ut_smart_s1", "s1s2_dual"} else ""
        return lane_suffix + f"{mode}|{instrument}|{tf}|{getattr(sig, 'signal_type', '')}|{ts_key}|{getattr(sig, 'bar_index', -1)}"

    @staticmethod
    def _source_lane(item: Any, mode: str = "") -> str:
        mode_key = str(mode or getattr(item, "engine", "") or "").lower()
        if mode_key in {"quad_prime_s1", "ut_smart_s1"}:
            from engine.scanner_modes import owner
            native = owner(item)
            if native == 'quad_prime':
                return 'QP:' + EngineLab._source_lane(item, native)
            return native
        raw_lane = str(getattr(item, "source_lane", "") or "").strip()
        lane = raw_lane.upper()
        if lane in {"A", "B", "UTS", "D1", "D2", "UTEMA"}:
            return lane
        if mode_key in {"quad_union", "quad_prime", "utbot_ema", "utbot_consensus", "quality_hybrid"}:
            if raw_lane.lower() in {"utbot", "ema", "supertrend", "consensus"}:
                return raw_lane.lower()
            try:
                from engine.signal_processor import SignalProcessor

                return SignalProcessor._historical_replay_stream_key(item, mode_key)
            except Exception:
                pass
        source = str(getattr(item, "source_engine", "") or "").lower()
        if source in {"flow_burst", "apex_hybrid"}:
            return "B"
        if mode_key == "adaptive_dual":
            return "A"
        return ""

    @staticmethod
    def _lane_event_key(instrument: str, tf: str, sig: UTBotSignal) -> str:
        ts = getattr(sig, "timestamp", None)
        ts_key = ts.isoformat() if hasattr(ts, "isoformat") else str(ts or "")
        return f"{instrument}|{tf}|{ts_key}|{str(getattr(sig, 'signal_type', '') or '').upper()}"

    @staticmethod
    def _direction(sig: UTBotSignal) -> str:
        return "LONG" if str(getattr(sig, "signal_type", "")).upper() == "BUY" else "SHORT"

    @staticmethod
    def _trade_key(mode: str, instrument: str, tf: str = "", lane: str = "") -> str:
        base = f"{mode}|{instrument}|{tf or 'all'}"
        mode_key = str(mode or "").lower()
        if mode_key in {"quad_prime_s1", "ut_smart_s1", "s1s2_dual"}:
            return f"{base}|{lane}"
        if mode_key == "adaptive_dual" and str(lane or "").upper() in {"A", "B"}:
            return f"{base}|{str(lane or '').upper()}"
        if mode_key in {"quad_union", "quad_prime", "utbot_ema", "utbot_consensus", "quality_hybrid"} and str(lane or "").strip():
            return f"{base}|{str(lane or '').strip().lower()}"
        return base

    @staticmethod
    def _replay_trade_key(mode: str, instrument: str, tf: str, lane: str = "") -> str:
        mode_key = str(mode or "").lower()
        if mode_key in {"quad_prime_s1", "ut_smart_s1", "s1s2_dual"}:
            return f"{instrument}|{tf}|{lane}"
        if mode_key == "adaptive_dual" and str(lane or "").upper() in {"A", "B"}:
            return f"{instrument}|{tf}|{str(lane or '').upper()}"
        if mode_key in {"quad_union", "quad_prime", "utbot_ema", "utbot_consensus", "quality_hybrid"} and str(lane or "").strip():
            return f"{instrument}|{tf}|{str(lane or '').strip().lower()}"
        return f"{instrument}|{tf}"

    @staticmethod
    def _expanded_replay_entries(mode: str, entries: list[tuple[str, UTBotSignal]]) -> list[tuple[str, UTBotSignal]]:
        mode_key = str(mode or "").lower()
        if mode_key in {"quad_prime_s1", "ut_smart_s1"}:
            from engine.scanner_modes import COMBINED_MODES, owner
            base = COMBINED_MODES[mode_key]
            return EngineLab._expanded_replay_entries(base, [(tf, sig) for tf, sig in entries or [] if owner(sig) == base]) + [(tf, sig) for tf, sig in entries or [] if owner(sig) == 's1']
        if mode_key not in {"adaptive_dual", "quad_union", "quad_prime", "utbot_ema", "utbot_consensus", "quality_hybrid", "s1s2_dual"}:
            return list(entries or [])
        try:
            from engine.signal_processor import SignalProcessor

            by_tf: Dict[str, list[UTBotSignal]] = {}
            for tf, sig in entries or []:
                by_tf.setdefault(str(tf or ""), []).append(sig)
            expanded: list[tuple[str, UTBotSignal]] = []
            for tf, signals in by_tf.items():
                for stream in SignalProcessor._historical_replay_streams(signals, mode_key):
                    expanded.extend((tf, sig) for sig in stream)
            return expanded
        except Exception:
            return list(entries or [])

    @staticmethod
    def _pnl_points(trade: ShadowTrade, price: float) -> float:
        if trade.direction == "LONG":
            return float(price) - trade.entry_price
        return trade.entry_price - float(price)

    @staticmethod
    def _trade_from_signal(mode: str, instrument: str, tf: str, sig: UTBotSignal, now_iso: str) -> Optional[ShadowTrade]:
        direction = EngineLab._direction(sig)
        price = float(getattr(sig, "price", 0.0) or 0.0)
        if price <= 0:
            return None
        stop = float(getattr(sig, "trailing_stop", 0.0) or 0.0)
        stop_dist = abs(price - stop) or abs(float(getattr(sig, "stop_distance", 0.0) or 0.0)) or max(1.0, price * 0.001)
        target = price + (stop_dist * 1.5) if direction == "LONG" else price - (stop_dist * 1.5)
        mode_key = str(mode or "").lower()
        if mode_key == "s1s2_dual":
            from engine.s12_lane import admission_levels

            price = float(getattr(sig, "s12_engine_entry", 0.0) or price)
            levels = admission_levels(sig, price)
            stop = float(levels.get("s12_stop") or stop)
            target = 0.0  # T1-T3 are milestones; the native engine owns exits.
        ts = getattr(sig, "timestamp", None)
        entry_time = ts.isoformat() if hasattr(ts, "isoformat") else now_iso
        return ShadowTrade(
            engine=mode,
            instrument=instrument,
            timeframe=tf,
            direction=direction,
            entry_price=price,
            entry_time=entry_time,
            stop=stop if stop > 0 else (price - stop_dist if direction == "LONG" else price + stop_dist),
            target=target,
            source_engine=str(getattr(sig, "source_engine", "") or mode),
            source_engines=list(getattr(sig, "source_engines", []) or []),
            source_lane=EngineLab._source_lane(sig, mode),
            signal_key=EngineLab._signal_key(mode, instrument, tf, sig),
            native_exit_time=getattr(sig, "s12_exit_time", None),
            native_exit_reason=str(getattr(sig, "s12_exit_reason", "") or ""),
            native_stop_updates=list(getattr(sig, "s12_stop_updates", []) or []),
        )

    @staticmethod
    def _sharpe(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
        std = math.sqrt(variance)
        if std <= 0:
            return 0.0
        return (mean / std) * math.sqrt(len(values))

    @staticmethod
    def _signal_sort_key(sig: UTBotSignal) -> tuple[float, int]:
        ts = getattr(sig, "timestamp", None)
        if hasattr(ts, "timestamp"):
            try:
                ts_value = float(ts.timestamp())
            except Exception:
                ts_value = 0.0
        else:
            ts_value = 0.0
        return (ts_value, int(getattr(sig, "bar_index", -1) or -1))

    @staticmethod
    def _as_timestamp(value: Any) -> Optional[pd.Timestamp]:
        if value is None:
            return None
        try:
            return pd.Timestamp(value)
        except Exception:
            return None

    @staticmethod
    def _timeframe_minutes(tf: str) -> int:
        return {"1min": 1, "5min": 5, "15min": 15}.get(str(tf or "").lower(), 5)

    @staticmethod
    def _apex_replay_signals(
        df: pd.DataFrame,
        instrument: str,
        timeframe: str,
        df_5m: Optional[pd.DataFrame] = None,
        df_15m: Optional[pd.DataFrame] = None,
    ) -> list[UTBotSignal]:
        """Build Lane B replay signals from the APEX v5.5 five-gate engine."""
        settings = get_settings()
        if not bool(getattr(settings, "dual_lane_enabled", True)):
            return []
        try:
            from engine.apex_hybrid import build_apex_signals

            return build_apex_signals(
                df,
                instrument=instrument,
                timeframe=timeframe,
                df_5m=df_5m,
                df_15m=df_15m,
                latest_only=False,
            )
        except Exception:
            return []

    @staticmethod
    def _latest_close(candles_by_instrument: Dict[str, Dict[str, pd.DataFrame]], instrument: str, fallback: float) -> float:
        tf_map = candles_by_instrument.get(instrument) or {}
        for tf in ("1min", "5min", "15min"):
            frame = tf_map.get(tf)
            if frame is None or frame.empty or "close" not in frame:
                continue
            try:
                price = float(frame["close"].iloc[-1])
            except Exception:
                continue
            if price > 0:
                return price
        return float(fallback)

    def _close_trade(self, key: str, price: float, reason: str, now_iso: str) -> None:
        trade = self._open.pop(key, None)
        if trade is None:
            return
        pnl_points = self._pnl_points(trade, price)
        self._closed.append({
            "engine": trade.engine,
            "instrument": trade.instrument,
            "timeframe": trade.timeframe,
            "direction": trade.direction,
            "entry_price": round(trade.entry_price, 2),
            "exit_price": round(float(price), 2),
            "entry_time": trade.entry_time,
            "exit_time": now_iso,
            "exit_reason": reason,
            "pnl_points": round(pnl_points, 2),
            "peak_points": round(trade.peak_points, 2),
            "trough_points": round(trade.trough_points, 2),
            "source_engine": trade.source_engine,
            "source_lane": trade.source_lane,
        })
        if len(self._closed) > 1000:
            self._closed = self._closed[-1000:]

    def _open_or_reverse(self, mode: str, instrument: str, tf: str, sig: UTBotSignal, now_iso: str) -> None:
        lane = self._source_lane(sig, mode)
        key = self._trade_key(mode, instrument, tf, lane)
        direction = self._direction(sig)
        price = float(getattr(sig, "price", 0.0) or 0.0)
        if price <= 0:
            return
        existing = self._open.get(key)
        if existing is not None:
            if existing.direction == direction:
                return
            self._close_trade(key, price, "OPPOSITE_SIGNAL", now_iso)
        stop = float(getattr(sig, "trailing_stop", 0.0) or 0.0)
        stop_dist = abs(price - stop) or abs(float(getattr(sig, "stop_distance", 0.0) or 0.0)) or max(1.0, price * 0.001)
        target = price + (stop_dist * 1.5) if direction == "LONG" else price - (stop_dist * 1.5)
        self._open[key] = ShadowTrade(
            engine=mode,
            instrument=instrument,
            timeframe=tf,
            direction=direction,
            entry_price=price,
            entry_time=now_iso,
            stop=stop if stop > 0 else (price - stop_dist if direction == "LONG" else price + stop_dist),
            target=target,
            source_engine=str(getattr(sig, "source_engine", "") or mode),
            source_engines=list(getattr(sig, "source_engines", []) or []),
            source_lane=lane,
            signal_key=self._signal_key(mode, instrument, tf, sig),
        )

    def _mark_open_trade(self, mode: str, instrument: str, price: float, now_iso: str, tf: str = "") -> None:
        if price <= 0:
            return
        if tf:
            keys = [self._trade_key(mode, instrument, tf)]
        else:
            # No timeframe given: mark every open shadow trade for this engine and
            # instrument (trades are keyed per-TF, e.g. "utbot|NIFTY|5min").
            prefix = f"{mode}|{instrument}|"
            keys = [key for key in list(self._open.keys()) if key.startswith(prefix)]
        for key in keys:
            trade = self._open.get(key)
            if trade is None:
                continue
            pnl = self._pnl_points(trade, price)
            trade.peak_points = max(trade.peak_points, pnl)
            trade.trough_points = min(trade.trough_points, pnl)
            if trade.direction == "LONG":
                if price <= trade.stop:
                    self._close_trade(key, price, "STOP_HIT", now_iso)
                elif price >= trade.target:
                    self._close_trade(key, price, "TARGET_HIT", now_iso)
            else:
                if price >= trade.stop:
                    self._close_trade(key, price, "STOP_HIT", now_iso)
                elif price <= trade.target:
                    self._close_trade(key, price, "TARGET_HIT", now_iso)

    def _simulate_trade_path(
        self,
        trade: ShadowTrade,
        frame: Optional[pd.DataFrame],
        *,
        until: Any = None,
        include_until: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Walk OHLC after entry and return the first virtual stop/target hit."""
        if frame is None or frame.empty:
            return None
        entry_ts = self._as_timestamp(trade.entry_time)
        until_ts = self._as_timestamp(until)
        if entry_ts is None:
            return None
        try:
            data = frame.sort_index()
            idx = pd.DatetimeIndex(data.index)
            entry_cutoff = entry_ts + pd.Timedelta(minutes=self._timeframe_minutes(trade.timeframe))
            mask = idx >= entry_cutoff
            if until_ts is not None:
                mask &= idx <= until_ts if include_until else idx < until_ts
            path = data.loc[mask]
        except Exception:
            return None
        for ts, candle in path.iterrows():
            try:
                op = float(candle.get("open", candle.get("close", trade.entry_price)))
                high = float(candle.get("high", candle.get("close", trade.entry_price)))
                low = float(candle.get("low", candle.get("close", trade.entry_price)))
            except Exception:
                continue
            if trade.engine == "s1s2_dual":
                for update in trade.native_stop_updates:
                    try:
                        effective, level = update[0], float(update[1])
                        effective = self._as_timestamp(effective)
                        if effective is not None and effective <= pd.Timestamp(ts):
                            trade.stop = max(trade.stop, level) if trade.direction == "LONG" else min(trade.stop, level)
                    except (TypeError, ValueError, IndexError):
                        continue
            if trade.direction == "LONG":
                stop_hit = low <= trade.stop
                target_hit = trade.target > 0 and high >= trade.target
            else:
                stop_hit = high >= trade.stop
                target_hit = trade.target > 0 and low <= trade.target
            if not stop_hit and not target_hit:
                close = float(candle.get("close", op))
                pnl = self._pnl_points(trade, close)
                trade.peak_points = max(trade.peak_points, pnl)
                trade.trough_points = min(trade.trough_points, pnl)
                if trade.engine == "s1s2_dual":
                    native_exit = self._as_timestamp(trade.native_exit_time)
                    bar_close = pd.Timestamp(ts) + pd.Timedelta(minutes=self._timeframe_minutes(trade.timeframe))
                    if native_exit is not None and bar_close >= native_exit + pd.Timedelta(minutes=self._timeframe_minutes(trade.timeframe)):
                        return {"price": close, "reason": trade.native_exit_reason or "S12_ENGINE_EXIT",
                                "time": bar_close.isoformat()}
                    from engine.s12_lane import square_off

                    square_time = datetime.strptime(square_off(), "%H:%M").time()
                    if bar_close.time() >= square_time:
                        return {"price": close, "reason": "S12_SQUARE_OFF", "time": bar_close.isoformat()}
                continue
            if stop_hit and target_hit:
                stop_dist = abs(op - trade.stop)
                target_dist = abs(op - trade.target)
                price = trade.stop if stop_dist <= target_dist else trade.target
                reason = "STOP_HIT" if stop_dist <= target_dist else "TARGET_HIT"
            elif stop_hit:
                price = trade.stop
                reason = "STOP_HIT"
            else:
                price = trade.target
                reason = "TARGET_HIT"
            return {
                "price": float(price),
                "reason": reason,
                "time": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            }
        return None

    def _record_replay_close(
        self,
        closed_points: list[float],
        trade: ShadowTrade,
        price: float,
        *,
        lane_points: Optional[Dict[str, list[float]]] = None,
        dual_points: Optional[list[float]] = None,
        dual_active: bool = False,
    ) -> tuple[int, int, float, float]:
        pnl = self._pnl_points(trade, price)
        closed_points.append(pnl)
        lane = self._source_lane(trade, trade.engine)
        if lane_points is not None and lane in lane_points:
            lane_points[lane].append(pnl)
        if dual_active and dual_points is not None:
            dual_points.append(pnl)
        if pnl > 0:
            return 1, 0, pnl, 0.0
        if pnl < 0:
            return 0, 1, 0.0, abs(pnl)
        return 0, 0, 0.0, 0.0

    def _cross_lane_open(self, open_trades: Dict[str, ShadowTrade], trade: ShadowTrade, exclude_key: str = "") -> bool:
        lane = self._source_lane(trade, trade.engine)
        if lane not in {"A", "B"}:
            return False
        other = "B" if lane == "A" else "A"
        for key, item in open_trades.items():
            if key == exclude_key:
                continue
            if item.instrument != trade.instrument or item.timeframe != trade.timeframe:
                continue
            if self._source_lane(item, item.engine) == other:
                return True
        return False

    def _replay_scorecard(
        self,
        *,
        mode: str,
        signals_by_instrument: Dict[str, list[tuple[str, UTBotSignal]]],
        candles_by_instrument: Dict[str, Dict[str, pd.DataFrame]],
        now_iso: str,
    ) -> Dict[str, Any]:
        open_trades: Dict[str, ShadowTrade] = {}
        closed_points: list[float] = []
        lane_closed_points: Dict[str, list[float]] = {"A": [], "B": []}
        lane_open_points: Dict[str, float] = {"A": 0.0, "B": 0.0}
        dual_concurrent_points: list[float] = []
        lane_events: Dict[str, list[str]] = {"A": [], "B": []}
        for instrument, entries in signals_by_instrument.items():
            for tf, sig in self._expanded_replay_entries(mode, entries):
                lane = self._source_lane(sig, mode)
                if lane in lane_events:
                    lane_events[lane].append(self._lane_event_key(instrument, tf, sig))
        wins = 0
        losses = 0
        gross_gain = 0.0
        gross_loss = 0.0
        for instrument, entries in signals_by_instrument.items():
            sorted_entries = sorted(
                self._expanded_replay_entries(mode, entries),
                key=lambda item: self._signal_sort_key(item[1]),
            )
            for tf, sig in sorted_entries:
                trade = self._trade_from_signal(mode, instrument, tf, sig, now_iso)
                if trade is None:
                    continue
                key = self._replay_trade_key(mode, instrument, tf, trade.source_lane)
                existing = open_trades.get(key)
                if existing is not None:
                    frame = (candles_by_instrument.get(instrument) or {}).get(existing.timeframe)
                    path_exit = self._simulate_trade_path(
                        existing,
                        frame,
                        until=getattr(sig, "timestamp", None),
                        include_until=False,
                    )
                    if path_exit is not None:
                        w, l, gg, gl = self._record_replay_close(
                            closed_points,
                            existing,
                            float(path_exit["price"]),
                            lane_points=lane_closed_points,
                            dual_points=dual_concurrent_points,
                            dual_active=self._cross_lane_open(open_trades, existing, key),
                        )
                        wins += w
                        losses += l
                        gross_gain += gg
                        gross_loss += gl
                        existing = None
                        open_trades.pop(key, None)
                if existing is not None:
                    if existing.direction == trade.direction:
                        continue
                    pnl = self._pnl_points(existing, trade.entry_price)
                    closed_points.append(pnl)
                    lane = self._source_lane(existing, mode)
                    if lane in lane_closed_points:
                        lane_closed_points[lane].append(pnl)
                    if self._cross_lane_open(open_trades, existing, key):
                        dual_concurrent_points.append(pnl)
                    if pnl > 0:
                        wins += 1
                        gross_gain += pnl
                    elif pnl < 0:
                        losses += 1
                        gross_loss += abs(pnl)
                open_trades[key] = trade

        open_points = 0.0
        open_rows = []
        for key, trade in list(open_trades.items()):
            frame = (candles_by_instrument.get(trade.instrument) or {}).get(trade.timeframe)
            path_exit = self._simulate_trade_path(trade, frame, include_until=True)
            if path_exit is not None:
                w, l, gg, gl = self._record_replay_close(
                    closed_points,
                    trade,
                    float(path_exit["price"]),
                    lane_points=lane_closed_points,
                    dual_points=dual_concurrent_points,
                    dual_active=self._cross_lane_open(open_trades, trade, key),
                )
                wins += w
                losses += l
                gross_gain += gg
                gross_loss += gl
                open_trades.pop(key, None)
                continue
            latest = self._latest_close(candles_by_instrument, trade.instrument, trade.entry_price)
            pnl = self._pnl_points(trade, latest)
            open_points += pnl
            lane = self._source_lane(trade, mode)
            if lane in lane_open_points:
                lane_open_points[lane] += pnl
            if self._cross_lane_open(open_trades, trade, key):
                dual_concurrent_points.append(pnl)
            open_rows.append({
                "instrument": trade.instrument,
                "timeframe": trade.timeframe,
                "direction": trade.direction,
                "entry": round(trade.entry_price, 2),
                "ltp": round(latest, 2),
                "pnl_points": round(pnl, 2),
                "source_lane": lane,
            })

        closed_net = sum(closed_points)
        total_points = closed_net + open_points
        trades = wins + losses
        lane_a_keys = set(lane_events["A"][-50:])
        lane_b_keys = set(lane_events["B"][-50:])
        union = lane_a_keys | lane_b_keys
        lane_jaccard = (len(lane_a_keys & lane_b_keys) / len(union)) if union else 0.0
        return {
            "open": len(open_trades),
            "open_rows": open_rows,
            "closed": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / max(1, trades)) * 100.0, 1),
            "gross_gain": round(gross_gain, 2),
            "gross_loss": round(gross_loss, 2),
            "closed_points": round(closed_net, 2),
            "open_points": round(open_points, 2),
            "total_points": round(total_points, 2),
            "profit_factor": round(gross_gain / gross_loss, 2) if gross_loss > 0 else (round(gross_gain, 2) if gross_gain > 0 else 0.0),
            "sharpe": round(self._sharpe(closed_points), 2),
            "lane_A_signals": len(lane_events["A"]),
            "lane_B_signals": len(lane_events["B"]),
            "lane_A_pnl": round(sum(lane_closed_points["A"]) + lane_open_points["A"], 2),
            "lane_B_pnl": round(sum(lane_closed_points["B"]) + lane_open_points["B"], 2),
            "dual_concurrent_pnl": round(sum(dual_concurrent_points), 2),
            "lane_jaccard": round(lane_jaccard, 4),
        }

    def _utema_scorecard(self, results, lots_by_instrument, now_iso):
        """Score native shared lifecycles using observed index prices only.

        The comparison lab has no option feed. Its index-point outcomes must not
        imply that an ATM premium stop or the selected option RR was exercised.
        Native Pine exits already contain the fixed T2 rule and stop priority.
        """
        scorecard = self._replay_scorecard(
            mode="ut_ema", signals_by_instrument={}, candles_by_instrument={}, now_iso=now_iso)
        closed_points, open_rows = [], []
        open_points, alignment_lots = 0.0, 0
        for (instrument, timeframe), result in results.items():
            additions = {}
            for signal in result.get("signals", []):
                if getattr(signal, "utema_action", "") == "ADD":
                    event = getattr(signal, "utema_event", {}) or {}
                    ident = (getattr(signal, "utema_state", {}) or {}).get("lifecycle_id")
                    additions.setdefault(ident, event)
            for ident, record in (result.get("utema_records") or {}).items():
                entry = float(record.get("entry_spot") or 0.0)
                side = int(record.get("side") or 0)
                if entry <= 0 or side not in (-1, 1):
                    continue
                base_lots = max(1, int(lots_by_instrument.get(instrument, 1) or 1))
                quantity, cost = base_lots, entry * base_lots
                addition = additions.get(ident)
                if addition is not None:
                    quantity += 1
                    cost += float(addition["price"])
                    alignment_lots += 1
                average = cost / quantity
                if record.get("exit_time"):
                    mark = float(record["exit_price"])
                    closed_points.append(side * (mark - average) * quantity)
                else:
                    mark = float((result.get("state") or {}).get("current_price") or entry)
                    pnl = side * (mark - average) * quantity
                    open_points += pnl
                    open_rows.append({
                        "instrument": instrument, "timeframe": timeframe,
                        "direction": "LONG" if side > 0 else "SHORT",
                        "entry": round(average, 2), "ltp": round(mark, 2),
                        "pnl_points": round(pnl, 2), "source_lane": "UTEMA", "lots": quantity,
                        "native_lifecycle_id": ident,
                    })
        wins = sum(point > 0 for point in closed_points)
        losses = sum(point < 0 for point in closed_points)
        gross_gain = sum(point for point in closed_points if point > 0)
        gross_loss = -sum(point for point in closed_points if point < 0)
        scorecard.update(
            open=len(open_rows), open_rows=open_rows, closed=len(closed_points), wins=wins, losses=losses,
            win_rate=round(100 * wins / max(1, len(closed_points)), 1),
            gross_gain=round(gross_gain, 2), gross_loss=round(gross_loss, 2),
            closed_points=round(sum(closed_points), 2), open_points=round(open_points, 2),
            total_points=round(sum(closed_points) + open_points, 2),
            profit_factor=round(gross_gain / gross_loss, 2) if gross_loss else round(gross_gain, 2),
            sharpe=round(self._sharpe(closed_points), 2), alignment_lots=alignment_lots,
            evaluation_basis="lot-weighted index points; closed-bar native replay",
            option_target_evaluated=False,
        )
        return scorecard

    def process(
        self,
        *,
        candles_by_instrument: Dict[str, Dict[str, pd.DataFrame]],
        active_engine: str = "",
        capital: float = 100000.0,
        risk_pct: float = 1.0,
        lots_by_instrument: Optional[Dict[str, int]] = None,
        lot_size_by_instrument: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        started = time.time()
        now_iso = datetime.now().isoformat()
        lots_by_instrument = lots_by_instrument or {}
        lot_size_by_instrument = lot_size_by_instrument or {}
        rows = []
        for mode in ENGINE_LAB_MODES:
            mode_signals = 0
            mode_buy = 0
            mode_sell = 0
            mode_fresh = 0
            mode_score = 0.0
            mode_score_n = 0
            last_signal = None
            errors = 0
            replay_signals: Dict[str, list[tuple[str, UTBotSignal]]] = {}
            utema_results = {}
            for instrument, tf_map in (candles_by_instrument or {}).items():
                latest_price = 0.0
                for tf in ("1min", "5min", "15min"):
                    df = tf_map.get(tf)
                    if df is not None and not df.empty:
                        try:
                            latest_price = float(df["close"].iloc[-1])
                        except Exception:
                            pass
                    engine = self._engines.get(f"{mode}|{instrument}|{tf}")
                    if engine is None or df is None or len(df) < (2 if mode == "ut_ema" else 15):
                        continue
                    cache_key = f"{mode}|{instrument}|{tf}"
                    try:
                        new_hash = self._hash_df(df, getattr(engine, "signal_mode", "realtime"))
                        extra = {}
                        if mode == "ut_ema":
                            from engine.ut_ema_engine import UTEmaEngine, market_stamp, minutes

                            native_frame = UTEmaEngine.prepare(df)
                            clock = market_stamp()
                            closed_count = int((native_frame.index + pd.Timedelta(minutes=minutes(tf)) <= clock).sum())
                            new_hash = hash((UTEmaEngine.fingerprint(native_frame), closed_count, tf))
                            extra = {"as_of": clock, "synchronous": True}
                        elif mode == "s1s2_dual":
                            from engine.s12_engine import (
                                config_from_settings, evaluation_clock, input_signature,
                                prepare_frame, aux_feeds,
                            )
                            from engine.s12_lane import timeframe_enabled

                            clock = evaluation_clock()
                            if timeframe_enabled(tf):
                                native_frame = prepare_frame(df, tf, as_of=clock)
                                df_5m = tf_map.get("5min") if tf == "15min" else None
                                aux = aux_feeds(tf, df_5m, as_of=clock)
                                new_hash = hash((input_signature(native_frame, aux, config_from_settings()), tf))
                                extra = {"as_of": clock, "synchronous": True}
                                if tf == "15min":
                                    extra["df_5min"] = df_5m
                            else:
                                new_hash = hash(("s12-disabled", tf))
                        elif mode == "s1" or mode in {'quad_prime_s1', 'ut_smart_s1'}:
                            from engine.s1_engine import params_from_settings

                            extra = {f"df_{dep}": tf_map.get(dep) for dep in ("5min", "15min") if dep != tf}
                            # HA and recursive indicators depend on the whole prefix,
                            # including native lower/higher timeframe inputs and settings.
                            frame_hashes = tuple(
                                int(pd.util.hash_pandas_object(frame, index=True).sum()) if frame is not None else None
                                for frame in (df, *extra.values())
                            )
                            new_hash = hash((new_hash, frame_hashes, params_from_settings(instrument=instrument)))
                            if mode == 'ut_smart_s1':
                                from engine.ut_smart_engine import evaluation_clock, closed_bar_mask, params_from_settings as uts_params
                                from engine.uts_lane import intrabar_enabled
                                extra['as_of'] = evaluation_clock()
                                new_hash = hash((new_hash, uts_params(timeframe=tf), intrabar_enabled(),
                                                 bool(closed_bar_mask(df, tf, extra['as_of'])[-1])))
                        elif mode == "ut_smart":
                            from engine.ut_smart_engine import params_from_settings, evaluation_clock, closed_bar_mask
                            from engine.uts_lane import intrabar_enabled

                            intrabar = intrabar_enabled()
                            uts_clock = evaluation_clock()
                            uts_last_closed = bool(closed_bar_mask(df, tf, uts_clock)[-1])
                            extra['as_of'] = uts_clock
                            new_hash = hash((
                                self._hash_df(df, "realtime" if intrabar or uts_last_closed else "confirmed"),
                                params_from_settings(timeframe=tf),
                                intrabar, uts_last_closed,
                            ))
                        cached = self._result_cache.get(cache_key) or {}
                        native_failed = any(lane.get('error') for lane in cached.get('native_results', {}).values())
                        if new_hash == self._data_hash.get(cache_key) and cache_key in self._result_cache and not native_failed:
                            result = self._result_cache[cache_key]
                        else:
                            result = engine.process(
                                df=df,
                                instrument=instrument,
                                timeframe=tf,
                                capital=capital,
                                risk_pct=risk_pct,
                                lots=int(lots_by_instrument.get(instrument, 1) or 1),
                                lot_size=int(lot_size_by_instrument.get(instrument, 1) or 1),
                                **extra,
                            )
                            self._result_cache[cache_key] = result
                            self._data_hash[cache_key] = new_hash
                    except Exception:
                        errors += 1
                        continue
                    errors += sum(bool(lane.get('error')) for lane in result.get('native_results', {}).values())
                    if result.get('error'):
                        continue
                    state = result.get("state") or {}
                    mode_score += float(state.get("position", 0) or 0)
                    mode_score_n += 1
                    sigs = list(result.get("signals") or [])
                    if mode == "ut_ema":
                        utema_results[(instrument, tf)] = result
                        # ADD and EXIT belong to one lifecycle, not new entries.
                        sigs = [sig for sig in sigs if getattr(sig, "utema_action", "") == "ENTRY"]
                    if mode == "adaptive_dual":
                        sigs.extend(
                            self._apex_replay_signals(
                                df,
                                instrument,
                                tf,
                                df_5m=tf_map.get("5min"),
                                df_15m=tf_map.get("15min"),
                            )
                        )
                        sigs.sort(key=self._signal_sort_key)
                    mode_signals += len(sigs)
                    for sig in sigs:
                        if str(getattr(sig, "signal_type", "")).upper() == "BUY":
                            mode_buy += 1
                        elif str(getattr(sig, "signal_type", "")).upper() == "SELL":
                            mode_sell += 1
                    if tf in {"5min", "15min"} and sigs:
                        replay_signals.setdefault(instrument, []).extend((tf, sig) for sig in sigs)
                        latest = [sigs[-1]]
                        if mode in {'quad_prime_s1', 'ut_smart_s1', 's1s2_dual'}:
                            from engine.scanner_modes import owner
                            latest = list({owner(signal): signal for signal in sigs}.values())
                        for sig in latest:
                            last_signal = sig
                            signal_key = self._signal_key(mode, instrument, tf, sig)
                            data_len = len(df)
                            signal_mode = getattr(engine, "signal_mode", "realtime")
                            latest_allowed = data_len - 1 if signal_mode == "realtime" else data_len - 2
                            if getattr(sig, "bar_index", -999) >= data_len - 3 and getattr(sig, "bar_index", -999) <= latest_allowed:
                                if signal_key not in self._seen_signals:
                                    self._seen_signals.add(signal_key)
                                    mode_fresh += 1
                                    if mode != "ut_ema":
                                        self._open_or_reverse(mode, instrument, tf, sig, now_iso)
                if latest_price > 0 and mode != "ut_ema":
                    self._mark_open_trade(mode, instrument, latest_price, now_iso)

            if mode == "ut_ema":
                scorecard = self._utema_scorecard(utema_results, lots_by_instrument, now_iso)
            else:
                scorecard = self._replay_scorecard(
                    mode=mode,
                    signals_by_instrument=replay_signals,
                    candles_by_instrument=candles_by_instrument,
                    now_iso=now_iso,
                )
            rows.append({
                "engine": mode,
                "active": mode == str(active_engine or "").lower(),
                "signals": mode_signals,
                "fresh_signals": mode_fresh,
                "buy": mode_buy,
                "sell": mode_sell,
                "bias": round(mode_score / mode_score_n, 2) if mode_score_n else 0.0,
                "open": scorecard["open"],
                "open_rows": scorecard["open_rows"],
                "closed": scorecard["closed"],
                "wins": scorecard["wins"],
                "losses": scorecard["losses"],
                "win_rate": scorecard["win_rate"],
                "gross_gain": scorecard["gross_gain"],
                "gross_loss": scorecard["gross_loss"],
                "net_points": scorecard["closed_points"],
                "open_points": scorecard["open_points"],
                "total_points": scorecard["total_points"],
                "profit_factor": scorecard["profit_factor"],
                "sharpe": scorecard["sharpe"],
                "lane_A_signals": scorecard["lane_A_signals"],
                "lane_B_signals": scorecard["lane_B_signals"],
                "lane_A_pnl": scorecard["lane_A_pnl"],
                "lane_B_pnl": scorecard["lane_B_pnl"],
                "dual_concurrent_pnl": scorecard["dual_concurrent_pnl"],
                "lane_jaccard": scorecard["lane_jaccard"],
                "last_signal": (
                    {
                        "type": getattr(last_signal, "signal_type", ""),
                        "instrument": getattr(last_signal, "instrument", ""),
                        "timeframe": getattr(last_signal, "timeframe", ""),
                        "price": round(float(getattr(last_signal, "price", 0.0) or 0.0), 2),
                        "source": str(getattr(last_signal, "source_engine", "") or mode),
                        "source_lane": self._source_lane(last_signal, mode),
                    }
                    if last_signal is not None
                    else None
                ),
                "errors": errors,
                **({"evaluation_basis": scorecard["evaluation_basis"],
                    "option_target_evaluated": False,
                    "alignment_lots": scorecard["alignment_lots"]} if mode == "ut_ema" else {}),
            })
        rows.sort(key=lambda row: (float(row.get("total_points", 0.0)), float(row.get("sharpe", 0.0))), reverse=True)
        snapshot = {
            "enabled": True,
            "modes": list(ENGINE_LAB_MODES),
            "active_engine": str(active_engine or "").lower(),
            "rows": rows,
            "summary": {
                "engines": len(ENGINE_LAB_MODES),
                "signals": sum(int(row.get("signals", 0) or 0) for row in rows),
                "fresh_signals": sum(int(row.get("fresh_signals", 0) or 0) for row in rows),
                "open": sum(int(row.get("open", 0) or 0) for row in rows),
                "closed": sum(int(row.get("closed", 0) or 0) for row in rows),
                "latency_ms": round((time.time() - started) * 1000.0),
            },
            "updated_at": now_iso,
        }
        self._last_snapshot = snapshot
        return snapshot

    def snapshot(self) -> Dict[str, Any]:
        return dict(self._last_snapshot or self._empty_snapshot())
