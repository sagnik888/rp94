"""
Intelligence Aggregator — Combines all intelligence into a unified score
═══════════════════════════════════════════════════════════════
"""
import time
import pandas as pd
from typing import Any, Dict, List, Optional
from loguru import logger
from intelligence.volume_profiler import VolumeProfiler
from intelligence.oi_tracker import OITracker
from intelligence.pcr_engine import PCREngine
from intelligence.greeks_engine import GreeksEngine
from intelligence.regime_detector import RegimeDetector
from intelligence.order_flow import OrderFlowAnalyzer


class IntelligenceAggregator:
    """Combines all market intelligence sources into a single score"""

    def __init__(self):
        self.volume = VolumeProfiler()
        self.oi = OITracker()
        self.pcr = PCREngine()
        self.greeks = GreeksEngine()
        self.regime = RegimeDetector()
        self.order_flow = OrderFlowAnalyzer()
        self._last_divergence_warning = {}

    def _warn_divergence_once(self, instrument: str, kind: str, message: str, throttle_seconds: Optional[float] = None) -> None:
        if throttle_seconds is None:
            # Default to one warning per 5min bar: the old 15s default re-fired
            # on every ~30s intel cycle (57 repeats in one session) and drowned logs.
            try:
                from config.settings import get_settings
                throttle_seconds = float(
                    getattr(get_settings(), "divergence_warning_throttle_seconds", 300.0) or 300.0
                )
            except Exception:
                throttle_seconds = 300.0
        now = time.time()
        key = (str(instrument or ""), str(kind or ""))
        last = float(self._last_divergence_warning.get(key, 0.0) or 0.0)
        if now - last >= max(1.0, float(throttle_seconds or 300.0)):
            self._last_divergence_warning[key] = now
            logger.warning(message)

    def _implied_iv_from_price(self, spot: float, strike: float, days: float, premium: float, opt_type: str) -> Optional[float]:
        if spot <= 0 or strike <= 0 or days <= 0 or premium <= 0:
            return None
        intrinsic = max(0.0, spot - strike) if opt_type == "call" else max(0.0, strike - spot)
        if premium + 0.05 < intrinsic:
            return None
        low, high = 0.01, 3.0
        for _ in range(40):
            mid = (low + high) / 2.0
            model_price = float(self.greeks.calculate(spot, strike, days, mid, opt_type).get("price", 0.0) or 0.0)
            if model_price < premium:
                low = mid
            else:
                high = mid
        solved = max(0.01, min(3.0, (low + high) / 2.0))
        if solved <= 0.0101 or solved >= 2.999:
            return None
        return solved

    def _chain_iv_context(self, options_chain: pd.DataFrame, spot_price: float, days_to_expiry: float) -> Dict:
        if options_chain is None or options_chain.empty or "strike" not in options_chain.columns:
            return {"call_iv": 0.15, "put_iv": 0.15, "source": "default"}
        row = options_chain.iloc[(options_chain["strike"].astype(float) - float(spot_price)).abs().argsort()[:1]]
        atm_row = row.iloc[0]
        call_iv = None
        put_iv = None
        for key in ("call_iv", "ce_iv", "iv_call"):
            if key in atm_row and pd.notna(atm_row.get(key)):
                call_iv = float(atm_row.get(key))
                break
        for key in ("put_iv", "pe_iv", "iv_put"):
            if key in atm_row and pd.notna(atm_row.get(key)):
                put_iv = float(atm_row.get(key))
                break
        if call_iv and call_iv > 1:
            call_iv /= 100.0
        if put_iv and put_iv > 1:
            put_iv /= 100.0
        source = "chain_iv" if call_iv or put_iv else "default"
        strike = float(atm_row.get("strike") or 0.0)
        if not call_iv and float(atm_row.get("call_ltp", 0.0) or 0.0) > 0:
            call_iv = self._implied_iv_from_price(spot_price, strike, days_to_expiry, float(atm_row.get("call_ltp")), "call")
            source = "premium_implied_iv" if call_iv else source
        if not put_iv and float(atm_row.get("put_ltp", 0.0) or 0.0) > 0:
            put_iv = self._implied_iv_from_price(spot_price, strike, days_to_expiry, float(atm_row.get("put_ltp")), "put")
            source = "premium_implied_iv" if put_iv else source
        if call_iv is None and put_iv is not None:
            call_iv = put_iv
            source = f"{source}_put_proxy"
        elif put_iv is None and call_iv is not None:
            put_iv = call_iv
            source = f"{source}_call_proxy"
        elif call_iv is None and put_iv is None:
            source = "default_low_quality"
        return {
            "call_iv": call_iv or 0.15,
            "put_iv": put_iv or 0.15,
            "source": source,
            "quality_ok": source != "default_low_quality",
        }

    def analyze(
        self,
        instrument: str,
        timeframe: str,
        candle_df: pd.DataFrame,
        candle_1min_df: Optional[pd.DataFrame] = None, # Added 1m support
        options_chain: Optional[pd.DataFrame] = None,
        spot_price: float = 0,
        strike_interval: int = 50,
        days_to_expiry: float = 7,
        price_change_pct: float = 0,
        chain_quality: Optional[Dict] = None,
        volatility_value: Optional[float] = None,
        volatility_percentile: Optional[float] = None,
        volatility_source: str = "",
        instrument_iv_observations: Optional[List[float]] = None,
        instrument_iv_min_sessions: int = 20,
        session_context: Optional[Dict[str, Any]] = None,
        anchor_regime: Optional[Dict[str, Any]] = None,
        breadth_context: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        # Run all intelligence modules and produce aggregate score
        results = {}
        score = 0.0  # -1.0 (bearish) to +1.0 (bullish)
        components = 0
        regime_score_multiplier = 1.0

        # Use 1m data for high-resolution intelligence if available
        intel_df = candle_1min_df if candle_1min_df is not None and not candle_1min_df.empty else candle_df

        # 1. Volume
        vol = self.volume.analyze(intel_df, instrument)
        results["volume"] = vol
        profile_ctx = vol.get("market_structure_profile", {}) if isinstance(vol, dict) else {}
        aroon_ctx = vol.get("aroon", {}) if isinstance(vol, dict) else {}
        wavetrend_ctx = vol.get("wavetrend", {}) if isinstance(vol, dict) else {}
        results["market_structure"] = profile_ctx if isinstance(profile_ctx, dict) else {}
        results["aroon_context"] = aroon_ctx if isinstance(aroon_ctx, dict) else {}
        results["wavetrend"] = wavetrend_ctx if isinstance(wavetrend_ctx, dict) else {}
        if vol["signal"] == "BULLISH_VOLUME":
            score += 0.2 # Increased from 0.15
        elif vol["signal"] == "BEARISH_VOLUME":
            score -= 0.2 # Increased from 0.15
        if vol["buy_sell_ratio"] > 1.5: # Increased threshold
            score += 0.15
        elif vol["buy_sell_ratio"] < 0.6: # Lowered threshold
            score -= 0.15
        profile_signal = str(results["market_structure"].get("signal") or "")
        if profile_signal in {"ACCEPTING_HIGHER", "UPSIDE_ACCEPTANCE"}:
            score += 0.04
        elif profile_signal in {"ACCEPTING_LOWER", "DOWNSIDE_ACCEPTANCE"}:
            score -= 0.04
        elif profile_signal == "REJECTING_HIGHER":
            score -= 0.03
        elif profile_signal == "REJECTING_LOWER":
            score += 0.03

        aroon_trend = str(results["aroon_context"].get("trend") or "")
        aroon_late = bool(results["aroon_context"].get("late_trend_caution"))
        if aroon_trend == "UP" and not aroon_late:
            score += 0.03
        elif aroon_trend == "DOWN" and not aroon_late:
            score -= 0.03
        components += 1

        # 2. OI
        if options_chain is not None:
            oi_result = self.oi.analyze(options_chain, price_change_pct, spot_price, strike_interval)
            if not isinstance(oi_result, dict): oi_result = {}
            results["oi"] = oi_result
            

            if oi_result["signal"] == "BULLISH":
                score += 0.2
            elif oi_result["signal"] == "BEARISH":
                score -= 0.2
            components += 1

            # 3. PCR
            pcr_result = self.pcr.analyze(options_chain, instrument, spot_price, strike_interval)
            if not isinstance(pcr_result, dict): pcr_result = {}
            results["pcr"] = pcr_result
            signal_map = {"STRONG_BULLISH": 0.2, "BULLISH": 0.1, "NEUTRAL": 0, "BEARISH": -0.1, "STRONG_BEARISH": -0.2}
            score += signal_map.get(pcr_result.get("signal", "NEUTRAL"), 0)
            components += 1

        # 4. Greeks & Gamma Awareness
        if options_chain is not None and spot_price > 0:
            iv_ctx = self._chain_iv_context(options_chain, spot_price, days_to_expiry)
            greeks_atm = self.greeks.analyze_atm(
                spot_price,
                strike_interval,
                days_to_expiry,
                iv_call=iv_ctx["call_iv"],
                iv_put=iv_ctx["put_iv"],
            )
            greeks_chain = self.greeks.analyze_chain(
                spot_price,
                strike_interval,
                days_to_expiry,
                iv_call=iv_ctx["call_iv"],
                iv_put=iv_ctx["put_iv"],
                iv_source=iv_ctx["source"],
            )
            # ── Volatility percentile for routing ──
            # Preference order: (1) instrument-specific option IV-Rank computed
            # from this instrument's own prior-session ATM IV, (2) India VIX
            # regime percentile as a broad fallback, (3) neutral 50. The caller
            # supplies only prior-session IV observations, so this stays
            # look-ahead-safe in historical routing.
            iv_ctx_source = str(iv_ctx.get("source", ""))
            iv_is_real = not iv_ctx_source.startswith("default")
            current_atm_iv = None
            if iv_is_real:
                current_atm_iv = round(
                    ((float(iv_ctx["call_iv"]) + float(iv_ctx["put_iv"])) / 2.0) * 100.0, 2
                )

            iv_obs: List[float] = []
            for raw_iv in (instrument_iv_observations or []):
                try:
                    parsed_iv = float(raw_iv)
                except (TypeError, ValueError):
                    continue
                if parsed_iv > 0:
                    iv_obs.append(parsed_iv)

            vix_available = volatility_percentile is not None
            vix_percentile = (
                max(0.0, min(100.0, float(volatility_percentile))) if vix_available else 50.0
            )
            vix_source = (
                str(volatility_source or "india_vix_percentile")
                if vix_available
                else "unavailable_neutral"
            )

            min_iv_sessions = max(2, int(instrument_iv_min_sessions or 20))
            if current_atm_iv is not None and len(iv_obs) >= min_iv_sessions:
                rank = sum(1 for obs in iv_obs if obs <= current_atm_iv) / len(iv_obs) * 100.0
                iv_percentile = round(max(0.0, min(100.0, rank)), 1)
                iv_basis = "instrument_iv_rank"
            elif vix_available:
                iv_percentile = round(vix_percentile, 1)
                iv_basis = "india_vix_percentile"
            else:
                iv_percentile = 50.0
                iv_basis = "unavailable_neutral"

            # ``percentile_available``/``percentile_source`` keep their original
            # meaning (India VIX availability) for existing dashboard consumers.
            percentile_available = vix_available
            percentile_source = vix_source

            # ── Institutional Edge: Proximity to major OI levels ──
            oi_data = results.get("oi", {})
            res_level = oi_data.get("resistance_level", 0)
            sup_level = oi_data.get("support_level", 0)
            
            dist_res = 999.0
            dist_sup = 999.0
            
            # Normalize threshold so that 0.25% at 24000 NIFTY (~60 pts) translates
            # proportionally to other instruments (e.g. ~0.50% at 12000 MIDCPNIFTY).
            dynamic_threshold = 0.25 * (24000.0 / max(1.0, spot_price))
            
            if res_level > 0:
                dist_res = (res_level - spot_price) / spot_price * 100
                if 0 < dist_res < dynamic_threshold: # Approaching major resistance wall
                    score -= 0.2 # Penalize buying into a wall
            
            if sup_level > 0:
                dist_sup = (spot_price - sup_level) / spot_price * 100
                if 0 < dist_sup < dynamic_threshold: # Approaching major support floor
                    score += 0.2 # Boost buying from a floor

            results["entry_quality"] = "EXCELLENT" if (dist_sup < dynamic_threshold or dist_res < dynamic_threshold) else "GOOD"
            results["greeks"] = {
                **greeks_atm,
                "iv_percentile": round(iv_percentile, 1),
                "iv_percentile_basis": iv_basis,
                "instrument_iv": current_atm_iv,
                "instrument_iv_sessions": len(iv_obs),
                "volatility_percentile": round(vix_percentile, 1),
                "volatility_percentile_available": percentile_available,
                "volatility_percentile_source": percentile_source,
                "volatility_value": (
                    round(float(volatility_value), 3)
                    if volatility_value is not None
                    else None
                ),
                "chain": greeks_chain["strikes"],
                "iv_source": iv_ctx["source"],
            }
            components += 1
        elif options_chain is None and spot_price > 0:
            # Fallback for when options chain is missing but we have price
            results["oi"] = self.oi._empty_result()
            results["pcr"] = self.pcr._empty_result()
            results["greeks"] = {
                "is_fallback": True,
                "msg": "Options data unavailable, using price-only fallback"
            }
        else:
            results["oi"] = self.oi._empty_result()
            results["pcr"] = self.pcr._empty_result()
            results["greeks"] = {}

        # 5. Regime
        try:
            regime_result = self.regime.detect(
                candle_df,
                instrument,
                timeframe,
                session_context=session_context,
                breadth_context=breadth_context,
            )
        except TypeError as exc:
            if "breadth_context" not in str(exc):
                raise
            regime_result = self.regime.detect(
                candle_df,
                instrument,
                timeframe,
                session_context=session_context,
            )
        results["regime"] = regime_result
        if regime_result["regime"] in ["TRENDING_UP", "BREAKOUT_UP"]:
            score += 0.25 # Increased from 0.15
        elif regime_result["regime"] in ["TRENDING_DOWN", "BREAKOUT_DOWN"]:
            score -= 0.25 # Increased from 0.15
        elif regime_result["regime"] in ["VOLATILE", "RANGING", "UNKNOWN"]:
            regime_score_multiplier = 0.15  # Institutional Dampening (was 0.3)
        components += 1

        # 6. Order Flow
        flow_result = self.order_flow.analyze(
            intel_df,
            options_chain=options_chain,
            spot_price=spot_price,
            strike_interval=strike_interval,
            market_context=chain_quality,
        )
        results["order_flow"] = flow_result
        results["candle_pressure"] = {**flow_result, "source": "ohlcv_close_position"}
        if flow_result["signal"] == "BULLISH":
            score += 0.2 # Increased from 0.15
        elif flow_result["signal"] == "BEARISH":
            score -= 0.2 # Increased from 0.15
        components += 1

        # ═══ STRESS TEST: Manipulation & Divergence Detection ═══
        # Outsmarting "Stop-loss Hunting" and "False Breakouts"
        divergence_detected = False
        # Case A: Price Up but Order Flow Bearish (Distribution Trap)
        if price_change_pct > 0.1 and flow_result["ratio"] < 0.8:
            score -= 0.3
            divergence_detected = True
            self._warn_divergence_once(
                instrument,
                "distribution_trap",
                f"⚠️ DIVERGENCE: {instrument} Price rising but Order Flow shows Distribution (Trap?)",
            )
        # Case B: Price Down but Order Flow Bullish (Accumulation Trap)
        elif price_change_pct < -0.1 and flow_result["ratio"] > 1.25:
            score += 0.3
            divergence_detected = True
            self._warn_divergence_once(
                instrument,
                "accumulation_shakeout",
                f"⚠️ DIVERGENCE: {instrument} Price falling but Order Flow shows Accumulation (Shakeout?)",
            )
        
        results["divergence_alert"] = divergence_detected

        # ═══ STRESS TEST: System CA OI (Cumulative Analysis) ═══
        if options_chain is not None and not options_chain.empty:
            # Analyze distribution of OI across entire chain (Wide Format Support)
            if 'call_oi' in options_chain.columns:
                total_call_oi = options_chain['call_oi'].sum()
                total_put_oi = options_chain['put_oi'].sum()
            else:
                total_call_oi = options_chain[options_chain['type'] == 'CE']['oi'].sum()
                total_put_oi = options_chain[options_chain['type'] == 'PE']['oi'].sum()
                
            oi_imbalance = (total_call_oi - total_put_oi) / (total_call_oi + total_put_oi) if (total_call_oi + total_put_oi) > 0 else 0
            
            # Calculate nearby strikes (+/- 5) for strike-specific analysis
            nearby_strikes = []
            if spot_price > 0:
                atm = round(spot_price / strike_interval) * strike_interval
                min_strike = atm - (5 * strike_interval)
                max_strike = atm + (5 * strike_interval)
                
                # Filter for wide format
                if 'strike' in options_chain.columns:
                    nearby_chain = options_chain[(options_chain['strike'] >= min_strike) & (options_chain['strike'] <= max_strike)]
                    nearby_strikes = nearby_chain[['strike', 'call_oi', 'put_oi', 'call_volume', 'put_volume']].to_dict('records')
            
            results["oi"]["cumulative_analysis"] = {
                "total_call_oi": int(total_call_oi),
                "total_put_oi": int(total_put_oi),
                "imbalance": round(oi_imbalance, 3),
                "sentiment": "BEARISH_PRESSURE" if oi_imbalance > 0.1 else ("BULLISH_PRESSURE" if oi_imbalance < -0.1 else "BALANCED"),
                "nearby_strikes": nearby_strikes
            }

            # ═══ MAX PAIN ═══ the expiry strike that minimises total option-buyer
            # payout (an expiry magnet for index options). Pure-pandas, wide format.
            try:
                if 'call_oi' in options_chain.columns and 'strike' in options_chain.columns and spot_price > 0:
                    oc = options_chain.dropna(subset=['strike'])
                    strikes = oc['strike'].astype(float)
                    coi = oc['call_oi'].astype(float).fillna(0.0)
                    poi = oc['put_oi'].astype(float).fillna(0.0)
                    best_k, best_pain = None, None
                    for P in strikes:
                        pain = (coi * (P - strikes).clip(lower=0)).sum() + (poi * (strikes - P).clip(lower=0)).sum()
                        if best_pain is None or pain < best_pain:
                            best_pain, best_k = pain, float(P)
                    if best_k:
                        results["max_pain"] = {
                            "strike": best_k,
                            "distance_pct": round((float(spot_price) - best_k) / best_k * 100, 2),
                        }
            except Exception:
                pass

        top_down = self._top_down_context_adjustment(
            session_context=session_context,
            anchor_regime=anchor_regime,
            breadth_context=breadth_context,
        )
        if top_down["has_context"]:
            score += float(top_down["score_adjustment"])
            components += 1
            multiplier_cap = float(top_down.get("regime_multiplier_cap") or 1.0)
            if multiplier_cap < regime_score_multiplier:
                regime_score_multiplier = multiplier_cap

        results["session_context"] = session_context or {}
        results["anchor_regime"] = anchor_regime or {}
        results["breadth_context"] = breadth_context or {}
        results["top_down_context"] = top_down

        if chain_quality is None:
            strike_count = int(len(options_chain)) if options_chain is not None else 0
            chain_quality = {
                "source": "unknown" if strike_count else "none",
                "strike_count": strike_count,
                "score": 95 if strike_count >= 10 else (35 if strike_count == 1 else 0),
                "fallback": strike_count <= 1,
                "age_seconds": 0,
            }
        results["data_quality"] = chain_quality

        # Final Score Aggregation (Scaled to -100 to +100 for SignalManager)
        score_before_regime_dampening = score
        score *= regime_score_multiplier
        final_score = (score / components) * 100 if components > 0 else 0

        # Normalize score to [-1, 1] for legacy logic
        score = max(-1.0, min(1.0, score))

        # Final signal
        if score > 0.3:
            final_signal = "STRONG_BUY" if score > 0.6 else "BUY"
        elif score < -0.3:
            final_signal = "STRONG_SELL" if score < -0.6 else "SELL"
        else:
            final_signal = "HOLD"

        # Safely extract values for final results dict
        vol_data = results.get("volume", {})
        if not isinstance(vol_data, dict): vol_data = {}
        
        flow_data = results.get("order_flow", {})
        if not isinstance(flow_data, dict): flow_data = {}
        
        regime_data = results.get("regime", {})
        if not isinstance(regime_data, dict): regime_data = {}

        results["aggregate"] = {
            "score": round(final_score, 1),
            "signal": final_signal,
            "components_analyzed": components,
            "instrument": instrument,
            "timeframe": timeframe,
            "futures_volume": vol_data.get("current_volume", 0),
            "order_ratio": flow_data.get("ratio", 1.0),
            "regime": regime_data.get("regime", "RANGING"),
            "regime_conviction_label": regime_data.get("conviction_label", ""),
            "regime_conviction_summary": regime_data.get("conviction_summary", ""),
            "regime_conviction_vector": dict(regime_data.get("conviction_vector", {}) or {}),
            "score_before_regime_dampening": round(score_before_regime_dampening, 4),
            "regime_score_multiplier": regime_score_multiplier,
            "day_type": (session_context or {}).get("day_type", ""),
            "session_bias": (session_context or {}).get("bias", ""),
            "effective_day_type": (session_context or {}).get("effective_day_type", ""),
            "effective_bias": (session_context or {}).get("effective_bias", ""),
            "phase_confidence": (session_context or {}).get("phase_confidence", 0.0),
            "mood_horizons": (session_context or {}).get("mood_horizons", {}),
            "hourly_anchor": (anchor_regime or {}).get("regime", ""),
            "breadth_bias": (breadth_context or {}).get("bias", ""),
            "top_down_adjustment": round(float(top_down.get("score_adjustment") or 0.0), 4),
            "market_structure_signal": results["market_structure"].get("signal", ""),
            "market_structure_state": results["market_structure"].get("state", ""),
            "aroon_trend": results["aroon_context"].get("trend", ""),
            "aroon_freshness": results["aroon_context"].get("freshness", ""),
            "wavetrend_exhaustion": results["wavetrend"].get("exhaustion", ""),
        }

        return results

    def _top_down_context_adjustment(
        self,
        *,
        session_context: Optional[Dict[str, Any]],
        anchor_regime: Optional[Dict[str, Any]],
        breadth_context: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        adjustment = 0.0
        reasons: List[str] = []
        multiplier_cap = 1.0

        session_context = session_context or {}
        anchor_regime = anchor_regime or {}
        breadth_context = breadth_context or {}

        day_type = str(session_context.get("day_type") or "").upper()
        day_conf = self._bounded_float(session_context.get("confidence"), 0.0, 1.0)
        if day_conf >= 0.45:
            if day_type == "TREND_DAY_UP":
                delta = 0.12 * day_conf
                adjustment += delta
                reasons.append(f"session trend-up prior {delta:+.3f}")
            elif day_type == "TREND_DAY_DOWN":
                delta = -0.12 * day_conf
                adjustment += delta
                reasons.append(f"session trend-down prior {delta:+.3f}")
            elif day_type == "RANGE_DAY":
                multiplier_cap = min(multiplier_cap, 0.85)
                reasons.append("range-day dampens directional conviction")

        delta_ctx = session_context.get("session_delta", {})
        if isinstance(delta_ctx, dict):
            delta_bias = str(delta_ctx.get("bias") or "").upper()
            delta_ratio = abs(self._bounded_float(delta_ctx.get("delta_ratio"), -1.0, 1.0))
            if delta_bias == "BULLISH" and delta_ratio >= 0.15:
                delta = min(0.06, delta_ratio * 0.12)
                adjustment += delta
                reasons.append(f"session cumulative delta bullish {delta:+.3f}")
            elif delta_bias == "BEARISH" and delta_ratio >= 0.15:
                delta = -min(0.06, delta_ratio * 0.12)
                adjustment += delta
                reasons.append(f"session cumulative delta bearish {delta:+.3f}")

        vix_change = self._bounded_float(session_context.get("vix_change_pct"), -20.0, 20.0)
        session_bias = str(session_context.get("bias") or "").upper()
        if abs(vix_change) >= 2.0:
            if vix_change > 0 and session_bias == "BULLISH":
                adjustment -= 0.04
                reasons.append("VIX rising against bullish session")
            elif vix_change > 0 and session_bias == "BEARISH":
                adjustment -= 0.04
                reasons.append("VIX rising confirms bearish session")
            elif vix_change < 0 and session_bias == "BULLISH":
                adjustment += 0.04
                reasons.append("VIX falling confirms bullish session")
            elif vix_change < 0 and session_bias == "BEARISH":
                adjustment += 0.04
                reasons.append("VIX falling softens bearish session")

        anchor_name = str(anchor_regime.get("regime") or "").upper()
        anchor_ready = bool(anchor_regime.get("ready_for_grading", True))
        anchor_conf = self._bounded_float(anchor_regime.get("confidence"), 0.0, 100.0) / 100.0
        if anchor_ready and anchor_conf >= 0.45:
            if anchor_name in {"TRENDING_UP", "BREAKOUT_UP"}:
                delta = 0.10 * anchor_conf
                adjustment += delta
                reasons.append(f"60min anchor up {delta:+.3f}")
            elif anchor_name in {"TRENDING_DOWN", "BREAKOUT_DOWN"}:
                delta = -0.10 * anchor_conf
                adjustment += delta
                reasons.append(f"60min anchor down {delta:+.3f}")

        breadth_bias = str(breadth_context.get("bias") or "").upper()
        breadth_votes = int(self._bounded_float(breadth_context.get("aligned_count"), 0.0, 4.0))
        if breadth_votes >= 3:
            if breadth_bias == "BULLISH":
                adjustment += 0.08
                reasons.append("cross-index breadth bullish")
            elif breadth_bias == "BEARISH":
                adjustment -= 0.08
                reasons.append("cross-index breadth bearish")

        adjustment = max(-0.35, min(0.35, adjustment))
        return {
            "has_context": bool(reasons),
            "score_adjustment": round(adjustment, 4),
            "regime_multiplier_cap": round(multiplier_cap, 3),
            "reasons": reasons,
        }

    @staticmethod
    def _bounded_float(value: Any, low: float, high: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = 0.0
        return max(low, min(high, parsed))
