"""
Volume Profiler — Intraday Volume Analysis
═══════════════════════════════════════════════════════════════

Tracks:
- Cumulative intraday volume vs previous day
- Volume surge detection (>1.5x average)
- VWAP calculation and deviation
- Buy/sell volume ratio estimation
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional
from loguru import logger
from intelligence.price_action import (
    aroon_context,
    market_structure_profile,
    wavetrend_context,
)


class VolumeProfiler:
    """Intraday volume analysis engine"""

    def __init__(self):
        self._prev_day_volumes: Dict[str, pd.Series] = {}
        self._today_volumes: Dict[str, float] = {}

    def analyze(self, df: pd.DataFrame, instrument: str = "") -> Dict:
        """
        Analyze volume patterns in OHLCV data.

        Returns:
            Dict with volume metrics and signals
        """
        if df is None or len(df) < 10:
            return self._empty_result()

        try:
            raw_volume = pd.to_numeric(
                df.get('volume', pd.Series(0.0, index=df.index)),
                errors='coerce',
            ).fillna(0)
            close = df['close']
            high = df['high']
            low = df['low']
            if isinstance(df.index, pd.DatetimeIndex):
                active_date = df.index[-1].date()
                active_mask = pd.Series(
                    df.index.date == active_date, index=df.index
                )
            else:
                active_mask = pd.Series(True, index=df.index)
            real_nonzero = raw_volume.where(raw_volume > 0)
            real_nonzero_count = int(real_nonzero.count())
            active_real_nonzero_count = int(
                raw_volume.loc[active_mask][raw_volume.loc[active_mask] > 0].count()
            )

            # Cash-index ticks do not carry traded quantity: zero is a valid
            # upstream absence marker, not a measured volume observation.  When
            # the live 1m candle builder has enough samples, expose relative tick
            # activity as an explicitly labelled proxy.  This gives the operator
            # useful participation telemetry without inventing exchange volume.
            tick_activity = pd.to_numeric(
                df.get('tick_count', pd.Series(0.0, index=df.index)),
                errors='coerce',
            ).fillna(0)
            tick_nonzero = tick_activity.where(tick_activity > 0)
            tick_nonzero_count = int(tick_nonzero.count())
            active_tick_nonzero_count = int(
                tick_activity.loc[active_mask][tick_activity.loc[active_mask] > 0].count()
            )
            is_proxy = (
                active_real_nonzero_count < 5
                and active_tick_nonzero_count >= 5
            )
            if active_real_nonzero_count >= 5:
                volume = raw_volume
                nonzero_volume = real_nonzero
                nonzero_count = active_real_nonzero_count
                metric_source = "EXCHANGE_VOLUME"
            elif is_proxy:
                volume = tick_activity
                nonzero_volume = tick_nonzero
                nonzero_count = active_tick_nonzero_count
                metric_source = "INDEX_TICK_ACTIVITY"
            else:
                volume = raw_volume
                nonzero_volume = real_nonzero
                nonzero_count = active_real_nonzero_count
                metric_source = "UNAVAILABLE"
            current_vol = float(volume.iloc[-1])
            # Historical sessions may provide a comparison baseline, but they
            # cannot qualify today's source.  The latest bar must also contain a
            # positive observation; otherwise 0.0x is a data gap, not a neutral
            # market measurement.
            #
            # "Latest" means the last bar that has actually had a chance to trade.
            # Index volume is a CUMULATIVE total and the candle stores the delta, so a
            # bar that is still forming legitimately reads 0 for the first ticks of every
            # minute. Testing only volume.iloc[-1] therefore reported the whole feed
            # unavailable for part of EVERY minute — which is what showed zero on all four
            # indices in the intel panel while the candles behind it held 700k+ of real
            # volume, and what starved both the confidence score and the exit volume test.
            #
            # The dead-feed case this guard exists for is still caught: nonzero_count >= 5
            # requires five measured bars in the active session, and a genuinely dead feed
            # has none. Looking one bar back only forgives the forming bar.
            recent_vol = current_vol
            if recent_vol <= 0 and len(volume) >= 2:
                try:
                    recent_vol = float(volume.iloc[-2])
                except (IndexError, TypeError, ValueError):
                    recent_vol = current_vol
            volume_available = bool(nonzero_count >= 5 and recent_vol > 0)
            volume_for_stats = volume.loc[active_mask]

            # ── Intraday Daily VWAP (Resets every day at 09:15) ──
            df_copy = df.copy()
            df_copy['volume'] = volume
            df_copy['date'] = df_copy.index.date
            # Group by date to calculate daily cumulative values
            grouped = df_copy.groupby('date')
            
            typical_price = (high + low + close) / 3
            cum_vol = grouped['volume'].cumsum()
            cum_pv = (typical_price * df_copy['volume']).groupby(df_copy.index.date).cumsum()
            vwap = cum_pv / cum_vol
            vwap_current = float(vwap.iloc[-1]) if not pd.isna(vwap.iloc[-1]) else 0
            vwap_deviation = ((close.iloc[-1] - vwap_current) / vwap_current * 100) if vwap_current > 0 else 0

            # ── Volume Moving Average ──
            current_idx = volume.index[-1]

            vol_ma = volume_for_stats.rolling(20, min_periods=5).mean()
            avg_at_current = vol_ma.loc[current_idx] if current_idx in vol_ma.index else vol_ma.iloc[-1]
            if pd.isna(avg_at_current) or avg_at_current <= 0:
                fallback_avg = volume_for_stats.tail(20).mean()
                avg_vol = float(fallback_avg) if not pd.isna(fallback_avg) and fallback_avg > 0 else max(current_vol, 1.0)
            else:
                avg_vol = float(avg_at_current)

            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

            tod_avg = 0.0
            relative_volume_ratio = 1.0
            volume_confidence = 50
            if isinstance(df.index, pd.DatetimeIndex) and len(df) >= 30:
                last_ts = current_idx
                same_slot = df[(df.index.time == last_ts.time()) & (df.index.date != last_ts.date())]
                if len(same_slot) >= 2:
                    same_slot_column = 'tick_count' if is_proxy else 'volume'
                    same_slot_volume = pd.to_numeric(
                        same_slot.get(same_slot_column, pd.Series(0.0, index=same_slot.index)),
                        errors='coerce',
                    ).fillna(0)
                    same_slot_nonzero = same_slot_volume[same_slot_volume > 0]
                    tod_source = same_slot_nonzero if len(same_slot_nonzero) >= 2 else same_slot_volume
                    tod_avg = float(tod_source.tail(10).mean())
                    if tod_avg > 0:
                        relative_volume_ratio = current_vol / tod_avg
                        volume_confidence = 65 if is_proxy else 75

            # ── Volume Surge Detection ──
            tod_confident = volume_confidence >= (65 if is_proxy else 75) and tod_avg > 0
            effective_ratio = max(vol_ratio, relative_volume_ratio if tod_confident else 1.0)
            display_ratio = relative_volume_ratio if tod_confident else vol_ratio
            ratio_basis = "TIME_OF_DAY" if tod_confident else "ROLLING_AVG"
            if is_proxy:
                ratio_basis = f"TICK_{ratio_basis}"
            is_surge = bool(volume_available and effective_ratio > 1.5)
            surge_level = "EXTREME" if effective_ratio > 3.0 else ("HIGH" if effective_ratio > 2.0 else ("MODERATE" if effective_ratio > 1.5 else "NORMAL"))
            if not volume_available:
                surge_level = "NORMAL"
                effective_ratio = 1.0
                display_ratio = 1.0
                vol_ratio = 1.0
                relative_volume_ratio = 1.0
                ratio_basis = "INSUFFICIENT_CURRENT_SESSION"
                volume_confidence = 0

            # ── Cumulative Volume Today ──
            total_volume = float(volume.loc[active_mask].sum())

            # ── Volume Trend (last 10 bars) ──
            trend_volume = volume_for_stats.dropna()
            if len(trend_volume) >= 10:
                recent_avg = float(trend_volume.iloc[-5:].mean())
                prior_avg = float(trend_volume.iloc[-10:-5].mean())
                vol_trend = "INCREASING" if recent_avg > prior_avg * 1.2 else (
                    "DECREASING" if recent_avg < prior_avg * 0.8 else "STABLE"
                )
            else:
                vol_trend = "INSUFFICIENT"

            # ── Buy/Sell Volume Estimation ──
            # Approximate using close position within high-low range
            close_position = (close - low) / (high - low)
            close_position = close_position.fillna(0.5)
            buy_volume = (volume.loc[active_mask] * close_position.loc[active_mask]).sum()
            sell_volume = (
                volume.loc[active_mask] * (1 - close_position.loc[active_mask])
            ).sum()
            buy_sell_ratio = (
                float(buy_volume / sell_volume)
                if volume_available and sell_volume > 0
                else 1.0
            )

            # ── Signal Generation ──
            signal = "NEUTRAL"
            if is_surge:
                if close.iloc[-1] > close.iloc[-2]:
                    signal = "BULLISH_VOLUME"
                else:
                    signal = "BEARISH_VOLUME"

            score = 50
            if signal == "BULLISH_VOLUME":
                score += 20
            elif signal == "BEARISH_VOLUME":
                score -= 20
            if buy_sell_ratio > 1.5:
                score += 15
            elif buy_sell_ratio < 0.6:
                score -= 15
            if effective_ratio > 2.0:
                score += 5 if signal == "BULLISH_VOLUME" else -5

            # ── Market Structure Profile v2 + Aroon/WaveTrend telemetry ──
            # These are context layers. They can slightly refine the volume score,
            # but they do not create a standalone trade signal.
            profile = market_structure_profile(df_copy, lookback=200, bins=40)
            aroon = aroon_context(high.to_numpy(dtype=float), low.to_numpy(dtype=float))
            wavetrend = wavetrend_context(
                high.to_numpy(dtype=float),
                low.to_numpy(dtype=float),
                close.to_numpy(dtype=float),
            )
            profile_signal = str((profile or {}).get("signal") or "")
            if profile_signal in {"ACCEPTING_HIGHER", "UPSIDE_ACCEPTANCE"}:
                score += 5
            elif profile_signal in {"ACCEPTING_LOWER", "DOWNSIDE_ACCEPTANCE"}:
                score -= 5
            elif profile_signal == "REJECTING_HIGHER":
                score -= 3
            elif profile_signal == "REJECTING_LOWER":
                score += 3

            aroon_trend = str((aroon or {}).get("trend") or "")
            if aroon_trend == "UP" and not bool((aroon or {}).get("late_trend_caution")):
                score += 3
            elif aroon_trend == "DOWN" and not bool((aroon or {}).get("late_trend_caution")):
                score -= 3
            score = int(max(0, min(100, score)))

            return {
                "vwap": round(vwap_current, 2),
                "vwap_deviation_pct": round(float(vwap_deviation), 2),
                "current_volume": int(current_vol),
                "avg_volume": int(avg_vol),
                "volume_ratio": round(vol_ratio, 2),
                "relative_volume_ratio": round(relative_volume_ratio, 2),
                "display_volume_ratio": round(display_ratio, 2),
                "effective_volume_ratio": round(effective_ratio, 2),
                "volume_ratio_basis": ratio_basis,
                "time_of_day_avg_volume": int(tod_avg),
                "volume_confidence": volume_confidence,
                "available": volume_available,
                "status": (
                    ("PROXY" if is_proxy else "OK")
                    if volume_available
                    else "UNAVAILABLE"
                ),
                "reason": (
                    "Index traded volume unavailable; using relative 1m tick activity"
                    if is_proxy and volume_available
                    else (
                        ""
                        if volume_available
                        else "Current-session OHLCV/tick activity is missing, zero, or insufficient"
                    )
                ),
                "source": metric_source if volume_available else "UNAVAILABLE",
                "metric_kind": "TICK_ACTIVITY" if is_proxy else "TRADED_VOLUME",
                "is_proxy": bool(is_proxy and volume_available),
                "sample_count": int(len(df)),
                "nonzero_bar_count": nonzero_count,
                "real_volume_nonzero_bar_count": real_nonzero_count,
                "tick_activity_nonzero_bar_count": tick_nonzero_count,
                "active_session_real_volume_nonzero_bar_count": active_real_nonzero_count,
                "active_session_tick_activity_nonzero_bar_count": active_tick_nonzero_count,
                "is_surge": is_surge,
                "surge_level": surge_level,
                "total_volume": int(total_volume),
                "volume_trend": vol_trend,
                "buy_sell_ratio": round(buy_sell_ratio, 2),
                "buy_pct": round(buy_sell_ratio / (1 + buy_sell_ratio) * 100, 1),
                "market_structure_profile": profile or {},
                "aroon": aroon or {"available": False},
                "wavetrend": wavetrend or {"available": False},
                "signal": signal,
                "score": score,
            }

        except Exception as e:
            logger.error(f"Volume analysis error: {e}")
            return self._empty_result()

    def _empty_result(self) -> Dict:
        return {
            "vwap": 0, "vwap_deviation_pct": 0, "current_volume": 0,
            "available": False, "status": "UNAVAILABLE", "reason": "Insufficient OHLCV bars",
            "source": "UNAVAILABLE", "metric_kind": "TRADED_VOLUME", "is_proxy": False,
            "sample_count": 0, "nonzero_bar_count": 0,
            "real_volume_nonzero_bar_count": 0, "tick_activity_nonzero_bar_count": 0,
            "avg_volume": 0, "volume_ratio": 1.0, "relative_volume_ratio": 1.0,
            "display_volume_ratio": 1.0, "effective_volume_ratio": 1.0,
            "volume_ratio_basis": "INSUFFICIENT",
            "time_of_day_avg_volume": 0, "volume_confidence": 0, "is_surge": False,
            "surge_level": "NORMAL", "total_volume": 0, "volume_trend": "INSUFFICIENT",
            "buy_sell_ratio": 1.0, "buy_pct": 50.0,
            "market_structure_profile": {}, "aroon": {"available": False},
            "wavetrend": {"available": False},
            "signal": "NEUTRAL", "score": 50,
        }
