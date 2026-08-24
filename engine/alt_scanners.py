"""Alternative signal engines — drop-in replacements for UTBotEngine.

These keep UTBot's ATR / ADX / trailing-stop machinery (so protective stops,
charting arrays and state stay identical) and only swap the ENTRY/FLIP trigger
for a smoother trend source. Offline replay over 22 trading days (5min, 4 indices,
flip-to-flip, unfiltered) showed UTBot's raw flips were far whippier than these:

    engine      flips   win-rate   net (raw, unfiltered)
    utbot        768      33%        -Rs.241k
    ema          281      39%        +Rs.149k     EMA(9/21) crossover
    consensus    229      45%        +Rs.90k      Supertrend(10,3) AND EMA(9/21) must
                                                  agree; HOLD through disagreement

Direction comes from the alt source; the protective stop is still the UT ATR trail.
This is intentionally conservative: only the flip trigger changes, nothing downstream.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numba import njit
from loguru import logger

from engine.ut_bot_core import UTBotEngine, UTBotSignal


@njit
def _nb_supertrend(close, upper, lower):
    """Numba-compiled Supertrend trend state (+1/-1). O(n), keeps live latency flat
    even on large bar windows (the 1min realtime engine recomputes every tick)."""
    n = len(close)
    fu = upper.copy()
    fl = lower.copy()
    trend = np.ones(n, dtype=np.int64)
    for i in range(1, n):
        # ATR warm-up NaNs: carry the prior trend, don't flip on noise.
        if not np.isfinite(upper[i]) or not np.isfinite(lower[i]):
            trend[i] = trend[i - 1]
            fu[i] = fu[i - 1]
            fl[i] = fl[i - 1]
            continue
        fu[i] = min(upper[i], fu[i - 1]) if close[i - 1] <= fu[i - 1] else upper[i]
        fl[i] = max(lower[i], fl[i - 1]) if close[i - 1] >= fl[i - 1] else lower[i]
        if close[i] > fu[i - 1]:
            trend[i] = 1
        elif close[i] < fl[i - 1]:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]
    return trend


class AltSignalEngine(UTBotEngine):
    """UTBotEngine with the flip trigger replaced by EMA or ST+EMA consensus."""

    def __init__(
        self,
        *,
        signal_source: str = "ema",
        ema_fast: int = 9,
        ema_slow: int = 21,
        st_period: int = 10,
        st_mult: float = 3.0,
        auto_trend_adx: float = 25.0,
        **params,
    ):
        super().__init__(**params)
        self.signal_source = str(signal_source or "ema").lower()
        self.ema_fast = int(ema_fast)
        self.ema_slow = int(ema_slow)
        self.st_period = int(st_period)
        self.st_mult = float(st_mult)
        # auto mode: above this ADX the market is trending -> use consensus (low-noise,
        # high-conviction); below it the market is choppy -> use EMA (more adaptive).
        self.auto_trend_adx = float(auto_trend_adx)

    # ── trend sources (return +1 / -1 per bar) ──────────────────────
    def _ema_trend(self, close: pd.Series) -> np.ndarray:
        ef = close.ewm(span=self.ema_fast, adjust=False).mean().values
        es = close.ewm(span=self.ema_slow, adjust=False).mean().values
        return np.where(ef > es, 1, -1).astype(int)

    def _supertrend_trend(self, df: pd.DataFrame) -> np.ndarray:
        # ATR for the Supertrend bands is a SIMPLE moving average of True Range —
        # this is exactly what the validated 22-day benchmark used (+Rs.90k, 45% WR).
        # (Wilder/RMA ATR is the "textbook" Supertrend but produced a different, less
        #  profitable flip set on this data, so we keep the benchmarked SMA variant.)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        atr = tr.rolling(self.st_period).mean().values.astype(np.float64)
        hl2 = ((high + low) / 2.0).values.astype(np.float64)
        close_arr = close.values.astype(np.float64)
        upper = hl2 + self.st_mult * atr
        lower = hl2 - self.st_mult * atr
        return _nb_supertrend(close_arr, upper, lower).astype(int)

    @staticmethod
    def _hold_through_zeros(arr: np.ndarray) -> np.ndarray:
        """Forward-fill 0s with the last non-zero direction (hold, don't flip/flatten)."""
        out = np.zeros(len(arr), dtype=int)
        last = 0
        for i, v in enumerate(arr):
            if v != 0:
                last = int(v)
            out[i] = last
        return out

    def _quad_union_consensus_positions(self, ema: np.ndarray, st: np.ndarray, utbot_pos: np.ndarray) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        """Quad-Union: Exit only on consensus or losing. At least 2/3 (2 out of 4?) Wait no, let's clarify:
            1. For a per-tf quad-union (EMA, Supertrend, Consensus, UTBot):
            - Entry on any qualified signal
            - Exit only on:
                a) At least 2/4 of sources agree on opposite direction, OR
                b) We are losing
        """
        n = len(ema)
        positions = np.zeros(n, dtype=int)
        consensus_positions = self._consensus_positions(ema, st)
        ctx = {
            "ema": ema,
            "supertrend": st,
            "consensus": consensus_positions,
            "utbot": utbot_pos,
        }
        last = 0
        for i in range(n):
            sources = []
            if 0 <= i < len(ema):
                sources.append(("ema", int(ema[i])))
            if 0 <= i < len(st):
                sources.append(("supertrend", int(st[i])))
            if 0 <= i < len(consensus_positions):
                sources.append(("consensus", int(consensus_positions[i])))
            if 0 <= i < len(utbot_pos):
                sources.append(("utbot", int(utbot_pos[i])))
            
            # Count votes for -1, 0, 1
            votes = {1: 0, -1: 0}
            for name, dir_val in sources:
                if dir_val in votes:
                    votes[dir_val] += 1

            # Determine majority
            want = 0
            if votes[1] > votes[-1]:
                want = 1
            elif votes[-1] > votes[1]:
                want = -1

            # Only flip on at least 2/4 sources for opposite direction
            active = len([v for v in sources if v[1] != 0])
            if last != 0:
                opposite_votes = votes.get(-last, 0)
                if opposite_votes >= max(2, active * 2 // 4):
                    last = want
            else:
                if want != 0:
                    last = want
            positions[i] = last
        return positions, ctx

    def _consensus_positions(self, ema: np.ndarray, st: np.ndarray) -> np.ndarray:
        """ST and EMA must agree; on disagreement HOLD the prior direction."""
        return self._hold_through_zeros(np.where(st == ema, st, 0))

    def _supertrend_line(self, df: pd.DataFrame) -> list:
        """The visible Supertrend line: the active band (lower band in an uptrend,
        upper band in a downtrend). Uses the same SMA-of-TR ATR as the trend logic."""
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr = tr.rolling(self.st_period).mean().values.astype(np.float64)
        hl2 = ((high + low) / 2.0).values.astype(np.float64)
        c = close.values.astype(np.float64)
        upper = hl2 + self.st_mult * atr
        lower = hl2 - self.st_mult * atr
        trend = _nb_supertrend(c, upper, lower)
        line = np.where(trend > 0, lower, upper)
        return [None if not np.isfinite(v) else round(float(v), 2) for v in line]

    def _indicator_overlays(self, df: pd.DataFrame) -> list:
        """Engine-specific chart overlays so the chart visibly reflects the active
        engine: EMA(fast/slow) for ema, Supertrend+EMA(fast/slow) for consensus, all three
        for auto. (utbot has none — its trailing-stop line is its indicator.)"""
        close = df["close"]
        ef = close.ewm(span=self.ema_fast, adjust=False).mean()
        es = close.ewm(span=self.ema_slow, adjust=False).mean()

        def _vals(series) -> list:
            return [None if not np.isfinite(v) else round(float(v), 2) for v in series.values]

        ov = []
        src = self.signal_source
        if src in ("ema", "auto", "utbot_ema", "quad_union", "utbot_prime", "quad_prime", "adaptive_hybrid", "adaptive_dual", "quality_hybrid"):
            ov.append({"name": f"EMA {self.ema_fast}", "color": "#f59e0b", "values": _vals(ef)})
            ov.append({"name": f"EMA {self.ema_slow}", "color": "#3b82f6", "values": _vals(es)})
        if src in ("consensus", "auto", "utbot_consensus", "quad_union", "utbot_prime", "quad_prime", "adaptive_hybrid", "adaptive_dual", "quality_hybrid"):
            ov.append({"name": f"Supertrend {self.st_period},{self.st_mult:g}", "color": "#a855f7", "values": self._supertrend_line(df)})
        if src in ("consensus", "utbot_consensus"):
            ov.append({"name": f"EMA {self.ema_fast}", "color": "#f59e0b", "values": _vals(ef)})
            ov.append({"name": f"EMA {self.ema_slow}", "color": "#3b82f6", "values": _vals(es)})
        return ov

    @property
    def _is_combo(self) -> bool:
        return self.signal_source in ("utbot_ema", "utbot_consensus")

    @property
    def _partner_name(self) -> str:
        """The non-UT1 source that produces this engine's (or, in combo, the partner's)
        flips — used to tag each signal so the dashboard can show which engine fired."""
        if self.signal_source in ("ema", "utbot_ema"):
            return "ema"
        if self.signal_source in ("consensus", "utbot_consensus"):
            return "consensus"
        return self.signal_source or "ema"

    def _alt_positions(self, df: pd.DataFrame) -> np.ndarray:
        """Per-bar position (+1/-1) for the active (or, in combo mode, the partner) source."""
        ema = self._ema_trend(df["close"])
        if self.signal_source in ("ema", "utbot_ema"):
            return ema
        st = self._supertrend_trend(df)
        cons = self._consensus_positions(ema, st)
        if self.signal_source in ("consensus", "utbot_consensus"):
            return cons
        if self.signal_source in ("quad_union", "quad_prime", "adaptive_hybrid", "adaptive_dual", "quality_hybrid"):
            return cons
        if self.signal_source == "auto":
            # Adaptive: consensus when ADX confirms a trend, EMA when choppy. Uses
            # per-bar ADX only (no look-ahead) so it is live-faithful.
            _, _, adx = self.calculate_adx(df, self.adx_period)
            adxv = np.nan_to_num(adx.values, nan=0.0)
            blended = np.where(adxv >= self.auto_trend_adx, cons, ema)
            return self._hold_through_zeros(blended)
        return ema

    @staticmethod
    def _tag_signal(signal: UTBotSignal, source: str, sources: list[str] | None = None) -> UTBotSignal:
        clean_sources = [str(s).lower() for s in (sources or [source]) if str(s or "").strip()]
        if not clean_sources:
            clean_sources = [str(source or "").lower()]
        unique = []
        for item in clean_sources:
            if item and item not in unique:
                unique.append(item)
        signal.source_engine = "multi" if len(unique) > 1 else unique[0]
        setattr(signal, "source_engines", unique)
        return signal

    def _signals_from_positions(
        self,
        positions: np.ndarray,
        df: pd.DataFrame,
        *,
        instrument: str,
        timeframe: str,
        source: str,
        trailing_stop: list,
        atr: list,
        adx: list,
    ) -> list[UTBotSignal]:
        n = len(df)
        signals: list[UTBotSignal] = []
        for i in range(1, n):
            if positions[i] != 0 and positions[i] != positions[i - 1]:
                signals.append(
                    self._signal_from_bar(
                        df,
                        i,
                        int(positions[i]),
                        instrument=instrument,
                        timeframe=timeframe,
                        source=source,
                        trailing_stop=trailing_stop,
                        atr=atr,
                        adx=adx,
                    )
                )
        return signals

    def _signal_from_bar(
        self,
        df: pd.DataFrame,
        i: int,
        direction: int,
        *,
        instrument: str,
        timeframe: str,
        source: str,
        trailing_stop: list,
        atr: list,
        adx: list,
    ) -> UTBotSignal:
        close = df["close"].values
        open_ = df["open"].values if "open" in df.columns else close
        high_ = df["high"].values if "high" in df.columns else close
        low_ = df["low"].values if "low" in df.columns else close
        idx = df.index

        def _num(arr, pos, default):
            try:
                v = float(arr[pos])
                return v if np.isfinite(v) else float(default)
            except Exception:
                return float(default)

        ts_val = _num(trailing_stop, i, close[i])
        ts_obj = idx[i]
        signal = UTBotSignal(
            timestamp=ts_obj.to_pydatetime() if hasattr(ts_obj, "to_pydatetime") else ts_obj,
            signal_type="BUY" if direction > 0 else "SELL",
            price=float(close[i]),
            trailing_stop=ts_val,
            stop_distance=abs(float(close[i]) - ts_val),
            atr_value=_num(atr, i, 0.0),
            adx_value=_num(adx, i, 0.0),
            position_state=int(direction),
            bar_index=int(i),
            instrument=instrument,
            timeframe=timeframe,
            suggested_qty=0,
            raw_candle={
                "open": float(open_[i]),
                "high": float(high_[i]),
                "low": float(low_[i]),
                "close": float(close[i]),
            },
            source_engine=source,
        )
        return self._tag_signal(signal, source)

    @staticmethod
    def _merge_union_signals(signals: list[UTBotSignal]) -> list[UTBotSignal]:
        merged: dict[tuple[int, str], UTBotSignal] = {}
        for signal in signals:
            key = (int(getattr(signal, "bar_index", 0) or 0), str(getattr(signal, "signal_type", "")))
            source_list = list(getattr(signal, "source_engines", None) or [])
            if not source_list:
                src = str(getattr(signal, "source_engine", "") or "").lower()
                source_list = [src] if src else []
            if key in merged:
                existing = merged[key]
                combined = list(getattr(existing, "source_engines", None) or [])
                for src in source_list:
                    if src and src not in combined:
                        combined.append(src)
                AltSignalEngine._tag_signal(existing, combined[0] if combined else "multi", combined)
            else:
                merged[key] = AltSignalEngine._tag_signal(signal, source_list[0] if source_list else "multi", source_list)
        return sorted(merged.values(), key=lambda s: int(getattr(s, "bar_index", 0) or 0))

    @staticmethod
    def _kaufman_efficiency_ratio(close: pd.Series, period: int = 10) -> np.ndarray:
        """Rolling Kaufman ER per bar: straight-line movement / total path."""
        close = close.astype(float)
        change = close.diff(period).abs()
        path = close.diff().abs().rolling(period).sum()
        er = change / path.replace(0, np.nan)
        return np.nan_to_num(er.values.astype(float), nan=0.0, posinf=0.0, neginf=0.0)

    @staticmethod
    def _aroon_trend(df: pd.DataFrame, period: int = 25) -> np.ndarray:
        """Aroon trend-freshness direction (+1/-1, held through neutral bars)."""
        high = df["high"].astype(float).values
        low = df["low"].astype(float).values
        out = np.zeros(len(df), dtype=int)
        period = max(5, int(period or 25))
        for i in range(period - 1, len(df)):
            hi_win = high[i - period + 1 : i + 1]
            lo_win = low[i - period + 1 : i + 1]
            if not (np.isfinite(hi_win).all() and np.isfinite(lo_win).all()):
                continue
            bars_since_high = period - 1 - int(np.argmax(hi_win))
            bars_since_low = period - 1 - int(np.argmin(lo_win))
            aroon_up = ((period - bars_since_high) / period) * 100.0
            aroon_down = ((period - bars_since_low) / period) * 100.0
            if aroon_up >= 70.0 and aroon_down <= 40.0:
                out[i] = 1
            elif aroon_down >= 70.0 and aroon_up <= 40.0:
                out[i] = -1
        return AltSignalEngine._hold_through_zeros(out)

    @staticmethod
    def _donchian_trend(df: pd.DataFrame, period: int = 20) -> np.ndarray:
        close = df["close"].astype(float)
        prior_high = df["high"].astype(float).shift(1).rolling(period, min_periods=period).max()
        prior_low = df["low"].astype(float).shift(1).rolling(period, min_periods=period).min()
        raw = np.where(close > prior_high, 1, np.where(close < prior_low, -1, 0)).astype(int)
        return AltSignalEngine._hold_through_zeros(raw)

    @staticmethod
    def _kama_trend(df: pd.DataFrame, er_period: int = 10, fast: int = 2, slow: int = 30) -> np.ndarray:
        close = df["close"].astype(float).values
        out = np.zeros(len(close), dtype=int)
        if len(close) == 0:
            return out
        fast_sc = 2.0 / (float(fast) + 1.0)
        slow_sc = 2.0 / (float(slow) + 1.0)
        kama = np.full(len(close), np.nan, dtype=float)
        kama[0] = close[0]
        er_period = max(3, int(er_period or 10))
        for i in range(1, len(close)):
            if i < er_period:
                kama[i] = close[i]
                continue
            change = abs(close[i] - close[i - er_period])
            path = float(np.sum(np.abs(np.diff(close[i - er_period : i + 1]))))
            er = 0.0 if path <= 0 else change / path
            smoothing = (er * (fast_sc - slow_sc) + slow_sc) ** 2
            kama[i] = kama[i - 1] + smoothing * (close[i] - kama[i - 1])
            out[i] = 1 if close[i] > kama[i] else -1
        return out

    @staticmethod
    def _session_vwap_trend(df: pd.DataFrame) -> np.ndarray:
        close = df["close"].astype(float)
        typical = (df["high"].astype(float) + df["low"].astype(float) + close) / 3.0
        volume = df["volume"].astype(float).replace(0, np.nan).fillna(1.0) if "volume" in df.columns else pd.Series(1.0, index=df.index)
        dates = df.index.normalize()
        vwap = (typical * volume).groupby(dates).cumsum() / volume.groupby(dates).cumsum().replace(0, np.nan)
        raw = np.where(close > vwap, 1, -1).astype(int)
        if len(raw):
            first_bar = np.r_[True, dates[1:] != dates[:-1]]
            raw[first_bar] = 0
        return raw

    def _adaptive_context_arrays(
        self,
        df: pd.DataFrame,
        ema: np.ndarray,
        supertrend: np.ndarray,
        consensus: np.ndarray,
        adx: list,
    ) -> dict[str, np.ndarray]:
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(max(5, int(self.atr_period or 10))).mean()
        range_atr = (tr / atr.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        span = (high - low).replace(0, np.nan)
        buy_close_loc = ((close - low) / span).replace([np.inf, -np.inf], np.nan).fillna(0.5)
        sell_close_loc = ((high - close) / span).replace([np.inf, -np.inf], np.nan).fillna(0.5)
        if "volume" in df.columns:
            volume = df["volume"].astype(float)
            volume_avg = volume.rolling(20, min_periods=5).mean()
            volume_ratio = (volume / volume_avg.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(1.0)
        else:
            volume_ratio = pd.Series(np.ones(len(df)), index=df.index)
        return {
            "adx": np.nan_to_num(np.asarray(adx, dtype=float), nan=0.0),
            "er": self._kaufman_efficiency_ratio(close, 10),
            "range_atr": range_atr.values.astype(float),
            "volume_ratio": volume_ratio.values.astype(float),
            "buy_close_loc": buy_close_loc.values.astype(float),
            "sell_close_loc": sell_close_loc.values.astype(float),
            "ema": np.asarray(ema, dtype=int),
            "supertrend": np.asarray(supertrend, dtype=int),
            "consensus": np.asarray(consensus, dtype=int),
        }

    def _quality_hybrid_positions(
        self,
        df: pd.DataFrame,
        *,
        ema: np.ndarray,
        supertrend: np.ndarray,
        consensus: np.ndarray,
        adx: list,
    ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        """Engine 12: independent confluence stream, not a UTBot-entry router.

        It uses ATR only for downstream stop/risk arrays inherited from UTBotEngine.
        Direction comes from smoother/trend-freshness sources that performed better
        in the research harness: consensus ST+EMA, EMA, Aroon, Donchian, KAMA and
        session VWAP. Indecisive bars hold prior state; each session starts flat.
        """
        n = len(df)
        aroon = self._aroon_trend(df)
        donchian = self._donchian_trend(df)
        kama = self._kama_trend(df)
        vwap = self._session_vwap_trend(df)
        ctx = self._adaptive_context_arrays(df, ema, supertrend, consensus, adx)
        try:
            from config.settings import get_settings as _gs_qh
            settings = _gs_qh()
            min_score = float(getattr(settings, "quality_hybrid_min_vote_score", 3.0) or 3.0)
            broad_min = int(getattr(settings, "quality_hybrid_broad_agreement", 4) or 4)
            chop_agree = int(getattr(settings, "quality_hybrid_chop_min_agreement", 4) or 4)
            trend_adx = float(getattr(settings, "quality_hybrid_trend_adx", 22.0) or 22.0)
            trend_er = float(getattr(settings, "quality_hybrid_trend_er", 0.50) or 0.50)
            th_vol = float(getattr(settings, "quality_hybrid_thrust_volume_ratio", 1.6) or 1.6)
            th_range = float(getattr(settings, "quality_hybrid_thrust_range_atr", 1.05) or 1.05)
            th_loc = float(getattr(settings, "quality_hybrid_thrust_close_loc", 0.60) or 0.60)
        except Exception:
            min_score, broad_min, chop_agree = 3.0, 4, 4
            trend_adx, trend_er = 22.0, 0.50
            th_vol, th_range, th_loc = 1.6, 1.05, 0.60

        sources = {
            "ema": np.asarray(ema, dtype=int),
            "supertrend": np.asarray(supertrend, dtype=int),
            "consensus": np.asarray(consensus, dtype=int),
            "aroon": np.asarray(aroon, dtype=int),
            "donchian": np.asarray(donchian, dtype=int),
            "kama": np.asarray(kama, dtype=int),
            "vwap": np.asarray(vwap, dtype=int),
        }
        weighted_vote = (
            sources["consensus"] * 2
            + sources["ema"]
            + sources["aroon"]
            + sources["donchian"]
            + sources["kama"]
            + sources["vwap"]
        ).astype(float)
        positions = np.zeros(n, dtype=int)
        route_codes = np.zeros(n, dtype=int)
        agreement_count = np.zeros(n, dtype=int)
        last = 0
        dates = df.index.normalize() if hasattr(df.index, "normalize") else None
        for i in range(n):
            if dates is not None and i > 0 and dates[i] != dates[i - 1]:
                last = 0
            want = 1 if weighted_vote[i] >= min_score else (-1 if weighted_vote[i] <= -min_score else 0)
            if want:
                agreeing = [name for name, arr in sources.items() if int(arr[i]) == want]
                agreement = len(agreeing)
                agreement_count[i] = agreement
                consensus_ok = int(sources["consensus"][i]) == want
                trend_ok = float(ctx["adx"][i]) >= trend_adx or float(ctx["er"][i]) >= trend_er
                close_loc = float(ctx["buy_close_loc"][i] if want > 0 else ctx["sell_close_loc"][i])
                thrust_ok = (
                    float(ctx["volume_ratio"][i]) >= th_vol
                    and float(ctx["range_atr"][i]) >= th_range
                    and close_loc >= th_loc
                    and agreement >= 3
                )
                chop = float(ctx["adx"][i]) < 18.0 and float(ctx["er"][i]) < 0.35
                allowed = (
                    (consensus_ok and agreement >= 3 and (trend_ok or not chop))
                    or agreement >= broad_min
                    or thrust_ok
                    or (chop and consensus_ok and agreement >= chop_agree)
                )
                if allowed:
                    last = want
                    route_codes[i] = 3 if thrust_ok else (2 if not consensus_ok else (4 if chop else 1))
            positions[i] = last

        ctx.update(
            {
                "aroon": sources["aroon"],
                "donchian": sources["donchian"],
                "kama": sources["kama"],
                "vwap": sources["vwap"],
                "quality_vote": weighted_vote,
                "quality_route": route_codes,
                "quality_agreement": agreement_count,
            }
        )
        return positions, ctx

    def _quality_hybrid_signals(
        self,
        df: pd.DataFrame,
        *,
        instrument: str,
        timeframe: str,
        trailing_stop: list,
        atr: list,
        adx: list,
    ) -> tuple[list[UTBotSignal], np.ndarray]:
        ema = self._ema_trend(df["close"])
        supertrend = self._supertrend_trend(df)
        consensus = self._consensus_positions(ema, supertrend)
        positions, ctx = self._quality_hybrid_positions(
            df,
            ema=ema,
            supertrend=supertrend,
            consensus=consensus,
            adx=adx,
        )
        signals = self._signals_from_positions(
            positions,
            df,
            instrument=instrument,
            timeframe=timeframe,
            source="quality_hybrid",
            trailing_stop=trailing_stop,
            atr=atr,
            adx=adx,
        )
        route_names = {
            1: "quality_core",
            2: "quality_broad",
            3: "quality_thrust",
            4: "quality_core",
        }
        component_names = ("ema", "supertrend", "consensus", "aroon", "donchian", "kama", "vwap")
        core_keys = set()

        def _agreement_at(bar: int, want: int) -> list[str]:
            return [
                name
                for name in component_names
                if 0 <= bar < len(ctx.get(name, [])) and int(ctx[name][bar]) == want
            ]

        def _tag_quality_signal(signal: UTBotSignal, route: str, reentry: bool = False) -> UTBotSignal:
            bi = int(getattr(signal, "bar_index", 0) or 0)
            want = 1 if str(getattr(signal, "signal_type", "")).upper() == "BUY" else -1
            agree = _agreement_at(bi, want)
            signal.source_engine = "quality_hybrid"
            setattr(signal, "source_engines", ["quality_hybrid"])
            setattr(signal, "quality_hybrid_route", route)
            setattr(signal, "quality_hybrid_stream", route)
            setattr(signal, "quality_hybrid_reentry", bool(reentry))
            setattr(signal, "quality_hybrid_vote_score", round(float(ctx["quality_vote"][bi]) if 0 <= bi < len(ctx["quality_vote"]) else 0.0, 3))
            setattr(signal, "trend_agreement", len(agree))
            setattr(signal, "trend_agreement_sources", agree)
            return signal

        for signal in signals:
            bi = int(getattr(signal, "bar_index", 0) or 0)
            want = 1 if str(getattr(signal, "signal_type", "")).upper() == "BUY" else -1
            route = route_names.get(
                int(ctx["quality_route"][bi]) if 0 <= bi < len(ctx["quality_route"]) else 0,
                "quality_core",
            )
            _tag_quality_signal(signal, route, reentry=False)
            core_keys.add((bi, want))

        try:
            from config.settings import get_settings as _gs_qh_sig
            settings = _gs_qh_sig()
            min_score = float(getattr(settings, "quality_hybrid_min_vote_score", 3.0) or 3.0)
            broad_min = int(getattr(settings, "quality_hybrid_broad_agreement", 4) or 4)
            trend_adx = float(getattr(settings, "quality_hybrid_trend_adx", 22.0) or 22.0)
            trend_er = float(getattr(settings, "quality_hybrid_trend_er", 0.50) or 0.50)
            th_vol = float(getattr(settings, "quality_hybrid_thrust_volume_ratio", 1.6) or 1.6)
            th_range = float(getattr(settings, "quality_hybrid_thrust_range_atr", 1.05) or 1.05)
            th_loc = float(getattr(settings, "quality_hybrid_thrust_close_loc", 0.60) or 0.60)
        except Exception:
            min_score, broad_min = 3.0, 4
            trend_adx, trend_er = 22.0, 0.50
            th_vol, th_range, th_loc = 1.6, 1.05, 0.60

        close = df["close"].astype(float).values
        open_ = df["open"].astype(float).values if "open" in df.columns else close
        high = df["high"].astype(float).values if "high" in df.columns else close
        low = df["low"].astype(float).values if "low" in df.columns else close
        roll_high = pd.Series(high).rolling(20, min_periods=5).max().shift(1).values
        roll_low = pd.Series(low).rolling(20, min_periods=5).min().shift(1).values
        tf_text = str(timeframe or "").lower()
        cooldown = 2 if "15" in tf_text else 3
        last_route_bar: dict[tuple[str, int], int] = {}
        route_signals: list[UTBotSignal] = []

        def _eligible(route: str, want: int, bar: int) -> bool:
            if (bar, want) in core_keys:
                return False
            key = (route, want)
            last = last_route_bar.get(key, -10_000)
            if bar - last < cooldown:
                return False
            last_route_bar[key] = bar
            return True

        for i in range(2, len(df)):
            want = int(positions[i])
            if want == 0:
                vote = float(ctx["quality_vote"][i]) if i < len(ctx["quality_vote"]) else 0.0
                want = 1 if vote >= min_score else (-1 if vote <= -min_score else 0)
            if want == 0:
                continue
            agreement = len(_agreement_at(i, want))
            vote_abs = abs(float(ctx["quality_vote"][i]) if i < len(ctx["quality_vote"]) else 0.0)
            close_loc = float(ctx["buy_close_loc"][i] if want > 0 else ctx["sell_close_loc"][i])
            trend_ok = (
                float(ctx["adx"][i]) >= trend_adx
                or float(ctx["er"][i]) >= trend_er
            )
            directional_candle = close[i] >= open_[i] if want > 0 else close[i] <= open_[i]
            previous_against = close[i - 1] < open_[i - 1] if want > 0 else close[i - 1] > open_[i - 1]
            fresh_donchian = int(ctx["donchian"][i]) == want and int(ctx["donchian"][i - 1]) != want
            fresh_aroon = int(ctx["aroon"][i]) == want and int(ctx["aroon"][i - 1]) != want
            price_break = (
                (want > 0 and np.isfinite(roll_high[i]) and close[i] > roll_high[i])
                or (want < 0 and np.isfinite(roll_low[i]) and close[i] < roll_low[i])
            )
            thrust_ok = (
                float(ctx["volume_ratio"][i]) >= th_vol
                and float(ctx["range_atr"][i]) >= th_range
                and close_loc >= th_loc
                and agreement >= 3
                and vote_abs >= max(2.0, min_score - 0.5)
            )
            breakout_ok = (
                (fresh_donchian or fresh_aroon or price_break)
                and agreement >= max(3, broad_min)
                and close_loc >= max(0.60, th_loc)
                and vote_abs >= min_score
            )
            pullback_ok = (
                trend_ok
                and agreement >= max(4, broad_min)
                and vote_abs >= min_score
                and previous_against
                and directional_candle
                and close_loc >= max(0.58, th_loc - 0.04)
                and float(ctx["range_atr"][i]) >= 0.55
            )

            # breakout_ok and pullback_ok were computed on every bar and then discarded:
            # `route` was only ever set from thrust_ok, so `if not route: continue` threw
            # both away. Two entire entry routes ran the full evaluation and emitted
            # nothing, on every index and every timeframe, forever — which is why
            # "pullback signal trading" did not exist despite the logic being here.
            #
            # Strongest evidence first, and one route per bar: a thrust that also happens
            # to satisfy the breakout test is a thrust, not two signals. _eligible keys
            # its cooldown on (route, want), so each route now gets its own spacing rather
            # than sharing the thrust route's.
            route = ""
            if thrust_ok:
                route = "quality_thrust"
            elif breakout_ok:
                route = "quality_breakout"
            elif pullback_ok:
                route = "quality_pullback"
            if not route or not _eligible(route, want, i):
                continue
            signal = self._signal_from_bar(
                df,
                i,
                want,
                instrument=instrument,
                timeframe=timeframe,
                source="quality_hybrid",
                trailing_stop=trailing_stop,
                atr=atr,
                adx=adx,
            )
            route_signals.append(_tag_quality_signal(signal, route, reentry=True))
        if route_signals:
            signals = sorted(signals + route_signals, key=lambda item: (int(getattr(item, "bar_index", 0) or 0), str(getattr(item, "quality_hybrid_route", ""))))
        return signals, positions

    @staticmethod
    def _is_opening_window(ts) -> bool:
        try:
            stamp = pd.Timestamp(ts)
            minutes = stamp.hour * 60 + stamp.minute
            return (9 * 60 + 15) <= minutes <= (10 * 60 + 30)
        except Exception:
            return False

    def _adaptive_accept_signal(
        self,
        signal: UTBotSignal,
        ctx: dict[str, np.ndarray],
        df: pd.DataFrame,
    ) -> tuple[bool, str, float, list[str]]:
        """Route Engine 10 signals by regime instead of raw-unioning every arrow."""
        i = int(getattr(signal, "bar_index", -1) or -1)
        n = len(df)
        if i < 0 or i >= n:
            return False, "invalid_bar", 0.0, []
        want = 1 if str(getattr(signal, "signal_type", "")).upper() == "BUY" else -1
        sources = list(getattr(signal, "source_engines", None) or [])
        if not sources:
            src = str(getattr(signal, "source_engine", "") or "").lower()
            sources = [src] if src else []
        sources = [s for s in sources if s]
        agreement_sources = [
            name for name, arr in (
                ("ema", ctx["ema"]),
                ("supertrend", ctx["supertrend"]),
                ("consensus", ctx["consensus"]),
            )
            if int(arr[i]) == want
        ]
        agreement = len(agreement_sources)
        source_count = len(set(sources))
        adx = float(ctx["adx"][i]) if i < len(ctx["adx"]) else 0.0
        er = float(ctx["er"][i]) if i < len(ctx["er"]) else 0.0
        range_atr = float(ctx["range_atr"][i]) if i < len(ctx["range_atr"]) else 0.0
        volume_ratio = float(ctx["volume_ratio"][i]) if i < len(ctx["volume_ratio"]) else 1.0
        close_loc = float(ctx["buy_close_loc"][i] if want > 0 else ctx["sell_close_loc"][i])
        consensus_aligned = "consensus" in agreement_sources
        opening = self._is_opening_window(df.index[i])

        # ── Engine 10 v2: regime-switched DELEGATION to the proven engines ──
        # v1 re-invented signal selection from raw arrows and lost 10x to the
        # engines it routed (three 30d backtests: Rs.17-21k vs Rs.190k). v2 does
        # what the concept said: trend/opening drive -> utbot_consensus behavior
        # (union of UT1 + consensus flips); chop/range -> utbot_prime behavior
        # (UT1 flips with cross-source agreement) or strict consensus; volume
        # thrust -> fast entry. Signals carry NO raw-union handicaps downstream.
        try:
            from config.settings import get_settings as _gs_v2
            _s = _gs_v2()
            trend_adx = float(getattr(_s, "adaptive_hybrid_trend_adx", 25.0) or 25.0)
            trend_er = float(getattr(_s, "adaptive_hybrid_trend_er", 0.68) or 0.68)
            opening_er = float(getattr(_s, "adaptive_hybrid_opening_er", 0.58) or 0.58)
            chop_min_agree = int(getattr(_s, "adaptive_hybrid_chop_min_agreement", 2) or 2)
            th_vol = float(getattr(_s, "adaptive_hybrid_thrust_volume_ratio", 1.8) or 1.8)
            th_range = float(getattr(_s, "adaptive_hybrid_thrust_range_atr", 1.15) or 1.15)
            th_loc = float(getattr(_s, "adaptive_hybrid_thrust_close_loc", 0.62) or 0.62)
        except Exception:
            trend_adx, trend_er, opening_er, chop_min_agree = 25.0, 0.68, 0.58, 2
            th_vol, th_range, th_loc = 1.8, 1.15, 0.62

        trend_state = adx >= trend_adx or er >= trend_er or (opening and er >= opening_er)
        power_thrust = (
            volume_ratio >= th_vol
            and range_atr >= th_range
            and close_loc >= th_loc
            and agreement >= 1
        )

        score = 0.0
        score += min(0.30, max(0.0, (adx - 15.0) / 50.0))
        score += min(0.25, er * 0.25)
        score += min(0.20, max(0.0, volume_ratio - 1.0) * 0.08)
        score += min(0.15, max(0.0, close_loc - 0.50) * 0.50)
        score += agreement * 0.10

        if power_thrust:
            return True, "power_thrust", score, agreement_sources

        if trend_state:
            # utbot_consensus behavior: BOTH engines' flips flow through — the
            # proven Rs.190k baseline. No extra selection layer on top.
            return True, ("opening_drive" if opening else "trend_utbot_consensus"), score, agreement_sources

        # Chop/range: conservative. utbot_prime behavior — UT1 flips need
        # cross-source agreement; strict consensus flips stand on their own.
        if "utbot" in sources and agreement >= chop_min_agree:
            return True, "chop_utbot_prime", score, agreement_sources
        if "consensus" in sources and consensus_aligned:
            return True, "chop_strict_consensus", score, agreement_sources
        return False, "chop_conservative_block", score, agreement_sources

    def _adaptive_hybrid_signals(
        self,
        df: pd.DataFrame,
        *,
        instrument: str,
        timeframe: str,
        trailing_stop: list,
        atr: list,
        adx: list,
        utbot_signals: list[UTBotSignal],
        source_label: str = "adaptive_hybrid",
    ) -> tuple[list[UTBotSignal], np.ndarray]:
        n = len(df)
        ema = self._ema_trend(df["close"])
        supertrend = self._supertrend_trend(df)
        consensus = self._consensus_positions(ema, supertrend)
        # v2 delegation: entries come ONLY from the two proven engines' flip
        # streams — UT1 and consensus (the utbot_consensus pair). EMA and
        # Supertrend serve as agreement CONTEXT, not as entry sources (v1's raw
        # ema/st arrows were the churn the router then had to fight).
        signals: list[UTBotSignal] = []
        signals.extend(
            self._signals_from_positions(
                consensus,
                df,
                instrument=instrument,
                timeframe=timeframe,
                source="consensus",
                trailing_stop=trailing_stop,
                atr=atr,
                adx=adx,
            )
        )
        for signal in utbot_signals:
            signals.append(self._tag_signal(signal, "utbot"))

        merged = self._merge_union_signals(signals)
        ctx = self._adaptive_context_arrays(df, ema, supertrend, consensus, adx)
        accepted: list[UTBotSignal] = []
        positions = np.zeros(n, dtype=int)
        for signal in merged:
            ok, route, score, agreement_sources = self._adaptive_accept_signal(signal, ctx, df)
            if not ok:
                continue
            original_sources = list(getattr(signal, "source_engines", None) or [])
            label = str(source_label or "adaptive_hybrid").lower()
            self._tag_signal(signal, label, original_sources or [str(getattr(signal, "source_engine", "") or label)])
            signal.source_engine = label
            setattr(signal, "source_lane", "A" if label == "adaptive_dual" else "")
            setattr(signal, "adaptive_hybrid_route", route)
            setattr(signal, "adaptive_hybrid_score", round(float(score), 4))
            setattr(signal, "adaptive_hybrid_sources", original_sources)
            setattr(signal, "trend_agreement", len(agreement_sources))
            setattr(signal, "trend_agreement_sources", agreement_sources)
            accepted.append(signal)

        last = 0
        by_bar: dict[int, UTBotSignal] = {
            int(getattr(sig, "bar_index", -1) or -1): sig for sig in accepted
        }
        for i in range(n):
            sig = by_bar.get(i)
            if sig is not None:
                last = 1 if str(getattr(sig, "signal_type", "")).upper() == "BUY" else -1
            positions[i] = last
        return accepted, positions

    def process(self, df: pd.DataFrame, instrument: str = "", timeframe: str = "", **kw):
        result = super().process(df=df, instrument=instrument, timeframe=timeframe, **kw)
        if not isinstance(result, dict) or result.get("error"):
            return result
        # In combo mode, keep UTBot's own flips (computed by super) so we can UNION them
        # with the partner engine's flips below — both engines' entries flow downstream.
        utbot_signals = list(result.get("signals") or []) if (
            self._is_combo
            or self.signal_source in ("quad_union", "utbot_prime", "quad_prime", "adaptive_hybrid", "adaptive_dual")
        ) else None
        try:
            n = len(df)
            ts_arr = result.get("trailing_stop") or []
            atr_arr = result.get("atr") or []
            adx_arr = result.get("adx") or []

            if self.signal_source in ("quad_union", "quad_prime"):
                ema = self._ema_trend(df["close"])
                supertrend = self._supertrend_trend(df)
                utbot_pos_arr = np.asarray(result.get("positions", []), dtype=int)
                if len(utbot_pos_arr) != len(df):
                    utbot_pos_arr = self._hold_through_zeros(np.zeros(len(df), dtype=int))
                positions, quad_ctx = self._quad_union_consensus_positions(ema, supertrend, utbot_pos_arr)
                signals = []
                for source, source_positions in (
                    ("ema", ema),
                    ("supertrend", supertrend),
                    ("consensus", self._consensus_positions(ema, supertrend)),
                ):
                    signals.extend(
                        self._signals_from_positions(
                            source_positions,
                            df,
                            instrument=instrument,
                            timeframe=timeframe,
                            source=source,
                            trailing_stop=ts_arr,
                            atr=atr_arr,
                            adx=adx_arr,
                        )
                    )
                for signal in utbot_signals or []:
                    signals.append(self._tag_signal(signal, "utbot"))
                signals = self._merge_union_signals(signals)
            elif self.signal_source == "utbot_prime":
                # UT1 remains the sole entry trigger. EMA, Supertrend, and their
                # consensus only annotate how much trend structure agrees at that
                # UT1 bar; they never add slower standalone entries.
                ema = self._ema_trend(df["close"])
                supertrend = self._supertrend_trend(df)
                consensus = self._consensus_positions(ema, supertrend)
                positions = np.asarray(result.get("positions") or [], dtype=int)
                if positions.size != n:
                    positions = self._hold_through_zeros(np.zeros(n, dtype=int))
                signals = []
                for signal in utbot_signals or []:
                    bi = int(getattr(signal, "bar_index", 0) or 0)
                    want = 1 if str(getattr(signal, "signal_type", "")).upper() == "BUY" else -1
                    agree: list[str] = []
                    if 0 <= bi < n:
                        if int(ema[bi]) == want:
                            agree.append("ema")
                        if int(supertrend[bi]) == want:
                            agree.append("supertrend")
                        if int(consensus[bi]) == want:
                            agree.append("consensus")
                    self._tag_signal(signal, "utbot")
                    setattr(signal, "trend_agreement", len(agree))
                    setattr(signal, "trend_agreement_sources", agree)
                    setattr(signal, "source_engines", ["utbot"] + agree)
                    signals.append(signal)
            elif self.signal_source in ("adaptive_hybrid", "adaptive_dual"):
                signals, positions = self._adaptive_hybrid_signals(
                    df,
                    instrument=instrument,
                    timeframe=timeframe,
                    trailing_stop=ts_arr,
                    atr=atr_arr,
                    adx=adx_arr,
                    utbot_signals=utbot_signals or [],
                    source_label=self.signal_source,
                )
            elif self.signal_source == "quality_hybrid":
                signals, positions = self._quality_hybrid_signals(
                    df,
                    instrument=instrument,
                    timeframe=timeframe,
                    trailing_stop=ts_arr,
                    atr=atr_arr,
                    adx=adx_arr,
                )
            else:
                positions = self._alt_positions(df)
                signals = self._signals_from_positions(
                    positions,
                    df,
                    instrument=instrument,
                    timeframe=timeframe,
                    source=self._partner_name,
                    trailing_stop=ts_arr,
                    atr=atr_arr,
                    adx=adx_arr,
                )

            if self._is_combo and utbot_signals:
                # Union UT1 + partner flips, de-duped by (bar_index, direction). When both
                # engines flip the same way on the same bar they AGREE -> tag "both" (the
                # strongest, complementary signal). Each kept signal carries its source_engine
                # so the dashboard can render UT1 vs partner vs both distinctly.
                merged = {(int(s.bar_index), s.signal_type): s for s in signals}
                for s in utbot_signals:
                    key = (int(s.bar_index), s.signal_type)
                    if key in merged:
                        merged[key].source_engine = "both"
                        existing_sources = list(getattr(merged[key], "source_engines", None) or [self._partner_name])
                        if "utbot" not in existing_sources:
                            existing_sources.append("utbot")
                        setattr(merged[key], "source_engines", existing_sources)
                    else:
                        self._tag_signal(s, "utbot")
                        merged[key] = s
                signals = sorted(merged.values(), key=lambda s: int(s.bar_index))

            result["signals"] = signals
            result["positions"] = positions.tolist()
            try:
                result["indicator_overlays"] = self._indicator_overlays(df)
            except Exception:
                result["indicator_overlays"] = []

            last_idx = n - 1
            state_idx = last_idx - 1 if self.signal_mode == "confirmed" and n > 1 else last_idx
            pos = int(positions[state_idx])
            if isinstance(result.get("state"), dict):
                result["state"]["position"] = pos
                result["state"]["position_label"] = (
                    "LONG" if pos == 1 else ("SHORT" if pos == -1 else "FLAT")
                )
            # Keep the engine's internal state consistent for the UT1 STATE panel.
            try:
                st_obj = self.get_state(f"{instrument}_{timeframe}")
                st_obj.position = pos
                if signals:
                    st_obj.last_signal = signals[-1]
            except Exception:
                pass
            return result
        except Exception as exc:
            # Any failure → fall back to the already-computed UT result (fail safe).
            logger.warning(
                "Alt signal engine failed; falling back to UT1 result "
                "engine={} instrument={} timeframe={} error={}",
                self.signal_source,
                instrument or "-",
                timeframe or "-",
                exc,
            )
            return result


def build_scanner_engine(tf_params: dict):
    """Factory: return the signal engine for the active `scanner_engine` setting.

    The production default is currently 'utbot_consensus'. Set scanner_engine='utbot'
    to run the unchanged baseline UTBotEngine.
    """
    auto_adx = 25.0
    try:
        from config.settings import get_settings

        _s = get_settings()
        mode = str(getattr(_s, "scanner_engine", "utbot") or "utbot").lower()
        auto_adx = float(getattr(_s, "scanner_auto_trend_adx", 25.0) or 25.0)
    except Exception:
        mode = "utbot"
    if mode in {"consensus", "ema", "utbot_consensus", "quad_union", "quad_prime", "adaptive_dual"}:
        return AltSignalEngine(signal_source=mode, auto_trend_adx=auto_adx, **tf_params)
    return UTBotEngine(**tf_params)
