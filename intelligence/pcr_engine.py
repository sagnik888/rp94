"""
PCR Engine — Put-Call Ratio Analysis
═══════════════════════════════════════════════════════════════

PCR Interpretation (Contrarian):
- PCR > 1.0: More puts than calls (Bearish sentiment → contrarian bullish)
- PCR < 0.7: More calls than puts (Bullish sentiment → contrarian bearish)
- PCR 0.7-1.0: Neutral
"""

import pandas as pd
import numpy as np
from typing import Dict, List
import threading
import hashlib
from collections import deque
from loguru import logger


class PCREngine:
    """Put-Call Ratio analysis engine with trend tracking"""

    def __init__(self, history_size: int = 50):
        self._pcr_history: Dict[str, deque] = {}
        self._pcr_last_observation: Dict[str, str] = {}
        self.history_size = history_size
        self._lock = threading.RLock()
        self.thresholds = {
            "extreme_fear": 1.5,
            "bearish": 1.0,
            "neutral_high": 0.9,
            "neutral_low": 0.7,
            "bullish": 0.5,
            "extreme_greed": 0.3,
        }

    def analyze(
        self,
        options_chain: pd.DataFrame,
        instrument: str = "",
        spot_price: float = 0.0,
        strike_interval: int = 50,
        near_strikes: int = 5,
    ) -> Dict:
        """
        Calculate and analyze PCR.

        Returns PCR value, sentiment, contrarian signal, and trend.
        """
        if options_chain is None or len(options_chain) < 3:
            return self._empty_result()

        try:
            # ── OI-based PCR ──
            broad_put_oi = pd.to_numeric(options_chain['put_oi'], errors='coerce').fillna(0)
            broad_call_oi = pd.to_numeric(options_chain['call_oi'], errors='coerce').fillna(0)
            total_put_oi = float(broad_put_oi.sum())
            total_call_oi = float(broad_call_oi.sum())
            # A literal 0 from an unavailable side is not a valid sentiment
            # observation.  Both sides need positive exchange OI before PCR is
            # allowed into scoring or the dashboard.
            broad_real_pair_count = int(
                ((broad_call_oi > 0) & (broad_put_oi > 0)).sum()
            )
            broad_available = bool(
                broad_real_pair_count >= 3
                and total_call_oi > 0
                and total_put_oi > 0
            )
            pcr_oi = float(total_put_oi / total_call_oi) if total_call_oi > 0 else 0

            # ── Volume-based PCR ──
            total_put_vol = float(pd.to_numeric(options_chain['put_volume'], errors='coerce').fillna(0).sum())
            total_call_vol = float(pd.to_numeric(options_chain['call_volume'], errors='coerce').fillna(0).sum())
            pcr_vol = float(total_put_vol / total_call_vol) if total_call_vol > 0 else 0

            pcr_basis = "BROAD"
            near_pcr_oi = pcr_oi
            near_pcr_vol = pcr_vol
            near_count = len(options_chain)
            near_real_pair_count = broad_real_pair_count
            if spot_price > 0 and strike_interval > 0 and 'strike' in options_chain.columns:
                atm = round(spot_price / strike_interval) * strike_interval
                min_strike = atm - (near_strikes * strike_interval)
                max_strike = atm + (near_strikes * strike_interval)
                near_chain = options_chain[(options_chain['strike'] >= min_strike) & (options_chain['strike'] <= max_strike)]
                if len(near_chain) >= 3:
                    near_put_oi = float(pd.to_numeric(near_chain['put_oi'], errors='coerce').fillna(0).sum())
                    near_call_oi = float(pd.to_numeric(near_chain['call_oi'], errors='coerce').fillna(0).sum())
                    near_put_vol = float(pd.to_numeric(near_chain['put_volume'], errors='coerce').fillna(0).sum())
                    near_call_vol = float(pd.to_numeric(near_chain['call_volume'], errors='coerce').fillna(0).sum())
                    near_count = len(near_chain)
                    near_call_series = pd.to_numeric(
                        near_chain['call_oi'], errors='coerce'
                    ).fillna(0)
                    near_put_series = pd.to_numeric(
                        near_chain['put_oi'], errors='coerce'
                    ).fillna(0)
                    near_real_pair_count = int(
                        ((near_call_series > 0) & (near_put_series > 0)).sum()
                    )
                    # Prefer near-money PCR only when both sides carry real OI.
                    # Otherwise retain a valid broad-chain observation instead
                    # of manufacturing a zero/STRONG_BEARISH near-money signal.
                    if (
                        near_real_pair_count >= 3
                        and near_call_oi > 0
                        and near_put_oi > 0
                    ):
                        near_pcr_oi = float(near_put_oi / near_call_oi)
                        near_pcr_vol = float(near_put_vol / near_call_vol) if near_call_vol > 0 else 0
                        pcr_basis = "NEAR_MONEY"

            # ── Interpret PCR ──
            primary_pcr = near_pcr_oi if pcr_basis == "NEAR_MONEY" else pcr_oi
            available = broad_available and primary_pcr > 0
            sentiment, signal = (
                self._interpret(primary_pcr)
                if available
                else ("UNKNOWN", "NEUTRAL")
            )

            # ── Track distinct source observations ──
            # The aggregator can request 1/5/15m intelligence against one option-chain
            # snapshot.  Scan count is not time, so repeated reads must not change PCR
            # trend.  Prefer the provider observation id; derive a stable fingerprint
            # for legacy chain frames that do not yet carry one.
            expiry = str(options_chain.attrs.get("expiry") or "")
            observation_id = str(options_chain.attrs.get("observation_id") or "")
            if not observation_id:
                fingerprint = "|".join((
                    str(round(total_put_oi, 4)), str(round(total_call_oi, 4)),
                    str(round(total_put_vol, 4)), str(round(total_call_vol, 4)),
                    str(len(options_chain)), expiry,
                ))
                observation_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
            provider = str(options_chain.attrs.get("oi_provider") or options_chain.attrs.get("provider") or "UNKNOWN")
            covered_chain = options_chain.loc[(broad_call_oi > 0) & (broad_put_oi > 0)]
            universe = ",".join(sorted(str(v) for v in covered_chain.get("strike", [])))
            # Near-money selection may change as spot moves even on one unchanged
            # broad chain; it is a different PCR population, not a trend observation.
            near_identity = str(round(float(spot_price) / max(float(strike_interval), 1))) if spot_price else ""
            history_key = f"{str(instrument).upper()}|{expiry or 'ACTIVE'}|{provider}|{universe}|{pcr_basis}|{near_identity}"
            with self._lock:
                if history_key not in self._pcr_history:
                    self._pcr_history[history_key] = deque(maxlen=self.history_size)
                if available and self._pcr_last_observation.get(history_key) != observation_id:
                    self._pcr_history[history_key].append(primary_pcr)
                    self._pcr_last_observation[history_key] = observation_id
    
                # ── PCR Trend ──
                history = list(self._pcr_history[history_key])
                
            trend = "STABLE"
            if len(history) >= 5:
                recent = np.mean(history[-3:])
                prior = np.mean(history[-6:-3]) if len(history) >= 6 else np.mean(history[:3])
                change = recent - prior
                if change > 0.1:
                    trend = "RISING"  # Becoming more bearish → contrarian bullish
                elif change < -0.1:
                    trend = "FALLING"  # Becoming more bullish → contrarian bearish
                else:
                    trend = "STABLE"

            return {
                "available": available,
                "status": "OK" if available else "UNAVAILABLE",
                "reason": (
                    ""
                    if available
                    else "Need at least 3 strikes with positive paired call and put OI"
                ),
                "pcr_oi": round(pcr_oi, 3),
                "pcr_volume": round(pcr_vol, 3),
                "pcr_oi_broad": round(pcr_oi, 3),
                "pcr_volume_broad": round(pcr_vol, 3),
                "pcr_near_oi": round(near_pcr_oi, 3),
                "pcr_near_volume": round(near_pcr_vol, 3),
                "pcr_basis": pcr_basis,
                "near_strike_count": int(near_count),
                "near_real_oi_pair_count": int(near_real_pair_count),
                "broad_real_oi_pair_count": int(broad_real_pair_count),
                "primary_pcr": round(primary_pcr, 3),
                "total_put_oi": int(total_put_oi),
                "total_call_oi": int(total_call_oi),
                "sentiment": sentiment,
                "signal": signal,
                "raw_sentiment": sentiment,
                "contrarian_signal": signal,
                "interpretation_model": "CONTRARIAN",
                "trend": trend,
                "pcr_history": history[-10:] if len(history) > 0 else [],
                "observation_id": observation_id,
                "interpretation": self._get_interpretation(primary_pcr, sentiment, signal, trend),
            }

        except Exception as e:
            logger.error(f"PCR analysis error: {e}")
            return self._empty_result()

    def _interpret(self, pcr: float) -> tuple:
        """Interpret PCR value → (sentiment, contrarian_signal)"""
        if pcr >= self.thresholds["extreme_fear"]:
            return "EXTREME_FEAR", "STRONG_BULLISH"
        elif pcr >= self.thresholds["bearish"]:
            return "BEARISH", "BULLISH"
        elif pcr >= self.thresholds["neutral_low"]:
            return "NEUTRAL", "NEUTRAL"
        elif pcr >= self.thresholds["bullish"]:
            return "BULLISH", "BEARISH"
        else:
            return "EXTREME_GREED", "STRONG_BEARISH"

    def _get_interpretation(self, pcr: float, sentiment: str, signal: str, trend: str) -> str:
        """Generate human-readable interpretation"""
        trend_text = {
            "RISING": "rising (more puts being added)",
            "FALLING": "falling (more calls being added)",
            "STABLE": "stable",
        }
        return (
            f"PCR {pcr:.3f} → {sentiment} sentiment | "
            f"Contrarian: {signal} | Trend: {trend_text.get(trend, trend)}"
        )

    def _empty_result(self) -> Dict:
        return {
            "available": False, "status": "UNAVAILABLE", "reason": "Option chain unavailable or has fewer than 3 strikes",
            "pcr_oi": 0, "pcr_volume": 0, "pcr_oi_broad": 0, "pcr_volume_broad": 0,
            "pcr_near_oi": 0, "pcr_near_volume": 0, "pcr_basis": "NONE",
            "near_strike_count": 0, "near_real_oi_pair_count": 0,
            "broad_real_oi_pair_count": 0, "primary_pcr": 0,
            "total_put_oi": 0, "total_call_oi": 0,
            "sentiment": "UNKNOWN", "signal": "NEUTRAL", "trend": "UNKNOWN",
            "raw_sentiment": "UNKNOWN", "contrarian_signal": "NEUTRAL", "interpretation_model": "CONTRARIAN",
            "pcr_history": [], "interpretation": "No data",
        }
