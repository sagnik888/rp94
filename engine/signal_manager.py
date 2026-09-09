"""
Signal Manager — Signal Lifecycle & Grading
═══════════════════════════════════════════════════════════════

Manages signal generation, deduplication, grading, and history.
"""

from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Dict, List, Optional
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from loguru import logger
from engine.intelligence_score import normalize_intelligence_score
from engine.signal_quality_policy import confidence_to_grade, setting_enabled

from engine.ut_bot_core import UTBotSignal


@dataclass
class GradedSignal:
    """A signal with quality grading"""
    signal: UTBotSignal
    grade: str = "B"           # A+, A, B+, B, C
    confidence: float = 0.5    # 0.0 - 1.0
    reasons: List[str] = field(default_factory=list)
    intelligence_score: float = 0.0
    confluence_score: float = 0.0
    is_actionable: bool = True
    created_at: datetime = field(default_factory=datetime.now)


class SignalManager:
    """
    Manages signal lifecycle:
    1. Receives raw signals from UT Bot engines
    2. Deduplicates (same instrument, same direction within N bars)
    3. Grades based on confluence + intelligence
    4. Maintains history for audit trail
    """

    def __init__(self, dedup_window_seconds: int = 300):
        self.dedup_window = timedelta(seconds=dedup_window_seconds)
        self.active_signals: Dict[str, GradedSignal] = {}  # key: instrument
        self.max_history = 500
        self.signal_history: Deque[GradedSignal] = deque(maxlen=self.max_history)

    @staticmethod
    def _timestamp_seconds(value: datetime) -> float:
        """Stable epoch comparison for mixed aware/naive market timestamps."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return float(value.timestamp())

    def process_signal(
        self,
        signal: UTBotSignal,
        confluence_score: float = 0.0,
        intelligence_score: float = 0.0,
        regime: str = "UNKNOWN",
        day_type: str = "",
        day_type_confidence: float = 0.0,
    ) -> Optional[GradedSignal]:
        """
        Process a new signal through grading and deduplication.

        Returns GradedSignal if actionable, None if filtered out.
        """
        # Deduplicate only the same direction.  A genuine stop-and-reverse signal
        # must be allowed through immediately, and epoch arithmetic avoids the
        # old UTC-vs-IST `.replace(tzinfo=None)` 5.5-hour skew.
        key = f"{signal.instrument}_{signal.timeframe}"
        existing = self.active_signals.get(key)
        if (
            existing
            and existing.signal.signal_type == signal.signal_type
            and signal.timestamp is not None
            and existing.signal.timestamp is not None
        ):
            time_diff_seconds = abs(
                self._timestamp_seconds(signal.timestamp)
                - self._timestamp_seconds(existing.signal.timestamp)
            )
            if time_diff_seconds <= self.dedup_window.total_seconds():
                return None

        # ── Grading ──
        grade, confidence, reasons = self._grade_signal(
            signal,
            confluence_score,
            intelligence_score,
            regime,
            day_type=day_type,
            day_type_confidence=day_type_confidence,
        )

        graded = GradedSignal(
            signal=signal,
            grade=grade,
            confidence=confidence,
            reasons=reasons,
            intelligence_score=intelligence_score,
            confluence_score=confluence_score,
            is_actionable=grade in ["A+", "A", "B+", "B"],  # Only C is blocked
        )

        # Update active and history
        self.active_signals[key] = graded
        self.signal_history.append(graded)

        logger.info(
            f"📊 Signal: {signal.signal_type} {signal.instrument} "
            f"@ {signal.price:.2f} | Grade: {grade} ({confidence:.0%}) "
            f"| ADX: {signal.adx_value:.1f}"
        )

        return graded

    def _grade_signal(
        self,
        signal: UTBotSignal,
        confluence_score: float,
        intelligence_score: float,
        regime: str = "UNKNOWN",
        vix_value: float = 15.0,
        day_type: str = "",
        day_type_confidence: float = 0.0,
    ) -> tuple:
        """
        Mixed-Intelligence Grading Engine:
        Combines 3 Pillars:
        1. TECHNICAL (Price/Trend/Momentum)
        2. INTELLIGENCE (Volume/OI/PCR)
        3. REGIME (Market Structure/Context)
        """
        from config.settings import get_settings
        _s = get_settings()
        tech_score = 0.0
        
        # ── VIX Patch ──
        vix_penalty = 0.0
        reasons = []
        try:
            _vix_settings = _s
            vix_high_threshold = float(
                getattr(_vix_settings, "signal_vix_high_threshold", 20.0) or 20.0
            )
            vix_low_threshold = float(
                getattr(_vix_settings, "signal_vix_low_threshold", 12.0) or 12.0
            )
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            logger.debug(f"Signal VIX threshold settings unavailable: {exc}")
            vix_high_threshold, vix_low_threshold = 20.0, 12.0
        if vix_value > vix_high_threshold:
            vix_penalty = 0.10
            reasons.append(f"VIX High ({vix_value:.1f})")
        elif vix_value < vix_low_threshold:
            vix_penalty = -0.05
            reasons.append(f"VIX Low ({vix_value:.1f})")
        intel_score_part = 0.0
        regime_score = 0.0

        # ── Pillar 1: TECHNICAL (40% Weight) ──
        # ADX Momentum
        adx = min(50.0, max(15.0, signal.adx_value))
        if adx > 20:
            adx_score = ((adx - 20) / 30.0) * 0.15
            tech_score += adx_score
            
            # ── ADX Penalization Patch ──
            if adx > 40:
                settings = _s
                if getattr(settings, "ut_dynamic_confidence", False):
                    tech_score -= 0.10
                    reasons.append(f"Momentum: Extreme (ADX: {signal.adx_value:.1f}) - Risk of exhaustion")
                    
            if adx > 30 and adx <= 40:
                reasons.append(f"Momentum: Strong (ADX: {signal.adx_value:.1f})")
            elif adx <= 30:
                reasons.append(f"Momentum: Healthy (ADX: {signal.adx_value:.1f})")
        
        # MTF Confluence
        direction_sign = 1.0 if signal.signal_type == "BUY" else -1.0
        aligned_conf = max(-1.0, min(1.0, float(confluence_score or 0.0) * direction_sign))
        if abs(aligned_conf) > 0.2:
            mtf_score = ((abs(aligned_conf) - 0.2) / 0.8) * 0.20
            tech_score += mtf_score if aligned_conf > 0 else -mtf_score
            if aligned_conf > 0.6:
                reasons.append("Technical: Strong Multi-TF Confluence")
            elif aligned_conf > 0:
                reasons.append("Technical: Moderate Multi-TF Confluence")
            else:
                reasons.append("Technical: Multi-TF trend CONTRADICTS signal")
            
        # Stop Distance
        if signal.stop_distance > 0 and signal.atr_value > 0:
            atr_ratio = signal.stop_distance / signal.atr_value
            if atr_ratio < 1.5:
                stop_score = min(0.05, ((1.5 - atr_ratio) / 1.0) * 0.05)
                tech_score += stop_score
                reasons.append(f"Technical: Tight Risk-Window ({atr_ratio:.1f}x ATR)")

        # ── Pillar 2: INTELLIGENCE (30% Weight) ──
        # Align intelligence score with signal direction
        intelligence_score = normalize_intelligence_score(intelligence_score)
        aligned_intel = intelligence_score if signal.signal_type == "BUY" else -intelligence_score
        
        intel_score_part = max(-0.20, min(0.30, aligned_intel * 0.30))
        
        if aligned_intel > 0.5:
            reasons.append("Intelligence: Institutional Volume/OI Confirmed")
        elif aligned_intel > 0.2:
            reasons.append("Intelligence: Moderate Data Support")
        elif aligned_intel < -0.3:
            reasons.append("Intelligence: Data CONTRADICTS signal")

        # ── Pillar 3: REGIME (30% Weight) ──
        # This is the 'Context' layer requested in Audit
        if regime in {"TRENDING_UP", "BREAKOUT_UP"}:
            if signal.signal_type == "BUY":
                regime_score += 0.30
                reasons.append("Regime: Trend-Following alignment")
            else:
                regime_score -= 0.25
                reasons.append("Regime: COUNTER-TREND (High Risk)")
        elif regime in {"TRENDING_DOWN", "BREAKOUT_DOWN"}:
            if signal.signal_type == "SELL":
                regime_score += 0.30
                reasons.append("Regime: Trend-Following alignment")
            else:
                regime_score -= 0.25
                reasons.append("Regime: COUNTER-TREND (High Risk)")
        elif regime in ["RANGING", "SIDEWAYS", "VOLATILE", "MEAN_REVERTING"]:
            regime_score -= 0.10
            reasons.append(f"Regime: Range-bound noise ({regime})")
        elif regime == "UNKNOWN":
            regime_score -= 0.30
            reasons.append(f"Regime: UNKNOWN (High Risk Penalty)")

        # ── Range-day damp on the regime pillar ──
        # 2026-07-03: the 5min detector read TRENDING 54-60% of a confirmed
        # RANGE_DAY, and the +0.30 alignment bonus on those phantom trends
        # manufactured A+ labels that went 0/6. On a confirmed range day the
        # trend-alignment bonus is capped — reversal/thrust paths are graded
        # elsewhere and unaffected.
        try:
            from config.settings import get_settings as _gs_damp
            _damp_settings = _gs_damp()
            if (
                bool(getattr(_damp_settings, "range_day_regime_pillar_damp_enabled", True))
                and str(day_type or "").upper() == "RANGE_DAY"
                and float(day_type_confidence or 0.0)
                >= float(getattr(_damp_settings, "range_day_regime_pillar_min_confidence", 0.60) or 0.60)
            ):
                _cap = float(getattr(_damp_settings, "range_day_regime_pillar_bonus_cap", 0.10) or 0.10)
                if regime_score > _cap:
                    regime_score = _cap
                    reasons.append(
                        f"Range-day damp: {regime} alignment bonus capped at +{_cap:.2f} "
                        "(session is a confirmed range day)"
                    )
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            logger.debug(f"Range-day regime damp settings unavailable: {exc}")

        # ── Pillar 4: MANIPULATION FILTER (Global Penalty) ──
        c = getattr(signal, "raw_candle", None)
        if isinstance(c, dict) and {"open", "high", "low", "close"}.issubset(c):
            body = abs(c['close'] - c['open'])
            total = c['high'] - c['low']
            if total > 0:
                wick_top = c['high'] - max(c['open'], c['close'])
                wick_bottom = min(c['open'], c['close']) - c['low']
                # Penalize BUY if it has a massive TOP wick (rejection of higher prices)
                if signal.signal_type == "BUY" and wick_top > (body * 2.5):
                    tech_score -= 0.4 
                    reasons.append("⚠️ ALERT: Supply-Trap Wick Detected")
                # Penalize SELL if it has a massive BOTTOM wick (rejection of lower prices)
                elif signal.signal_type == "SELL" and wick_bottom > (body * 2.5):
                    tech_score -= 0.4
                    reasons.append("⚠️ ALERT: SL-Hunt Wick Detected")

        # Final Mixed Score Calculation
        total_score = tech_score + intel_score_part + regime_score - vix_penalty
        # ── Confidence de-saturation ──
        # Legacy: clamp(0.5 + total_score, 0, 1) pinned at 1.0 for any setup whose
        # pillars summed past +0.5 — ~45% of signals graded "A+ @ 100%", so the
        # score had no ranking power. A logistic squash keeps the 0.5 midpoint and
        # tracks the old linear mapping across the gate band (~0.55-0.85) while
        # asymptoting to 1.0 instead of clipping. k≈5 ≈ old slope at the floors.
        use_logistic = False
        logistic_k = 5.0
        try:
            from config.settings import get_settings as _get_settings
            _s = _get_settings()
            use_logistic = setting_enabled(getattr(_s, "ut_confidence_logistic", True), True)
            logistic_k = float(getattr(_s, "ut_confidence_logistic_k", 5.0) or 5.0)
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            logger.debug(f"Confidence logistic settings unavailable: {exc}")
            use_logistic = True
        if use_logistic:
            import math
            confidence = 1.0 / (1.0 + math.exp(-logistic_k * total_score))
        else:
            confidence = max(0.0, min(1.0, 0.5 + total_score))
        confidence = max(0.0, min(1.0, confidence))
        _confidence_pre_adjust = confidence
        # R5: optional isotonic calibration to realized win-rate (default OFF;
        # no-op until the outcome dataset is ready). Makes confidence a probability.
        try:
            from config.settings import get_settings as _gs2
            from intelligence.confidence_calibration import calibrate as _calibrate_conf
            _settings = _gs2()
            confidence = max(
                0.0,
                min(
                    1.0,
                    _calibrate_conf(
                        confidence,
                        _settings,
                        instrument=getattr(signal, "instrument", ""),
                        timeframe=getattr(signal, "timeframe", ""),
                        regime=regime,
                    ),
                ),
            )
        except Exception as exc:
            logger.debug(f"Confidence calibration unavailable: {exc}")
            _settings = None

        try:
            if _settings is None:
                from config.settings import get_settings as _gs_cap
                _settings = _gs_cap()
            raw_adx = float(getattr(signal, "adx_value", 0.0) or 0.0)
            cap = float(getattr(_settings, "ut_exhaustion_confidence_cap", 0.82) or 0.82)
            if raw_adx > 40.0 and 0.0 < cap < 1.0 and confidence > cap:
                confidence = cap
                reasons.append(
                    f"Exhaustion cap: ADX {raw_adx:.0f} > 40 caps confidence at {cap:.0%}"
                )
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            logger.debug(f"Exhaustion confidence cap unavailable: {exc}")

        try:
            if _settings is None:
                from config.settings import get_settings as _gs3
                _settings = _gs3()
            scanner_mode = str(getattr(_settings, "scanner_engine", "") or "").lower()
            if scanner_mode in {"adaptive_dual", "quad_prime"}:
                agreement = max(0, min(3, int(getattr(signal, "trend_agreement", 0) or 0)))
                per = max(
                    0.0,
                    float(getattr(_settings, "utbot_prime_confidence_per_agreement", 0.03) or 0.0),
                )
                cap = max(
                    0.0,
                    float(getattr(_settings, "utbot_prime_max_confidence_bonus", 0.09) or 0.0),
                )
                bonus = min(cap, agreement * per)
                if bonus > 0:
                    confidence = max(0.0, min(1.0, confidence + bonus))
                    sources = [
                        str(src).lower()
                        for src in (getattr(signal, "trend_agreement_sources", None) or [])
                        if str(src or "").strip()
                    ]
                    source_text = f" ({'+'.join(sources)})" if sources else ""
                    label = (
                        "Adaptive Dual Lane A"
                        if scanner_mode == "adaptive_dual"
                        else "Quad Prime"
                    )
                    reasons.append(
                        f"{label} agreement {agreement}/3{source_text}: +{bonus:.0%} confidence"
                    )
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            logger.debug(f"Prime agreement confidence settings unavailable: {exc}")

        # Grade is purely a display bucket of confidence — single source of truth
        # lives in engine.signal_quality_policy.confidence_to_grade.
        grade = confidence_to_grade(confidence)

        # ── Pillar breakdown telemetry ──
        # The three pillars collapse into one number before anything is recorded, so a
        # rejected B+ and a taken A+ looked identical in the tape apart from the score —
        # there was no way to tell whether a grade came from technicals, from intel, or
        # from the regime bonus, nor which post-hoc adjustment moved it. Written onto the
        # signal (which IS the candidate object at every call site) so it rides through to
        # rtracker without changing this function's return arity.
        try:
            setattr(signal, "grade_pillars", {
                "tech": round(float(tech_score), 4),
                "intel": round(float(intel_score_part), 4),
                "regime": round(float(regime_score), 4),
                "vix_penalty": round(float(vix_penalty), 4),
                "total_score": round(float(total_score), 4),
                "confidence_pre_adjust": round(float(_confidence_pre_adjust), 4),
                "confidence_final": round(float(confidence), 4),
                "adjustments": round(float(confidence - _confidence_pre_adjust), 4),
                "grade": str(grade),
            })
        except Exception as exc:  # telemetry must never break grading
            logger.debug(f"Pillar breakdown telemetry unavailable: {exc}")

        return grade, confidence, reasons

    def get_active_signals(self, mode: str = "HISTORICAL", instrument: str = None) -> List[Dict]:
        """Get all current active signals as dicts"""
        result = []
        
        import datetime
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        today = datetime.datetime.now(IST).date()
        
        for key, gs in self.active_signals.items():
            if instrument and gs.signal.instrument != instrument:
                continue
            # In REAL mode, only show signals from today
            if mode == "REAL" and gs.signal.timestamp:
                sig_date = gs.signal.timestamp.date()
                if sig_date != today:
                    continue
                    
            result.append({
                "instrument": gs.signal.instrument,
                "timeframe": gs.signal.timeframe,
                "type": gs.signal.signal_type,
                "price": gs.signal.price,
                "trailing_stop": gs.signal.trailing_stop,
                "stop_distance": gs.signal.stop_distance,
                "grade": gs.grade,
                "confidence": gs.confidence,
                "reasons": gs.reasons,
                "timestamp": gs.signal.timestamp.isoformat() if gs.signal.timestamp else "",
                "is_actionable": gs.is_actionable,
            })
        return result

    def get_signal_stats(self) -> Dict:
        """Get signal statistics"""
        if not self.signal_history:
            return {"total": 0, "buys": 0, "sells": 0, "grade_dist": {}}

        buys = sum(1 for s in self.signal_history if s.signal.signal_type == "BUY")
        sells = sum(1 for s in self.signal_history if s.signal.signal_type == "SELL")
        grades = {}
        for s in self.signal_history:
            grades[s.grade] = grades.get(s.grade, 0) + 1

        return {
            "total": len(self.signal_history),
            "buys": buys,
            "sells": sells,
            "grade_distribution": grades,
            "avg_confidence": sum(s.confidence for s in self.signal_history) / len(self.signal_history),
        }
