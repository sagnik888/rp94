"""
Regime Detector — Multi-TF Market Regime Detection
═══════════════════════════════════════════════════════════════
States: TRENDING_UP, TRENDING_DOWN, RANGING, VOLATILE, BREAKOUT
"""
import pandas as pd
import numpy as np
from typing import Any, Dict, Optional
from loguru import logger
from engine.market_regime import MarketRegime, normalize_regime, regime_aligns


class RegimeDetector:
    # Shared across instances ON PURPOSE. Scanner._detect_regime_warmed builds a
    # deliberate throwaway detector on every call (it needs empty smoothing history to
    # stay replay-deterministic), so a per-instance dedup set is empty every time and
    # the "log once" below logged on every scan instead: 1,050 lines in one session,
    # a single BANKNIFTY_60min promotion repeating 314 times. Whether a given promotion
    # has already been reported for a given bar is a question about the session, not
    # about one short-lived detector object.
    _ER_PROMOTION_LOG_KEYS: set = set()
    _ER_PROMOTION_LOG_CAP = 2048

    def __init__(self):
        self._regime_history: Dict[str, list] = {}

    def reset_history(self, instrument=None) -> int:
        """Clear stateful smoothing/hysteresis history.

        Historical replays must start from the same empty regime memory every
        recalculation; otherwise the same candles can grade differently after a
        prior run has already warmed this detector.
        """
        if instrument is None:
            count = len(self._regime_history)
            self._regime_history.clear()
            RegimeDetector._ER_PROMOTION_LOG_KEYS.clear()
            return count
        prefix = f"{str(instrument).upper()}_"
        keys = [
            key for key in list(self._regime_history)
            if str(key).upper().startswith(prefix)
        ]
        for key in keys:
            self._regime_history.pop(key, None)
        RegimeDetector._ER_PROMOTION_LOG_KEYS = {
            key for key in RegimeDetector._ER_PROMOTION_LOG_KEYS
            if not str(key[0]).upper().startswith(prefix)
        }
        return len(keys)

    def detect(
        self,
        df: pd.DataFrame,
        instrument: str = "",
        timeframe: str = "",
        *,
        session_context: Optional[Dict[str, Any]] = None,
        breadth_context: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        candle_count = 0 if df is None else len(df)
        if candle_count < 30:
            return self._unknown(
                candle_count=candle_count,
                reason=f"Need at least 30 candles; have {candle_count}",
            )
        try:
            if df is not None and not {'close', 'high', 'low'}.issubset(df.columns):
                return self._unknown(
                    candle_count=candle_count,
                    reason="Missing OHLC columns",
                )
            close = df['close']
            high = df['high']
            low = df['low']
            # ADX
            from engine.ut_bot_core import UTBotEngine
            _, _, adx = UTBotEngine.calculate_adx(df, 14)
            adx_val = float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else 0
            # ATR %
            atr = UTBotEngine.calculate_atr(df, 14)
            atr_pct = float(atr.iloc[-1] / close.iloc[-1] * 100) if close.iloc[-1] > 0 else 0
            # Bollinger Band Width
            sma60 = close.rolling(60).mean()
            std60 = close.rolling(60).std()
            bb_width = float(((sma60.iloc[-1] + 2 * std60.iloc[-1]) - (sma60.iloc[-1] - 2 * std60.iloc[-1])) / sma60.iloc[-1] * 100) if not pd.isna(sma60.iloc[-1]) and sma60.iloc[-1] > 0 else 0
            # Trend direction (SMA alignment)
            sma9 = close.rolling(9).mean().iloc[-1] if len(close) >= 9 else close.iloc[-1]
            sma21 = close.rolling(21).mean().iloc[-1] if len(close) >= 21 else close.iloc[-1]
            price = close.iloc[-1]
            direction = "UP" if price > sma9 > sma21 else ("DOWN" if price < sma9 < sma21 else "NEUTRAL")
            # RSI
            delta = close.diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi = float(100 - (100 / (1 + rs.iloc[-1]))) if not pd.isna(rs.iloc[-1]) else 50
            # MACD
            ema12 = close.ewm(span=12).mean()
            ema26 = close.ewm(span=26).mean()
            macd_line = ema12 - ema26
            macd_signal = macd_line.ewm(span=9).mean()
            macd_hist = float(macd_line.iloc[-1] - macd_signal.iloc[-1]) if not pd.isna(macd_line.iloc[-1]) else 0
            # Momentum
            momentum = "BULLISH" if rsi > 55 and macd_hist > 0 else ("BEARISH" if rsi < 45 and macd_hist < 0 else "NEUTRAL")
            # Dynamic Regime Thresholds (Normalized to 95th Percentile)
            lookback = min(500, len(df))
            
            # Create full BB width series for percentile calculation
            bb_width_series = (4 * std60) / sma60 * 100
            
            atr_samples = (atr.iloc[-lookback:] / close.iloc[-lookback:] * 100).dropna()
            bb_samples = bb_width_series.iloc[-lookback:].dropna()
            atr_95 = float(np.percentile(atr_samples, 95)) if len(atr_samples) >= 20 else 2.0
            bb_95 = float(np.percentile(bb_samples, 95)) if len(bb_samples) >= 20 else 4.0
            
            # Cap the thresholds to prevent absurd values in extreme markets
            vol_atr_thresh = min(max(atr_95, 0.5), 3.0)
            vol_bb_thresh = min(max(bb_95, 1.0), 5.0)

            # Settings (fetched once; reused for soft-direction and breakout floor).
            try:
                from config.settings import get_settings as _gs
                _settings = _gs()
            except Exception:
                _settings = None
            soft_direction_enabled = bool(
                getattr(_settings, "regime_trend_soft_direction_enabled", True)
            )
            strong_adx_hold = bool(
                getattr(_settings, "regime_strong_adx_hold_trend", True)
            )
            key = f"{instrument}_{timeframe}"
            er_period = max(
                2,
                int(getattr(_settings, "regime_early_trend_er_period", 10) or 10),
            )
            kaufman_er = self._kaufman_efficiency_ratio(close, er_period)
            er_promotion = False
            er_promotion_reason = ""
            er_promotion_blocked = ""
            # Regime classification
            regime = MarketRegime.UNKNOWN
            confidence = 40
            # Lowered ADX threshold from 25 to 20 to be more flexible to early trends
            if adx_val >= 20:
                effective_direction = direction
                # Soft directional resolution: ADX>=20 means a trend IS present, but
                # the strict price>sma9>sma21 stack breaks on a pullback within a grind
                # (price dips under the fast SMA). Rather than dumping a structurally
                # intact trend into VOLATILE, resolve via MA structure: the fast SMA
                # above the slow SMA AND price above the slow SMA is a textbook uptrend
                # even when price is momentarily below the fast SMA. Chop-safe: needs a
                # stacked MA structure AND an RSI momentum bias that agrees, so it
                # cannot fire when the SMAs are intertwined (true chop) or rolling over.
                # RSI (not MACD-hist) is the bias guard: it survives a single-bar
                # pullback that briefly breaks the fast-SMA stack, where MACD-hist
                # would knife negative and wrongly veto an intact trend.
                if effective_direction == "NEUTRAL" and soft_direction_enabled:
                    if sma9 > sma21 and price > sma21 and rsi >= 50:
                        effective_direction = "UP"
                    elif sma9 < sma21 and price < sma21 and rsi <= 50:
                        effective_direction = "DOWN"
                if effective_direction == "UP":
                    regime = MarketRegime.TRENDING_UP
                    confidence = min(95, int(adx_val + 30))
                elif effective_direction == "DOWN":
                    regime = MarketRegime.TRENDING_DOWN
                    confidence = min(95, int(adx_val + 30))
                else:
                    # Strong ADX but ambiguous direction is NOT choppy. Hold the recent
                    # trend (hysteresis) instead of mislabeling it VOLATILE.
                    held = self._recent_trend_regime(key) if strong_adx_hold else None
                    if held is not None:
                        regime = held
                        confidence = min(90, int(adx_val + 25))
                    else:
                        regime = MarketRegime.VOLATILE
                        confidence = 55
            elif atr_pct > vol_atr_thresh or bb_width > vol_bb_thresh:
                regime = MarketRegime.VOLATILE
                confidence = min(85, int((atr_pct / vol_atr_thresh) * 40 + 30))
            else:
                regime = MarketRegime.RANGING
                confidence = min(85, int((30 - adx_val) * 3.5))
            er_decision = self._early_trend_er_promotion(
                settings=_settings,
                session_context=session_context,
                current_regime=regime,
                direction=direction,
                adx_val=adx_val,
                er_value=kaufman_er,
                er_period=er_period,
                candle_count=candle_count,
            )
            if er_decision.get("promote"):
                regime = er_decision["regime"]
                confidence = max(confidence, int(er_decision["confidence"]))
                er_promotion = True
                er_promotion_reason = str(er_decision.get("reason") or "")
                self._log_er_promotion_once(
                    key=key,
                    df=df,
                    regime=regime,
                    adx_val=adx_val,
                    er_value=kaufman_er,
                    reason=er_promotion_reason,
                )
            else:
                er_promotion_blocked = str(er_decision.get("blocked") or "")
            # Breakout detection — CONFIRMED breakouts only.
            # A price merely touching the 60-bar extreme is NOT a breakout: in a
            # dead, low-ADX tape it's noise grazing the band. A genuine breakout
            # shows momentum (ADX) or volatility expansion (ATR%/BB-width). Without
            # that confirmation we keep the honest RANGING/VOLATILE label so the
            # confidence pillar, routing and veto are all fed a truthful regime.
            try:
                breakout_adx_floor = float(getattr(_settings, "regime_breakout_adx_floor", 20.0) or 20.0)
            except Exception:
                breakout_adx_floor = 20.0
            vol_expanding = (atr_pct > vol_atr_thresh) or (bb_width > vol_bb_thresh)
            breakout_confirmed = (adx_val >= breakout_adx_floor) or vol_expanding
            high_60 = high.rolling(60).max().iloc[-1]
            low_60 = low.rolling(60).min().iloc[-1]
            breakout_direction = "NONE"
            raw_regime = regime.value
            if not pd.isna(high_60) and not pd.isna(low_60):
                touched_up = price >= high_60 * 0.999
                touched_down = price <= low_60 * 1.001
                if touched_up and breakout_confirmed:
                    regime = MarketRegime.BREAKOUT_UP
                    confidence = 70
                    breakout_direction = "UP"
                elif touched_down and breakout_confirmed:
                    regime = MarketRegime.BREAKOUT_DOWN
                    confidence = 70
                    breakout_direction = "DOWN"
                elif touched_up or touched_down:
                    # Unconfirmed band touch — keep the prior (RANGING/VOLATILE)
                    # label; flag it so downstream/telemetry can see the near-miss.
                    breakout_direction = "UNCONFIRMED_UP" if touched_up else "UNCONFIRMED_DOWN"
            raw_regime = regime.value
            range_day_guard_reason = ""
            guarded_regime = self._range_day_guarded_regime(
                settings=_settings,
                session_context=session_context,
                regime=regime.value,
                adx_val=adx_val,
            )
            if guarded_regime:
                range_day_guard_reason = f"confirmed_range_day_demoted_{regime.value}"
                regime = MarketRegime.RANGING
                confidence = min(confidence, 60)
            guarded_regime = regime.value
            # Track history (raw per-bar opinion)
            if key not in self._regime_history:
                self._regime_history[key] = []
            self._regime_history[key].append(regime.value)
            if len(self._regime_history[key]) > 50:
                self._regime_history[key] = self._regime_history[key][-50:]
            # Smoothing: expose a majority vote over the last N bars, not the raw bar —
            # damps the bar-to-bar flips that fed the gates an unstable regime.
            out_regime = regime.value
            if bool(getattr(_settings, "regime_smoothing_enabled", True)):
                window = int(getattr(_settings, "regime_smoothing_window", 5) or 5)
                smoothed = self._smooth_regime(key, window)
                if smoothed:
                    out_regime = smoothed
            guarded_out = self._range_day_guarded_regime(
                settings=_settings,
                session_context=session_context,
                regime=out_regime,
                adx_val=adx_val,
            )
            if guarded_out:
                if not range_day_guard_reason:
                    range_day_guard_reason = f"confirmed_range_day_demoted_smoothed_{out_regime}"
                out_regime = MarketRegime.RANGING.value
                confidence = min(confidence, 60)
            readiness = "FULL" if candle_count >= 60 else "PARTIAL"
            if readiness == "PARTIAL":
                confidence = min(confidence, 60)
            conviction = self._regime_conviction_vector(
                raw_regime=raw_regime,
                guarded_regime=guarded_regime,
                smoothed_regime=out_regime,
                confidence=confidence,
                adx_val=adx_val,
                atr_pct=atr_pct,
                bb_width=bb_width,
                vol_atr_thresh=vol_atr_thresh,
                vol_bb_thresh=vol_bb_thresh,
                direction=direction,
                momentum=momentum,
                rsi=rsi,
                macd_hist=macd_hist,
                er_value=kaufman_er,
                breakout_direction=breakout_direction,
                breakout_confirmed=breakout_confirmed,
                session_context=session_context,
                breadth_context=breadth_context,
                range_day_guard_reason=range_day_guard_reason,
            )
            return {
                "regime": out_regime, "confidence": confidence,
                "raw_regime": raw_regime,
                "guarded_regime": guarded_regime,
                "smoothed_regime": out_regime,
                "adx": round(adx_val, 1), "atr_pct": round(atr_pct, 2),
                "bb_width": round(bb_width, 2), "direction": direction,
                "kaufman_er": round(kaufman_er, 3),
                "kaufman_er_period": er_period,
                "er_promotion": er_promotion,
                "er_promotion_reason": er_promotion_reason,
                "er_promotion_blocked": er_promotion_blocked,
                "range_day_trend_guard": bool(range_day_guard_reason),
                "range_day_trend_guard_reason": range_day_guard_reason,
                "breakout_direction": breakout_direction,
                "breakout_confirmed": bool(breakout_confirmed),
                "conviction_vector": conviction["vector"],
                "conviction_label": conviction["label"],
                "conviction_summary": conviction["summary"],
                "conviction_explanation": conviction["explanation"],
                "rsi": round(rsi, 1), "macd_histogram": round(macd_hist, 2),
                "momentum": momentum,
                "description": (
                    f"{conviction['summary']} | label {out_regime}, "
                    f"ADX {adx_val:.0f}, ER {kaufman_er:.2f}, RSI {rsi:.0f}, dir {direction}"
                ),
                "candle_count": candle_count,
                "minimum_candles": 30,
                "full_confidence_candles": 60,
                "readiness": readiness,
                "ready_for_grading": True,
                "full_confidence": readiness == "FULL",
            }
        except Exception as e:
            logger.error(f"Regime detection error: {e}")
            return self._unknown(candle_count=candle_count, reason=f"Detection error: {e}")

    @staticmethod
    def _clamp01(value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = 0.0
        return max(0.0, min(1.0, parsed))

    @classmethod
    def _regime_conviction_vector(
        cls,
        *,
        raw_regime: str,
        guarded_regime: str,
        smoothed_regime: str,
        confidence: float,
        adx_val: float,
        atr_pct: float,
        bb_width: float,
        vol_atr_thresh: float,
        vol_bb_thresh: float,
        direction: str,
        momentum: str,
        rsi: float,
        macd_hist: float,
        er_value: float,
        breakout_direction: str,
        breakout_confirmed: bool,
        session_context: Optional[Dict[str, Any]],
        breadth_context: Optional[Dict[str, Any]],
        range_day_guard_reason: str,
    ) -> Dict[str, Any]:
        """Expose regime as evidence probabilities for shadow study/reporting.

        These are independent probabilities, not a mutually-exclusive softmax. A
        market can be both volatile and trending, or ranging while a breakout
        attempt is forming. Live gates still use the existing hard label.
        """
        direction = str(direction or "NEUTRAL").upper()
        momentum = str(momentum or "NEUTRAL").upper()
        raw_regime = str(raw_regime or "UNKNOWN").upper()
        guarded_regime = str(guarded_regime or raw_regime or "UNKNOWN").upper()
        smoothed_regime = str(smoothed_regime or guarded_regime or "UNKNOWN").upper()
        session_context = session_context or {}
        breadth_context = breadth_context or {}

        trend_strength = cls._clamp01((float(adx_val or 0.0) - 14.0) / 22.0)
        er_strength = cls._clamp01((float(er_value or 0.0) - 0.35) / 0.40)
        directional_strength = max(trend_strength, er_strength * 0.85)
        atr_ratio = float(atr_pct or 0.0) / max(float(vol_atr_thresh or 0.0), 0.01)
        bb_ratio = float(bb_width or 0.0) / max(float(vol_bb_thresh or 0.0), 0.01)
        volatility_expansion_prob = cls._clamp01((max(atr_ratio, bb_ratio) - 0.55) / 0.85)

        up_bias = 0.0
        down_bias = 0.0
        if direction == "UP":
            up_bias += 0.48
        elif direction == "DOWN":
            down_bias += 0.48
        if momentum == "BULLISH":
            up_bias += 0.24
        elif momentum == "BEARISH":
            down_bias += 0.24
        up_bias += cls._clamp01((float(rsi or 50.0) - 50.0) / 22.0) * 0.18
        down_bias += cls._clamp01((50.0 - float(rsi or 50.0)) / 22.0) * 0.18
        if float(macd_hist or 0.0) > 0:
            up_bias += 0.10
        elif float(macd_hist or 0.0) < 0:
            down_bias += 0.10

        trend_up_prob = cls._clamp01(up_bias * (0.30 + 0.70 * directional_strength))
        trend_down_prob = cls._clamp01(down_bias * (0.30 + 0.70 * directional_strength))

        breakout_direction = str(breakout_direction or "NONE").upper()
        if breakout_confirmed and breakout_direction in {"UP", "DOWN"}:
            breakout_prob = 0.78
            if breakout_direction == "UP":
                trend_up_prob = max(trend_up_prob, 0.68)
            else:
                trend_down_prob = max(trend_down_prob, 0.68)
        elif breakout_direction.startswith("UNCONFIRMED"):
            breakout_prob = 0.34
        else:
            breakout_prob = 0.10 if volatility_expansion_prob < 0.45 else 0.22

        day_type = str(session_context.get("day_type") or "").upper()
        try:
            day_conf = float(session_context.get("confidence") or 0.0)
        except Exception:
            day_conf = 0.0
        try:
            vwap_crosses = int(float(session_context.get("vwap_cross_count") or 0))
        except Exception:
            vwap_crosses = 0
        range_day_prob = cls._clamp01(0.20 + (1.0 - directional_strength) * 0.38)
        if day_type == "RANGE_DAY":
            range_day_prob = max(range_day_prob, cls._clamp01(0.45 + day_conf * 0.45))
        if vwap_crosses >= 3:
            range_day_prob = max(range_day_prob, min(0.88, 0.42 + vwap_crosses * 0.08))
        if smoothed_regime == "RANGING":
            range_day_prob = max(range_day_prob, 0.62)

        reversal_prob = 0.08
        if direction == "UP" and momentum == "BEARISH":
            reversal_prob = max(reversal_prob, 0.48)
        elif direction == "DOWN" and momentum == "BULLISH":
            reversal_prob = max(reversal_prob, 0.48)
        if raw_regime != guarded_regime or guarded_regime != smoothed_regime:
            reversal_prob = max(reversal_prob, 0.28)
        if breakout_direction.startswith("UNCONFIRMED"):
            reversal_prob = max(reversal_prob, 0.34)

        breadth_bias = str(breadth_context.get("bias") or "").upper()
        try:
            breadth_votes = int(float(breadth_context.get("aligned_count") or 0))
        except Exception:
            breadth_votes = 0
        breadth_stress_prob = 0.0
        if breadth_votes >= 3:
            broad_up = breadth_bias == "BULLISH"
            broad_down = breadth_bias == "BEARISH"
            final_up = smoothed_regime.endswith("_UP")
            final_down = smoothed_regime.endswith("_DOWN")
            if (broad_up and final_down) or (broad_down and final_up):
                breadth_stress_prob = 0.78
            else:
                breadth_stress_prob = max(0.25, volatility_expansion_prob * 0.55)
        elif breadth_context:
            breadth_stress_prob = 0.20

        if range_day_guard_reason:
            trend_up_prob = min(trend_up_prob, 0.48)
            trend_down_prob = min(trend_down_prob, 0.48)
            range_day_prob = max(range_day_prob, 0.74)
            reversal_prob = max(reversal_prob, 0.30)

        vector = {
            "trend_up_prob": round(cls._clamp01(trend_up_prob), 3),
            "trend_down_prob": round(cls._clamp01(trend_down_prob), 3),
            "range_prob": round(cls._clamp01(range_day_prob), 3),
            "breakout_prob": round(cls._clamp01(breakout_prob), 3),
            "reversal_prob": round(cls._clamp01(reversal_prob), 3),
            "volatility_expansion_prob": round(cls._clamp01(volatility_expansion_prob), 3),
            "breadth_stress_prob": round(cls._clamp01(breadth_stress_prob), 3),
        }
        label_key = max(vector, key=lambda k: vector[k])
        label_names = {
            "trend_up_prob": "Trend Up",
            "trend_down_prob": "Trend Down",
            "range_prob": "Range/Chop",
            "breakout_prob": "Breakout Attempt",
            "reversal_prob": "Reversal Risk",
            "volatility_expansion_prob": "Volatility Expansion",
            "breadth_stress_prob": "Breadth Stress",
        }
        label = label_names.get(label_key, "Mixed")
        explanation = [
            f"Hard label: raw {raw_regime}, guarded {guarded_regime}, smoothed {smoothed_regime}",
            f"Trend evidence: ADX {adx_val:.1f}, ER {er_value:.2f}, direction {direction}, momentum {momentum}",
            f"Range evidence: session {day_type or 'UNKNOWN'} conf {day_conf:.2f}, VWAP crosses {vwap_crosses}",
            f"Volatility evidence: ATR ratio {atr_ratio:.2f}, BB ratio {bb_ratio:.2f}",
        ]
        if breadth_context:
            explanation.append(f"Breadth evidence: {breadth_bias or 'MIXED'} {breadth_votes}/4 votes")
        if range_day_guard_reason:
            explanation.append(f"Range-day guard active: {range_day_guard_reason}")
        summary = (
            f"{label} leads ({vector[label_key]:.0%}); "
            f"up {vector['trend_up_prob']:.0%}, down {vector['trend_down_prob']:.0%}, "
            f"range {vector['range_prob']:.0%}, vol {vector['volatility_expansion_prob']:.0%}"
        )
        return {
            "vector": vector,
            "label": label,
            "summary": summary,
            "explanation": explanation,
        }

    @staticmethod
    def _setting_enabled(value, default: bool = True) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(value)

    @staticmethod
    def _kaufman_efficiency_ratio(close: pd.Series, period: int) -> float:
        try:
            period = max(2, int(period or 10))
            if close is None or len(close) <= period:
                return 0.0
            window = close.iloc[-(period + 1):].astype(float)
            net_move = abs(float(window.iloc[-1]) - float(window.iloc[0]))
            path = float(window.diff().abs().iloc[1:].sum())
            if path <= 0:
                return 0.0
            return max(0.0, min(1.0, net_move / path))
        except Exception:
            return 0.0

    def _early_trend_er_promotion(
        self,
        *,
        settings,
        session_context: Optional[Dict[str, Any]],
        current_regime: MarketRegime,
        direction: str,
        adx_val: float,
        er_value: float,
        er_period: int,
        candle_count: int,
    ) -> Dict[str, Any]:
        if not self._setting_enabled(
            getattr(settings, "regime_early_trend_er_promotion_enabled", True),
            True,
        ):
            return {}
        if current_regime in {
            MarketRegime.TRENDING_UP,
            MarketRegime.TRENDING_DOWN,
            MarketRegime.BREAKOUT_UP,
            MarketRegime.BREAKOUT_DOWN,
        }:
            return {}
        min_candles = max(30, er_period + 21)
        if candle_count < min_candles:
            return {"blocked": f"need_{min_candles}_candles"}
        adx_ceiling = float(
            getattr(settings, "regime_early_trend_er_adx_ceiling", 20.0) or 20.0
        )
        if float(adx_val or 0.0) >= adx_ceiling:
            return {}
        threshold = float(
            getattr(settings, "regime_early_trend_er_threshold", 0.60) or 0.60
        )
        if float(er_value or 0.0) < threshold:
            return {}
        day_ctx = session_context or {}
        day_type = str(day_ctx.get("day_type") or "").upper()
        try:
            day_conf = float(day_ctx.get("confidence") or 0.0)
        except Exception:
            day_conf = 0.0
        range_conf = float(
            getattr(settings, "regime_early_trend_er_range_day_min_confidence", 0.60)
            or 0.60
        )
        if day_type == "RANGE_DAY" and day_conf >= range_conf:
            return {"blocked": "confirmed_range_day"}
        if direction not in {"UP", "DOWN"}:
            return {"blocked": "sma_stack_not_directional"}
        promoted_regime = (
            MarketRegime.TRENDING_UP if direction == "UP" else MarketRegime.TRENDING_DOWN
        )
        confidence = min(
            78,
            max(60, int(45 + float(er_value or 0.0) * 30 + float(adx_val or 0.0) * 0.5)),
        )
        reason = (
            f"Kaufman ER promotion: ER {er_value:.2f}/{er_period}, "
            f"ADX {adx_val:.1f} below {adx_ceiling:.1f}, SMA stack {direction}"
        )
        return {
            "promote": True,
            "regime": promoted_regime,
            "confidence": confidence,
            "reason": reason,
        }

    def _range_day_guarded_regime(
        self,
        *,
        settings,
        session_context: Optional[Dict[str, Any]],
        regime: str,
        adx_val: float = 0.0,
    ) -> bool:
        if not self._setting_enabled(
            getattr(settings, "regime_range_day_trend_guard_enabled", True),
            True,
        ):
            return False
        reg = str(regime or "").upper()
        if reg not in {
            MarketRegime.TRENDING_UP.value,
            MarketRegime.TRENDING_DOWN.value,
            MarketRegime.BREAKOUT_UP.value,
            MarketRegime.BREAKOUT_DOWN.value,
        }:
            return False

        # ADX-strength escape (2026-07-24 audit): a genuinely strong trend (high ADX) is
        # real even when the day-type classifier labels the session a RANGE_DAY. Without
        # this, strong trend bars (ADX 44+) were demoted to RANGING, cascading into
        # choppy gating (mass signal rejection), scalp-lock exits (winners cut early) and
        # dormant trend re-entry — so the system missed big directional moves.
        adx_override = float(
            getattr(settings, "regime_range_day_trend_guard_adx_override", 35.0) or 0.0
        )
        if adx_override > 0.0 and float(adx_val or 0.0) >= adx_override:
            return False

        ctx = session_context or {}
        if str(ctx.get("day_type") or "").upper() != "RANGE_DAY":
            return False
        try:
            confidence = float(ctx.get("confidence") or 0.0)
        except Exception:
            confidence = 0.0
        min_conf = float(
            getattr(settings, "regime_range_day_trend_guard_min_confidence", 0.60)
            or 0.60
        )
        if confidence < min_conf:
            return False

        side = "UP" if reg.endswith("_UP") else "DOWN"
        opening_status = str(ctx.get("opening_range_status") or "").upper()
        try:
            vwap_crosses = int(float(ctx.get("vwap_cross_count") or 0))
        except Exception:
            vwap_crosses = 0
        try:
            above_vwap_pct = float(ctx.get("close_above_vwap_pct") or 50.0)
        except Exception:
            above_vwap_pct = 50.0

        directional_proof = (
            (
                side == "UP"
                and opening_status == "BREAK_UP"
                and above_vwap_pct >= 68.0
                and vwap_crosses <= 2
            )
            or (
                side == "DOWN"
                and opening_status == "BREAK_DOWN"
                and above_vwap_pct <= 32.0
                and vwap_crosses <= 2
            )
        )
        return not directional_proof

    def _log_er_promotion_once(
        self,
        *,
        key: str,
        df: pd.DataFrame,
        regime: MarketRegime,
        adx_val: float,
        er_value: float,
        reason: str,
    ) -> None:
        try:
            bar_id = str(df.index[-1]) if df is not None and len(df.index) else ""
            log_key = (str(key), bar_id, str(regime.value))
            seen = RegimeDetector._ER_PROMOTION_LOG_KEYS
            if log_key in seen:
                return
            # Drop the whole window rather than slicing it: a set has no order, so
            # keeping "the last 1024" kept an arbitrary 1024 and evicted live keys at
            # random. Clearing costs at most one repeated line per key.
            if len(seen) >= RegimeDetector._ER_PROMOTION_LOG_CAP:
                seen.clear()
            seen.add(log_key)
            logger.info(
                "Regime ER promotion: "
                f"{key} -> {regime.value} (ER={er_value:.2f}, ADX={adx_val:.1f}); {reason}"
            )
        except Exception:
            pass

    def _recent_trend_regime(self, key: str):
        """If the recent regime history is predominantly trending one way, return that
        trend — used as hysteresis so a strong-ADX but direction-ambiguous bar holds the
        trend instead of mislabeling it VOLATILE. Returns None when there is no clear
        recent trend (so genuine chop still falls through to VOLATILE)."""
        hist = self._regime_history.get(key, [])
        if len(hist) < 3:
            return None
        recent = hist[-5:]
        ups = sum(1 for r in recent if r in ("TRENDING_UP", "BREAKOUT_UP"))
        downs = sum(1 for r in recent if r in ("TRENDING_DOWN", "BREAKOUT_DOWN"))
        if ups >= 3 and ups > downs:
            return MarketRegime.TRENDING_UP
        if downs >= 3 and downs > ups:
            return MarketRegime.TRENDING_DOWN
        return None

    def _smooth_regime(self, key: str, window: int):
        """Majority vote over the last `window` raw regimes (damps bar-to-bar noise).
        Ties keep the most-recent label so the smoothed regime never lags a fresh,
        decisive shift by more than the window."""
        hist = self._regime_history.get(key, [])
        if not hist:
            return None
        recent = hist[-max(1, int(window)):]
        from collections import Counter
        counts = Counter(recent)
        top = counts.most_common(1)[0][1]
        # Among labels tied for the top count, prefer the most recent occurrence.
        tied = {r for r, c in counts.items() if c == top}
        for r in reversed(recent):
            if r in tied:
                return r
        return recent[-1]

    def should_trade(self, regime: str, signal_type: str) -> tuple:
        """
        Evaluate if trading is appropriate given the current regime.
        Returns (should_trade: bool, reason: str).

        PHILOSOPHY: Institutional Grade Filtering.
        We BLOCK trades in high-risk regimes unless they are A/A+.
        """
        canonical = normalize_regime(regime)
        if regime_aligns(canonical, signal_type):
            side = "Bullish" if str(signal_type or "").upper() == "BUY" else "Bearish"
            return True, f"Aligned with {side} Momentum"

        if canonical == MarketRegime.UNKNOWN:
            return True, "High risk regime (UNKNOWN). Unknown regime. Require A+ Grade and reduced size."

        if canonical == MarketRegime.VOLATILE:
            # These are high-risk. Signal processor must check for A/A+ grade only.
            return True, f"High risk regime ({canonical.value}). Require A+ Grade."

        if canonical == MarketRegime.RANGING:
            return True, "Range-bound — expect mean reversion"
            
        return True, "Proceed with caution"

    def _unknown(self, candle_count: int = 0, reason: str = "Insufficient data") -> Dict:
        vector = {
            "trend_up_prob": 0.0,
            "trend_down_prob": 0.0,
            "range_prob": 0.0,
            "breakout_prob": 0.0,
            "reversal_prob": 0.0,
            "volatility_expansion_prob": 0.0,
            "breadth_stress_prob": 0.0,
        }
        return {
            "regime": "UNKNOWN",
            "confidence": 0,
            "raw_regime": "UNKNOWN",
            "guarded_regime": "UNKNOWN",
            "smoothed_regime": "UNKNOWN",
            "adx": 0,
            "atr_pct": 0,
            "bb_width": 0,
            "direction": "NEUTRAL",
            "rsi": 50,
            "macd_histogram": 0,
            "momentum": "NEUTRAL",
            "conviction_vector": vector,
            "conviction_label": "Unknown",
            "conviction_summary": reason,
            "conviction_explanation": [reason],
            "description": reason,
            "candle_count": int(candle_count or 0),
            "minimum_candles": 30,
            "full_confidence_candles": 60,
            "readiness": "WARMING",
            "ready_for_grading": False,
            "full_confidence": False,
        }
