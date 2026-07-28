"""
OI Tracker — Open Interest Analysis
═══════════════════════════════════════════════════════════════

Options OI pressure model:
- Put OI addition / Call covering contributes bullish support.
- Call OI addition / Put unwinding contributes bearish resistance.
- Call and Put legs stay separate; futures-style aggregate OI/price labels
  are not valid directional evidence for an option chain.
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional, List
from loguru import logger


class OITracker:
    """Tracks and analyzes Open Interest changes"""

    def __init__(self):
        self._oi_history: Dict[str, List[Dict]] = {}

    def analyze(
        self,
        options_chain: pd.DataFrame,
        price_change: float = 0.0,
        spot_price: Optional[float] = None,
        strike_interval: int = 50,
        near_strikes: int = 5,
    ) -> Dict:
        """
        Analyze OI changes with price movement.

        Args:
            options_chain: DataFrame with columns [strike, call_oi, call_oi_change,
                          put_oi, put_oi_change, call_volume, put_volume, ...]
            price_change: Price change percentage of the underlying

        Returns:
            OI analysis dictionary
        """
        if options_chain is None or len(options_chain) == 0:
            return self._empty_result()

        try:
            analysis_chain = options_chain.copy()
            analysis_basis = "FULL_CHAIN"
            broad_call_oi = pd.to_numeric(
                options_chain.get("call_oi", pd.Series(0, index=options_chain.index)),
                errors="coerce",
            ).fillna(0)
            broad_put_oi = pd.to_numeric(
                options_chain.get("put_oi", pd.Series(0, index=options_chain.index)),
                errors="coerce",
            ).fillna(0)
            broad_real_pair_count = int(
                ((broad_call_oi > 0) & (broad_put_oi > 0)).sum()
            )
            near_real_pair_count = 0
            if spot_price and spot_price > 0 and "strike" in options_chain.columns:
                interval = max(float(strike_interval or 0), 1.0)
                atm = round(float(spot_price) / interval) * interval
                width = max(int(near_strikes or 0), 0) * interval
                strike_values = pd.to_numeric(options_chain["strike"], errors="coerce")
                near_chain = options_chain.loc[
                    strike_values.between(atm - width, atm + width, inclusive="both")
                ].copy()
                near_call_oi = pd.to_numeric(
                    near_chain.get("call_oi", pd.Series(0, index=near_chain.index)),
                    errors="coerce",
                ).fillna(0)
                near_put_oi = pd.to_numeric(
                    near_chain.get("put_oi", pd.Series(0, index=near_chain.index)),
                    errors="coerce",
                ).fillna(0)
                near_real_pair_count = int(
                    ((near_call_oi > 0) & (near_put_oi > 0)).sum()
                )
                if near_real_pair_count >= 3:
                    analysis_chain = near_chain
                    analysis_basis = "NEAR_MONEY"

            if "strike" in analysis_chain.columns:
                analysis_chain = analysis_chain.assign(
                    strike=pd.to_numeric(
                        analysis_chain["strike"], errors="coerce"
                    ).fillna(0)
                )
            for column in ("call_oi", "put_oi", "call_oi_change", "put_oi_change"):
                if column not in analysis_chain.columns:
                    analysis_chain = analysis_chain.assign(**{column: 0})
                else:
                    analysis_chain = analysis_chain.assign(
                        **{
                            column: pd.to_numeric(
                                analysis_chain[column], errors="coerce"
                            ).fillna(0)
                        }
                    )
            total_call_oi = int(analysis_chain['call_oi'].sum())
            total_put_oi = int(analysis_chain['put_oi'].sum())
            total_call_oi_change = int(analysis_chain['call_oi_change'].sum())
            total_put_oi_change = int(analysis_chain['put_oi_change'].sum())
            net_oi_change = total_put_oi_change - total_call_oi_change
            absolute_oi_change = int(
                analysis_chain["call_oi_change"].abs().sum()
                + analysis_chain["put_oi_change"].abs().sum()
            )
            analysis_real_pair_count = int(
                (
                    (analysis_chain["call_oi"] > 0)
                    & (analysis_chain["put_oi"] > 0)
                ).sum()
            )
            oi_change_source = "unknown"
            if "oi_change_source" in analysis_chain.columns:
                sources = [
                    str(value)
                    for value in analysis_chain["oi_change_source"].dropna().unique()
                    if str(value)
                ]
                if sources:
                    oi_change_source = ",".join(sorted(sources))

            oi_available = bool(
                analysis_real_pair_count >= 3
                and total_call_oi > 0
                and total_put_oi > 0
            )
            # Options-specific pressure model. Positive put additions and call
            # covering strengthen support (bullish); positive call additions and
            # put unwinding strengthen resistance (bearish). This is explicitly a
            # writer/support-resistance heuristic—not the invalid futures-style
            # price×aggregate-OI classification previously used here.
            bullish_pressure = max(total_put_oi_change, 0) + max(-total_call_oi_change, 0)
            bearish_pressure = max(total_call_oi_change, 0) + max(-total_put_oi_change, 0)
            pressure_total = bullish_pressure + bearish_pressure
            pressure_imbalance = (
                (bullish_pressure - bearish_pressure) / pressure_total
                if pressure_total > 0
                else 0.0
            )
            if absolute_oi_change == 0:
                activity = "NO_CONFIRMED_OI_CHANGE"
                signal = "NEUTRAL"
                interpretation = (
                    "OI-change unavailable, warming up, or unchanged; "
                    "directional OI inference withheld"
                )
            elif pressure_imbalance >= 0.12:
                activity = "PUT_SUPPORT_OR_CALL_COVERING"
                signal = "BULLISH"
                interpretation = (
                    "Bullish options pressure: put support addition and/or call covering "
                    f"outpaced bearish pressure ({pressure_imbalance:+.2f})"
                )
            elif pressure_imbalance <= -0.12:
                activity = "CALL_RESISTANCE_OR_PUT_UNWINDING"
                signal = "BEARISH"
                interpretation = (
                    "Bearish options pressure: call resistance addition and/or put unwinding "
                    f"outpaced bullish pressure ({pressure_imbalance:+.2f})"
                )
            else:
                activity = "BALANCED_OPTIONS_OI_ACTIVITY"
                signal = "NEUTRAL"
                interpretation = "Call/put OI pressure is balanced; directional inference withheld"

            if not oi_available:
                signal = "NEUTRAL"
                interpretation = "Call/put OI breadth unavailable; directional inference withheld"

            # ── Key Strikes (max OI) ──
            score = int(round(50.0 + (pressure_imbalance * 25.0))) if oi_available else 50
            score = max(0, min(100, score))
            if signal != "NEUTRAL" and abs(price_change) < 0.05:
                score = 55 if signal == "BULLISH" else 45

            max_call_oi_strike = int(analysis_chain.loc[analysis_chain['call_oi'].idxmax(), 'strike'])
            max_put_oi_strike = int(analysis_chain.loc[analysis_chain['put_oi'].idxmax(), 'strike'])

            # ── Top OI Change Strikes ──
            top_call_buildup = analysis_chain.nlargest(3, 'call_oi_change')[
                ['strike', 'call_oi_change']
            ].to_dict('records')
            top_put_buildup = analysis_chain.nlargest(3, 'put_oi_change')[
                ['strike', 'put_oi_change']
            ].to_dict('records')

            return {
                "available": oi_available,
                "status": "OK" if oi_available else "UNAVAILABLE",
                "reason": "" if oi_available else "Need at least 3 strikes with positive call and put OI",
                "total_call_oi": total_call_oi,
                "total_put_oi": total_put_oi,
                "total_call_oi_change": total_call_oi_change,
                "total_put_oi_change": total_put_oi_change,
                "net_oi_change": net_oi_change,
                "absolute_oi_change": absolute_oi_change,
                "bullish_pressure": int(bullish_pressure),
                "bearish_pressure": int(bearish_pressure),
                "pressure_imbalance": round(float(pressure_imbalance), 3),
                "directional_model": "CALL_PUT_PRESSURE_V1",
                "oi_change_source": oi_change_source,
                "activity": activity,
                "signal": signal,
                "score": score,
                "interpretation": interpretation,
                "max_call_oi_strike": max_call_oi_strike,
                "max_put_oi_strike": max_put_oi_strike,
                "resistance_level": max_call_oi_strike,
                "support_level": max_put_oi_strike,
                "top_call_buildup": top_call_buildup,
                "top_put_buildup": top_put_buildup,
                "analysis_basis": analysis_basis,
                "analysis_strike_count": int(len(analysis_chain)),
                "analysis_real_oi_pair_count": analysis_real_pair_count,
                "near_real_oi_pair_count": near_real_pair_count,
                "broad_real_oi_pair_count": broad_real_pair_count,
            }

        except Exception as e:
            logger.error(f"OI analysis error: {e}")
            return self._empty_result()

    def _empty_result(self) -> Dict:
        return {
            "available": False, "status": "UNAVAILABLE", "reason": "Option chain unavailable",
            "total_call_oi": 0, "total_put_oi": 0,
            "total_call_oi_change": 0, "total_put_oi_change": 0,
            "net_oi_change": 0, "absolute_oi_change": 0,
            "bullish_pressure": 0, "bearish_pressure": 0,
            "pressure_imbalance": 0.0, "directional_model": "CALL_PUT_PRESSURE_V1",
            "oi_change_source": "none",
            "activity": "UNKNOWN", "signal": "NEUTRAL", "score": 50,
            "interpretation": "No data", "max_call_oi_strike": 0,
            "max_put_oi_strike": 0, "resistance_level": 0, "support_level": 0,
            "top_call_buildup": [], "top_put_buildup": [],
            "analysis_basis": "NONE", "analysis_strike_count": 0,
            "analysis_real_oi_pair_count": 0, "near_real_oi_pair_count": 0,
            "broad_real_oi_pair_count": 0,
        }
