"""Angel One SmartAPI Execution Broker (Standby / Fallback).

Provides order execution, protective stops, and position reconciliation
as a backup broker when primary Upstox fails for > 30 seconds.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

import pyotp
from loguru import logger

from trading.broker import Broker, order_filled_quantity
from trading.order_retry import BrokerOrderAmbiguous, BrokerOrderRejected

try:
    from SmartApi import SmartConnect
    HAS_SMARTAPI = True
except ImportError:
    SmartConnect = None
    HAS_SMARTAPI = False


class AngelOneBroker(Broker):
    """Angel One SmartAPI execution broker for fallback."""

    def __init__(
        self,
        api_key: str = "",
        client_id: str = "",
        password: str = "",
        totp_secret: str = "",
    ):
        super().__init__()
        from config.settings import get_settings
        settings = get_settings()

        self.api_key = api_key or getattr(settings, "angelone_api_key", "")
        self.client_id = client_id or getattr(settings, "angelone_client_id", "")
        self.password = password or getattr(settings, "angelone_password", "")
        self.totp_secret = totp_secret or getattr(settings, "angelone_totp_secret", "")

        self._lock = threading.RLock()
        self.client: Optional[Any] = None
        self._auth_time = 0.0
        self._jwt_token = ""
        self._refresh_token = ""
        self._feed_token = ""
        self.is_connected = False

    def _ensure_connected(self) -> bool:
        if not HAS_SMARTAPI:
            return False

        with self._lock:
            now = time.time()
            if self.client and self.is_connected and (now - self._auth_time < 3600.0 * 6):
                return True

            if not self.api_key or not self.client_id or not self.password or not self.totp_secret:
                return False

            try:
                totp = pyotp.TOTP(self.totp_secret.replace(" ", "")).now()
                smart = SmartConnect(api_key=self.api_key)
                data = smart.generateSession(self.client_id, self.password, totp)
                if data and data.get("status"):
                    self.client = smart
                    self._jwt_token = data.get("data", {}).get("jwtToken", "")
                    self._refresh_token = data.get("data", {}).get("refreshToken", "")
                    self._feed_token = data.get("data", {}).get("feedToken", "")
                    self._auth_time = now
                    self.is_connected = True
                    logger.info("Angel One fallback broker authenticated successfully")
                    return True
                else:
                    logger.error(f"Angel One fallback login failed: {data}")
            except Exception as e:
                logger.error(f"Angel One connection exception: {e}")
        return False

    def place_order(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "INTRADAY",
        price: float = 0.0,
        **kwargs: Any,
    ) -> str:
        if not self._ensure_connected() or not self.client:
            raise BrokerOrderRejected("Angel One fallback broker unauthenticated")

        transaction_type = "BUY" if side.upper() == "BUY" else "SELL"
        order_type = "MARKET" if price <= 0.0 else "LIMIT"
        exchange = "BSE" if "SENSEX" in symbol else "NFO"

        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": str(token),
            "transactiontype": transaction_type,
            "exchange": exchange,
            "ordertype": order_type,
            "producttype": "INTRADAY" if "INTRA" in product_type.upper() else "CARRYFORWARD",
            "duration": "DAY",
            "price": str(round(float(price), 2)) if price > 0.0 else "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(abs(int(qty))),
        }
        try:
            order_id = self.client.placeOrder(order_params)
            if order_id:
                logger.info(f"[FALLBACK] Angel One order placed: {symbol} -> order_id={order_id}")
                return str(order_id)
        except Exception as e:
            logger.error(f"Angel One place_order error for {symbol}: {e}")
            raise BrokerOrderAmbiguous(f"Angel One order status uncertain: {e}") from e

        raise BrokerOrderRejected(f"Angel One rejected order for {symbol}")

    def close_order(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "INTRADAY",
        price: float = 0.0,
        **kwargs: Any,
    ) -> str:
        exit_side = "SELL" if side.upper() == "BUY" else "BUY"
        return self.place_order(symbol, token, qty, exit_side, product_type, price, **kwargs)

    def get_positions(self) -> Optional[Dict[str, List[Dict[str, Any]]]]:
        if not self._ensure_connected() or not self.client:
            return None
        try:
            resp = self.client.position()
            if resp and resp.get("status"):
                raw = resp.get("data") or []
                rows = []
                for p in raw:
                    rows.append({
                        "tradingsymbol": p.get("tradingsymbol", ""),
                        "symboltoken": p.get("symboltoken", ""),
                        "netqty": int(float(p.get("netqty") or 0)),
                        "buyqty": int(float(p.get("buyqty") or 0)),
                        "sellqty": int(float(p.get("sellqty") or 0)),
                        "buyprice": float(p.get("buyavgprice") or 0.0),
                        "sellprice": float(p.get("sellavgprice") or 0.0),
                        "pnl": float(p.get("pnl") or 0.0),
                        "realised": float(p.get("realised") or 0.0),
                        "unrealised": float(p.get("unrealised") or 0.0),
                        "producttype": p.get("producttype", "INTRADAY"),
                    })
                return {"data": rows}
        except Exception as e:
            logger.warning(f"Angel One get_positions failed: {e}")
        return None

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        if not self._ensure_connected() or not self.client:
            return {"status": "UNKNOWN", "orderstatus": "UNKNOWN"}
        try:
            resp = self.client.orderBook()
            if resp and resp.get("status"):
                for o in resp.get("data") or []:
                    if str(o.get("orderid")) == str(order_id):
                        st = str(o.get("orderstatus") or "").lower()
                        return {
                            "order_id": order_id,
                            "orderstatus": st,
                            "status": st,
                            "filledshares": int(float(o.get("filledshares") or 0)),
                            "filled_quantity": int(float(o.get("filledshares") or 0)),
                            "price": float(o.get("averageprice") or o.get("price") or 0.0),
                        }
        except Exception as e:
            logger.warning(f"Angel One get_order_status failed: {e}")
        return {"status": "UNKNOWN", "orderstatus": "UNKNOWN"}

    def place_protective_stop(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        trigger_price: float,
        product_type: str = "INTRADAY",
        **kwargs: Any,
    ) -> Optional[str]:
        if not self._ensure_connected() or not self.client:
            return None
        stop_side = "SELL" if side.upper() == "BUY" else "BUY"
        exchange = "BSE" if "SENSEX" in symbol else "NFO"
        order_params = {
            "variety": "STOPLOSS",
            "tradingsymbol": symbol,
            "symboltoken": str(token),
            "transactiontype": stop_side,
            "exchange": exchange,
            "ordertype": "STOPLOSS_LIMIT",
            "producttype": "INTRADAY" if "INTRA" in product_type.upper() else "CARRYFORWARD",
            "duration": "DAY",
            "price": str(round(float(trigger_price) * 0.98 if stop_side == "SELL" else float(trigger_price) * 1.02, 2)),
            "triggerprice": str(round(float(trigger_price), 2)),
            "quantity": str(abs(int(qty))),
        }
        try:
            order_id = self.client.placeOrder(order_params)
            if order_id:
                logger.info(f"[FALLBACK] Angel One stop placed: {symbol} trigger={trigger_price} -> order_id={order_id}")
                return str(order_id)
        except Exception as e:
            logger.error(f"Angel One stop order error: {e}")
        return None

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        if not self._ensure_connected() or not self.client:
            return False
        try:
            resp = self.client.cancelOrder(orderid=order_id, variety=variety)
            return bool(resp and resp.get("status"))
        except Exception as e:
            logger.warning(f"Angel One cancel_order failed for {order_id}: {e}")
        return False
