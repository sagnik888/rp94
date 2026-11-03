"""
Greeks Engine — Options Greeks Calculator (Black-Scholes)
"""
import math
from typing import Dict

import numpy as np
from loguru import logger

_SQRT2 = math.sqrt(2.0)
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


def _ncdf(x: float) -> float:
    # Standard normal CDF for one scalar. scipy.stats.norm.cdf on a scalar costs tens of
    # microseconds of Python overhead per call; the implied-IV solver calls it ~160 times
    # per index per analysis and it was ~20% of all intelligence CPU (2026-09-25).
    return 0.5 * math.erfc(-x / _SQRT2)


def _npdf(x: float) -> float:
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


class GreeksEngine:
    def __init__(self, risk_free_rate: float = 0.065):
        self.r = risk_free_rate

    def calculate(self, spot: float, strike: float, days: float, iv: float, opt_type: str = "call") -> Dict:
        try:
            if spot <= 0 or strike <= 0 or iv <= 0:
                return self._zero()
            spot, strike, iv = float(spot), float(strike), float(iv)
            raw_days = float(days or 0.0)
            effective_days = max(raw_days, 0.5)
            T = effective_days / 365.0
            sqrt_t = math.sqrt(T)
            d1 = (math.log(spot / strike) + (self.r + 0.5 * iv**2) * T) / (iv * sqrt_t)
            d2 = d1 - iv * sqrt_t
            pdf_d1 = _npdf(d1)
            discount = math.exp(-self.r * T)
            if opt_type.lower() == "call":
                delta = _ncdf(d1)
                theta = (-(spot * pdf_d1 * iv) / (2 * sqrt_t) - self.r * strike * discount * _ncdf(d2))
                price = spot * _ncdf(d1) - strike * discount * _ncdf(d2)
            else:
                delta = _ncdf(d1) - 1
                theta = (-(spot * pdf_d1 * iv) / (2 * sqrt_t) + self.r * strike * discount * _ncdf(-d2))
                price = strike * discount * _ncdf(-d2) - spot * _ncdf(-d1)
            gamma = pdf_d1 / (spot * iv * sqrt_t)
            vega = spot * pdf_d1 * sqrt_t / 100
            mn = "ATM" if abs(spot - strike) / spot < 0.005 else ("ITM" if (spot > strike if opt_type == "call" else spot < strike) else "OTM")
            return {"delta": round(float(delta), 4), "gamma": round(float(gamma), 6), "theta": round(float(theta / 365), 2), "vega": round(float(vega), 2), "price": round(float(max(0, price)), 2), "iv": round(iv * 100, 1), "moneyness": mn, "days_to_expiry": days, "effective_days_to_expiry": round(effective_days, 4)}
        except Exception as e:
            logger.error(f"Greeks error: {e}")
            return self._zero()

    def analyze_atm(self, spot: float, strike_interval: int, days: float, iv_call: float = 0.15, iv_put: float = 0.15) -> Dict:
        atm = round(spot / strike_interval) * strike_interval
        c = self.calculate(spot, atm, days, iv_call, "call")
        p = self.calculate(spot, atm, days, iv_put, "put")
        tw = "⚠️ EXPIRY DAY!" if days <= 1 else ("⚠️ High theta near expiry" if days <= 2 else "")
        return {"atm_strike": atm, "spot": spot, "days_to_expiry": days, "call": c, "put": p, "net_delta": round(c["delta"] + p["delta"], 4), "total_gamma": round(c["gamma"] + p["gamma"], 6), "total_theta": round(c["theta"] + p["theta"], 2), "total_vega": round(c["vega"] + p["vega"], 2), "straddle_price": round(c["price"] + p["price"], 2), "theta_warning": tw}

    def analyze_chain(
        self,
        spot: float,
        strike_interval: int,
        days: float,
        num_strikes: int = 5,
        iv_call: float = 0.15,
        iv_put: float = 0.15,
        iv_source: str = "default",
    ) -> Dict:
        """Analyze a range of strikes around ATM for Heatmap"""
        atm = round(spot / strike_interval) * strike_interval
        results = {"strikes": []}
        
        # Scan range: ATM +/- num_strikes
        start_strike = atm - (num_strikes * strike_interval)
        for i in range(num_strikes * 2 + 1):
            strike = start_strike + (i * strike_interval)
            c = self.calculate(spot, strike, days, iv_call, "call")
            p = self.calculate(spot, strike, days, iv_put, "put")
            
            results["strikes"].append({
                "strike": strike,
                "call_delta": c["delta"],
                "call_gamma": c["gamma"],
                "call_theta": c["theta"],
                "put_delta": p["delta"],
                "put_gamma": p["gamma"],
                "put_theta": p["theta"],
                "call_iv": c["iv"],
                "put_iv": p["iv"],
                "iv_source": iv_source,
                "is_atm": strike == atm
            })
        return results

    def _zero(self) -> Dict:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "price": 0.0, "iv": 0.0, "moneyness": "UNKNOWN", "days_to_expiry": 0, "effective_days_to_expiry": 0.0}
