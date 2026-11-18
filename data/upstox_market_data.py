"""Upstox Market Data Adapter for UT1.

Provides real-time index quotes, option chain with live Greeks,
historical candles, and failure tracking for the 30-second Angel One fallback.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from loguru import logger

from data.upstox_auth import get_upstox_token

# Index key mappings in Upstox
UPSTOX_INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
    "SENSEX": "BSE_INDEX|SENSEX",
}

# Reverse mapping
UPSTOX_KEY_TO_INDEX = {v: k for k, v in UPSTOX_INDEX_KEYS.items()}

UPSTOX_BASE_URL = "https://api.upstox.com"
UPSTOX_LTP_URL = f"{UPSTOX_BASE_URL}/v2/market-quote/ltp"
UPSTOX_QUOTES_URL = f"{UPSTOX_BASE_URL}/v2/market-quote/quotes"
UPSTOX_OPTION_CONTRACT_URL = f"{UPSTOX_BASE_URL}/v2/option/contract"
UPSTOX_OPTION_CHAIN_URL = f"{UPSTOX_BASE_URL}/v2/option/chain"
UPSTOX_INTRADAY_CANDLE_URL = f"{UPSTOX_BASE_URL}/v3/historical-candle/intraday/{{key}}/{{unit}}/{{interval}}"
UPSTOX_HIST_CANDLE_URL = f"{UPSTOX_BASE_URL}/v3/historical-candle/{{key}}/{{unit}}/{{interval}}/{{to}}/{{frm}}"


class UpstoxMarketDataClient:
    """Primary market data client using Upstox API v2 / v3."""

    def __init__(self, timeout: float = 7.0):
        self.timeout = timeout
        self._lock = threading.RLock()
        self._last_success_time = time.monotonic()
        self._last_error_time: Optional[float] = None
        self._consecutive_failures = 0
        self._last_error_reason = ""
        self._contracts_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._chain_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._quote_cache: Dict[str, Dict[str, Any]] = {}

    def _headers(self) -> Dict[str, str]:
        token = get_upstox_token()
        if not token:
            raise ValueError("No Upstox token available in upstox_token.json")
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def _record_success(self) -> None:
        with self._lock:
            self._last_success_time = time.monotonic()
            self._consecutive_failures = 0
            self._last_error_time = None
            self._last_error_reason = ""

    def _record_failure(self, error: Exception | str) -> None:
        with self._lock:
            now = time.monotonic()
            if self._last_error_time is None:
                self._last_error_time = now
            self._consecutive_failures += 1
            self._last_error_reason = str(error)

    def is_healthy(self, threshold_seconds: float = 30.0) -> bool:
        """Return True if Upstox has not been continuously failing for > threshold_seconds."""
        with self._lock:
            if self._last_error_time is None:
                return True
            failure_duration = time.monotonic() - self._last_error_time
            return failure_duration < threshold_seconds

    def failure_duration(self) -> float:
        with self._lock:
            if self._last_error_time is None:
                return 0.0
            return max(0.0, time.monotonic() - self._last_error_time)

    def get_index_ltp(self, index_name: str) -> Optional[float]:
        """Fetch spot price for NIFTY, BANKNIFTY, MIDCPNIFTY, or SENSEX."""
        key = UPSTOX_INDEX_KEYS.get(index_name.upper())
        if not key:
            return None

        url = f"{UPSTOX_LTP_URL}?instrument_key={urllib.parse.quote(key)}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                for k, v in data.items():
                    price = v.get("last_price")
                    if price is not None:
                        self._record_success()
                        return float(price)
            self._record_failure(f"HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            self._record_failure(e)
            logger.warning(f"Upstox index LTP fetch failed for {index_name}: {e}")
        return None

    def get_multi_index_quotes(self, indices: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        """Fetch quotes for all or selected indices simultaneously."""
        targets = indices or list(UPSTOX_INDEX_KEYS.keys())
        keys = [UPSTOX_INDEX_KEYS[idx.upper()] for idx in targets if idx.upper() in UPSTOX_INDEX_KEYS]
        if not keys:
            return {}

        url = f"{UPSTOX_QUOTES_URL}?instrument_key={','.join(keys)}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                payload = resp.json().get("data", {})
                result = {}
                for instrument_key, quote in payload.items():
                    norm_k = instrument_key.replace(':', '|').upper()
                    for idx_name, idx_key in UPSTOX_INDEX_KEYS.items():
                        if idx_key.upper() in norm_k or norm_k in idx_key.upper():
                            ohlc = quote.get("ohlc", {})
                            result[idx_name] = {
                                "symbol": idx_name,
                                "instrument_key": idx_key,
                                "ltp": float(quote.get("last_price") or ohlc.get("close") or 0.0),
                                "open": float(ohlc.get("open") or 0.0),
                                "high": float(ohlc.get("high") or 0.0),
                                "low": float(ohlc.get("low") or 0.0),
                                "close": float(ohlc.get("close") or 0.0),
                                "volume": float(quote.get("volume") or 0.0),
                                "timestamp": time.time(),
                                "source": "upstox",
                            }
                            self._quote_cache[idx_name] = result[idx_name]
                if result:
                    self._record_success()
                    return result
            self._record_failure(f"HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            self._record_failure(e)
            logger.warning(f"Upstox multi-index quotes failed: {e}")

        # Return cached if within 5 seconds
        return {k: v for k, v in self._quote_cache.items() if time.time() - v.get("timestamp", 0) < 5.0}

    def get_option_contracts(self, index_name: str) -> List[Dict[str, Any]]:
        """Fetch and cache available option contracts for an index."""
        key = UPSTOX_INDEX_KEYS.get(index_name.upper())
        if not key:
            return []

        now = time.monotonic()
        with self._lock:
            cached = self._contracts_cache.get(index_name)
            if cached and (now - cached[0] < 300.0): # 5-minute cache
                return cached[1]

        url = f"{UPSTOX_OPTION_CONTRACT_URL}?instrument_key={urllib.parse.quote(key)}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                contracts = resp.json().get("data", [])
                with self._lock:
                    self._contracts_cache[index_name] = (now, contracts)
                self._record_success()
                return contracts
            self._record_failure(f"HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            self._record_failure(e)
            logger.warning(f"Upstox option contracts fetch failed for {index_name}: {e}")

        cached = self._contracts_cache.get(index_name)
        return cached[1] if cached else []

    def get_active_expiry(self, index_name: str) -> Optional[str]:
        """Get the current active/weekly expiry date for an index (YYYY-MM-DD)."""
        contracts = self.get_option_contracts(index_name)
        if not contracts:
            return None
        today = datetime.now().strftime("%Y-%m-%d")
        valid_expiries = sorted({c["expiry"] for c in contracts if c.get("expiry") and c["expiry"] >= today})
        return valid_expiries[0] if valid_expiries else None

    def get_option_chain(self, index_name: str, expiry_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch the full real-time option chain with Greeks, IV, PCR, volume, OI."""
        key = UPSTOX_INDEX_KEYS.get(index_name.upper())
        if not key:
            return []

        expiry = expiry_date or self.get_active_expiry(index_name)
        if not expiry:
            return []

        cache_key = f"{index_name}_{expiry}"
        now = time.monotonic()
        with self._lock:
            cached = self._chain_cache.get(cache_key)
            if cached and (now - cached[0] < 1.0): # 1-second debounce
                return cached[1]

        url = f"{UPSTOX_OPTION_CHAIN_URL}?instrument_key={urllib.parse.quote(key)}&expiry_date={expiry}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                with self._lock:
                    self._chain_cache[cache_key] = (now, data)
                self._record_success()
                return data
            self._record_failure(f"HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            self._record_failure(e)
            logger.warning(f"Upstox option chain fetch failed for {index_name} ({expiry}): {e}")

        cached = self._chain_cache.get(cache_key)
        return cached[1] if cached else []

    def get_option_premium_and_greeks(
        self,
        index_name: str,
        strike: float,
        option_type: str,
        expiry_date: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Find the matching strike in the option chain and return price, Greeks, and liquidity."""
        chain = self.get_option_chain(index_name, expiry_date)
        if not chain:
            return None

        is_call = option_type.upper() in ("CE", "CALL")
        target_strike = round(float(strike), 2)

        # Find closest or exact strike
        best_diff = float("inf")
        best_item = None
        for item in chain:
            s = float(item.get("strike_price") or 0.0)
            diff = abs(s - target_strike)
            if diff < best_diff:
                best_diff = diff
                best_item = item

        if not best_item or best_diff > 50.0:
            return None

        leg_data = best_item.get("call_options" if is_call else "put_options") or {}
        market_data = leg_data.get("market_data") or {}
        greeks = leg_data.get("option_greeks") or {}

        ltp = float(market_data.get("ltp") or 0.0)
        return {
            "instrument_key": leg_data.get("instrument_key", ""),
            "price": ltp,
            "ltp": ltp,
            "bid": float(market_data.get("bid_price") or 0.0),
            "ask": float(market_data.get("ask_price") or 0.0),
            "volume": float(market_data.get("volume") or 0.0),
            "oi": float(market_data.get("oi") or 0.0),
            "prev_oi": float(market_data.get("prev_oi") or 0.0),
            "iv": float(greeks.get("iv") or 0.0),
            "delta": float(greeks.get("delta") or 0.0),
            "gamma": float(greeks.get("gamma") or 0.0),
            "theta": float(greeks.get("theta") or 0.0),
            "vega": float(greeks.get("vega") or 0.0),
            "strike": float(best_item.get("strike_price") or 0.0),
            "spot": float(best_item.get("underlying_spot_price") or 0.0),
            "pcr": float(best_item.get("pcr") or 0.0),
            "source": "upstox_option_chain",
        }

    def get_historical_candles(
        self,
        index_name: str,
        interval: str = "5minute",
        days: int = 5
    ) -> List[List[Any]]:
        """Fetch historical candles via Upstox v3 API.

        interval can be: '1minute', '5minute', '15minute', '30minute', 'day'
        """
        key = UPSTOX_INDEX_KEYS.get(index_name.upper())
        if not key:
            return []

        # Map interval to v3 format (unit, interval_num)
        unit = "minutes"
        interval_val = "5"
        if interval in ("1minute", "1m"):
            interval_val = "1"
        elif interval in ("5minute", "5m"):
            interval_val = "5"
        elif interval in ("15minute", "15m"):
            interval_val = "15"
        elif interval in ("30minute", "30m"):
            interval_val = "30"
        elif interval in ("day", "1d"):
            unit = "days"
            interval_val = "1"

        quoted_key = urllib.parse.quote(key, safe="")
        # Try intraday endpoint first
        url_intraday = UPSTOX_INTRADAY_CANDLE_URL.format(key=quoted_key, unit=unit, interval=interval_val)
        try:
            resp = requests.get(url_intraday, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                candles = (resp.json().get("data") or {}).get("candles", [])
                if candles:
                    self._record_success()
                    return candles
        except Exception as e:
            logger.debug(f"Upstox intraday candle fetch error: {e}")

        # Fallback to date-range historical v3
        today = datetime.now()
        frm = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        to = today.strftime("%Y-%m-%d")
        url_hist = UPSTOX_HIST_CANDLE_URL.format(
            key=quoted_key, unit=unit, interval=interval_val, to=to, frm=frm
        )
        try:
            resp = requests.get(url_hist, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                candles = (resp.json().get("data") or {}).get("candles", [])
                self._record_success()
                return candles
            self._record_failure(f"HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            self._record_failure(e)
            logger.warning(f"Upstox historical candles failed for {index_name}: {e}")

        return []

    def get_option_chain_df(self, index_name: str, expiry_date: Optional[str] = None) -> Optional[Any]:
        """Convert Upstox option chain into standardized pandas DataFrame."""
        import pandas as pd
        raw_chain = self.get_option_chain(index_name, expiry_date)
        if isinstance(raw_chain, dict):
            raw_chain = raw_chain.get("data", [])
        if not raw_chain:
            return None
        rows = []
        for item in raw_chain:
            strike = float(item.get("strike_price") or 0.0)
            c = item.get("call_options") or {}
            p = item.get("put_options") or {}
            c_mkt = c.get("market_data") or {}
            p_mkt = p.get("market_data") or {}
            c_grk = c.get("option_greeks") or {}
            p_grk = p.get("option_greeks") or {}

            c_oi = float(c_mkt.get("oi") or 0.0)
            c_prev_oi = float(c_mkt.get("prev_oi") or 0.0)
            p_oi = float(p_mkt.get("oi") or 0.0)
            p_prev_oi = float(p_mkt.get("prev_oi") or 0.0)

            rows.append({
                "strike": strike,
                "call_ltp": float(c_mkt.get("ltp") or 0.0),
                "put_ltp": float(p_mkt.get("ltp") or 0.0),
                "call_oi": c_oi,
                "put_oi": p_oi,
                "call_oi_change": c_oi - c_prev_oi,
                "put_oi_change": p_oi - p_prev_oi,
                "call_volume": float(c_mkt.get("volume") or 0.0),
                "put_volume": float(p_mkt.get("volume") or 0.0),
                "call_iv": float(c_grk.get("iv") or 0.0),
                "put_iv": float(p_grk.get("iv") or 0.0),
                "call_delta": float(c_grk.get("delta") or 0.0),
                "put_delta": float(p_grk.get("delta") or 0.0),
                "call_gamma": float(c_grk.get("gamma") or 0.0),
                "put_gamma": float(p_grk.get("gamma") or 0.0),
                "call_theta": float(c_grk.get("theta") or 0.0),
                "put_theta": float(p_grk.get("theta") or 0.0),
                "call_vega": float(c_grk.get("vega") or 0.0),
                "put_vega": float(p_grk.get("vega") or 0.0),
                "call_bid": float(c_mkt.get("bid_price") or 0.0),
                "call_ask": float(c_mkt.get("ask_price") or 0.0),
                "put_bid": float(p_mkt.get("bid_price") or 0.0),
                "put_ask": float(p_mkt.get("ask_price") or 0.0),
                "pcr": float(item.get("pcr") or 0.0),
                "underlying_spot": float(item.get("underlying_spot_price") or 0.0),
            })
        df = pd.DataFrame(rows)
        df.sort_values(by="strike", inplace=True)
        df.reset_index(drop=True, inplace=True)
        df.attrs["source"] = "upstox"
        df.attrs["provider"] = "upstox"
        df.attrs["auto_certified"] = True
        return df

