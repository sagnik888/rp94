"""
Broker Interface — Abstracting order execution
═══════════════════════════════════════════════════════════════
"""

import time
import threading
import logging
from collections import deque
from typing import Any, Dict, Optional
from loguru import logger
from trading.api_rate_limiter import get_shared_api_limiter
from trading.order_retry import BrokerOrderAmbiguous
from trading.secure_logging import install_smartapi_log_redaction
try:
    from SmartApi import SmartConnect
    install_smartapi_log_redaction()
    try:
        import logzero
        logzero.logger.setLevel(logging.CRITICAL)
    except Exception:
        pass
    HAS_SMARTAPI = True
except ImportError:
    SmartConnect = None
    HAS_SMARTAPI = False


from concurrent.futures import ThreadPoolExecutor

class Broker:
    """Base Broker Interface"""
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=5)

    def place_order(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "CARRYFORWARD",
        price: float = 0.0,
        touch_quote: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        raise NotImplementedError

    def place_order_async(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "CARRYFORWARD",
        price: float = 0.0,
        callback=None,
        touch_quote: Optional[Dict[str, Any]] = None,
    ):
        """Fire-and-forget async order placement"""
        def task():
            try:
                res = self.place_order(symbol, token, qty, side, product_type, price, touch_quote=touch_quote)
                if callback: callback(res)
                return res
            except Exception as e:
                logger.error(f"Async Order Task Error: {e}")
                return None
        return self._executor.submit(task)

    def close_order(self, symbol: str, token: str, qty: int, side: str, product_type: str = "CARRYFORWARD", price: float = 0.0) -> Optional[str]:
        raise NotImplementedError

    def get_positions(self) -> Optional[Dict]:
        """Fetch real-time position book from broker"""
        raise NotImplementedError

    def get_order_status(self, order_id: str) -> Optional[Dict]:
        return None

    def find_working_protective_stop(self, symbol: str, token: str, side: str) -> str:
        """Return the order id of an existing working exchange-side STOPLOSS for this
        contract, or "" if none. Overridden by real brokers; keeps protective-stop
        placement idempotent across retries and concurrent callers."""
        return ""

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        return False

    def place_protective_stop(
        self, symbol: str, token: str, qty: int, side: str, trigger_price: float,
        product_type: str = "CARRYFORWARD",
    ) -> Optional[str]:
        return None

    def modify_protective_stop(
        self, order_id: str, symbol: str, token: str, qty: int, side: str,
        trigger_price: float, product_type: str = "CARRYFORWARD",
    ) -> bool:
        return False

class SignalSimBroker(Broker):
    """Simulation Broker for Scanned Signals"""
    def __init__(self):
        super().__init__()

    def place_order(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "CARRYFORWARD",
        price: float = 0.0,
        touch_quote: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        logger.debug(f"SIGNAL SIMULATION: {side} {qty} {symbol} (Real Data Analysed) | Price: {price}")
        return "SIM_ORD_" + symbol

    def close_order(self, symbol: str, token: str, qty: int, side: str, product_type: str = "CARRYFORWARD", price: float = 0.0) -> Optional[str]:
        logger.debug(f"SIGNAL SIMULATION CLOSE: {side} {qty} {symbol} | Price: {price}")
        return "SIM_EXIT_" + symbol

    def get_positions(self) -> Optional[Dict]:
        return {"data": []}

    def get_order_status(self, order_id: str) -> Optional[Dict]:
        return {"orderid": order_id, "status": "complete", "orderstatus": "complete"}

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        return True

    def place_protective_stop(
        self, symbol: str, token: str, qty: int, side: str, trigger_price: float,
        product_type: str = "CARRYFORWARD",
    ) -> Optional[str]:
        return f"SIM_SL_{symbol}"

    def modify_protective_stop(
        self, order_id: str, symbol: str, token: str, qty: int, side: str,
        trigger_price: float, product_type: str = "CARRYFORWARD",
    ) -> bool:
        return True

class SmartApiBroker(Broker):
    """AngelOne SmartAPI Broker"""
    def __init__(self, smart_api: SmartConnect):
        super().__init__()
        install_smartapi_log_redaction()
        self.api = smart_api
        self._order_lock = threading.Lock()
        self._position_backoff_until = 0.0
        self._position_backoff_failures = 0
        self._api_limiter = get_shared_api_limiter(self.api)
        self._entry_order_times = deque()
        self._entry_kill_latched_until = 0.0

    @staticmethod
    def _exchange_for_symbol(symbol: str) -> str:
        return "BFO" if "SENSEX" in str(symbol or "").upper() else "NFO"

    @staticmethod
    def _tick_price(value: float) -> float:
        return round(max(0.05, round(float(value) / 0.05) * 0.05), 2)

    @staticmethod
    def _positive_float(value: Any) -> float:
        try:
            number = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return number if number > 0 else 0.0

    def _ltp_anchor_limit(self, price: float, side: str, is_option: bool, settings) -> float:
        if price <= 0:
            return 0.0
        if is_option:
            buffer_pct = (
                float(getattr(settings, "option_limit_buffer_pct", 0.40) or 0.40) / 100.0
                if settings
                else 0.004
            )
            min_ticks = int(getattr(settings, "option_limit_buffer_min_ticks", 2) or 2) if settings else 2
            buffer_amt = max(price * buffer_pct, min_ticks * 0.05)
        else:
            buffer_bps = (
                float(getattr(settings, "fut_limit_buffer_bps", 5.0) or 5.0) / 10000.0
                if settings
                else 0.0005
            )
            buffer_amt = price * buffer_bps
        if str(side).upper() == "BUY":
            return self._tick_price(price + buffer_amt)
        return self._tick_price(max(0.05, price - buffer_amt))

    def _touch_quote_usable(
        self,
        touch_quote: Optional[Dict[str, Any]],
        settings,
    ) -> tuple[bool, str, Dict[str, float]]:
        if not isinstance(touch_quote, dict):
            return False, "missing", {}
        bid = self._positive_float(touch_quote.get("bid"))
        ask = self._positive_float(touch_quote.get("ask"))
        values = {"bid": bid, "ask": ask}
        if bid <= 0 or ask <= 0:
            return False, "missing_bid_ask", values
        if ask < bid:
            return False, "crossed_book", values
        age = self._positive_float(touch_quote.get("age_seconds"))
        max_age = (
            float(getattr(settings, "execution_touch_quote_max_age_seconds", 1.5) or 1.5)
            if settings
            else 1.5
        )
        values["age_seconds"] = age
        if age > max_age:
            return False, "stale", values
        spread = max(0.0, ask - bid)
        mid = (ask + bid) / 2.0
        spread_pct = (spread / mid * 100.0) if mid > 0 else 999.0
        spread_ticks = spread / 0.05
        values.update({"spread": spread, "spread_pct": spread_pct, "spread_ticks": spread_ticks})
        max_spread_pct = (
            float(getattr(settings, "option_touch_quote_max_spread_pct", 1.0) or 1.0)
            if settings
            else 1.0
        )
        max_spread_ticks = (
            int(getattr(settings, "option_touch_quote_max_spread_ticks", 40) or 40)
            if settings
            else 40
        )
        if spread_pct > max_spread_pct or spread_ticks > max_spread_ticks:
            return False, "wide_spread", values
        return True, "ok", values

    def _touch_anchor_limit(self, side: str, values: Dict[str, float], settings) -> float:
        extra_ticks = int(getattr(settings, "option_touch_limit_extra_ticks", 1) or 1) if settings else 1
        extra = max(0, extra_ticks) * 0.05
        if str(side).upper() == "BUY":
            return self._tick_price(values.get("ask", 0.0) + extra)
        return self._tick_price(max(0.05, values.get("bid", 0.0) - extra))

    def _api_call(self, context: str, fn, timeout: float = 5.0):
        if not self._api_limiter.begin_priority_call(timeout=timeout):
            raise TimeoutError(f"Shared AngelOne REST limiter timed out before {context}")
        try:
            with self._order_lock:
                return fn()
        finally:
            self._api_limiter.end_priority_call()

    def _entry_rate_allowed(self) -> bool:
        from config.settings import get_settings

        settings = get_settings()
        now = time.monotonic()
        if now < self._entry_kill_latched_until:
            return False
        window = max(1.0, float(getattr(settings, "entry_order_rate_window_seconds", 60.0) or 60.0))
        limit = max(1, int(getattr(settings, "max_entry_orders_per_minute", 6) or 6))
        while self._entry_order_times and now - self._entry_order_times[0] >= window:
            self._entry_order_times.popleft()
        if len(self._entry_order_times) >= limit:
            latch = max(10.0, float(getattr(settings, "entry_order_kill_latch_seconds", 300.0) or 300.0))
            self._entry_kill_latched_until = now + latch
            logger.critical(
                f"ENTRY ORDER RATE KILL-SWITCH: {len(self._entry_order_times)} orders "
                f"inside {window:.0f}s; entries blocked for {latch:.0f}s."
            )
            return False
        self._entry_order_times.append(now)
        return True

    def _position_should_backoff(self, payload) -> bool:
        text = str(payload or "").lower()
        markers = (
            "exceeding access rate",
            "access rate",
            "rate limit",
            "too many requests",
            "read timed out",
            "connect timeout",
            "connection timed out",
            "timed out",
            "timeout",
        )
        return any(marker in text for marker in markers)

    def _mark_position_backoff(self, payload) -> None:
        failures = int(getattr(self, "_position_backoff_failures", 0) or 0) + 1
        delay = min(60.0, 12.0 * (2 ** max(0, failures - 1)))
        self._position_backoff_failures = failures
        self._position_backoff_until = time.time() + delay
        logger.warning(f"Broker position REST backoff {delay:.1f}s after failure: {payload}")

    def place_order(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "CARRYFORWARD",
        price: float = 0.0,
        touch_quote: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        try:
            if not self._entry_rate_allowed():
                return None
            # ══ Dynamic Exchange Resolution ══
            # SENSEX Options/Futures live on BFO (BSE), NIFTY/BANKNIFTY on NFO (NSE)
            exchange = self._exchange_for_symbol(symbol)

            # Use a MARKETABLE LIMIT wherever a reference price exists. The safe
            # default remains signal/LTP anchored. If guarded touch-quote execution
            # is enabled and the book is fresh/tight, option limits anchor to
            # ask+ticks (BUY) or bid-ticks (SELL). Shadow mode logs both limits.
            is_option = "CE" in symbol.upper() or "PE" in symbol.upper()
            try:
                from config.settings import get_settings
                settings = get_settings()
            except Exception:
                settings = None
            fut_protection = (
                bool(getattr(settings, "fut_entry_protection_limit_enabled", True))
                if settings is not None
                else True
            )
            order_type = "LIMIT" if (price > 0.0 and (is_option or fut_protection)) else "MARKET"

            limit_price = price
            if order_type == "LIMIT":
                ltp_limit = self._ltp_anchor_limit(float(price or 0.0), side, is_option, settings)
                limit_price = ltp_limit
                anchor = "ltp"
                touch_limit = 0.0
                touch_reason = "disabled"
                if is_option:
                    touch_ok, touch_reason, touch_values = self._touch_quote_usable(touch_quote, settings)
                    if touch_ok:
                        touch_limit = self._touch_anchor_limit(side, touch_values, settings)
                    use_touch = bool(getattr(settings, "execution_use_touch_quote", False)) if settings else False
                    shadow = bool(getattr(settings, "execution_touch_quote_shadow_enabled", True)) if settings else True
                    strict = bool(getattr(settings, "execution_touch_quote_strict", False)) if settings else False
                    if shadow or use_touch:
                        logger.info(
                            f"TOUCH EXECUTION SHADOW: {side} {symbol} "
                            f"ref={float(price or 0.0):.2f} ltp_limit={ltp_limit:.2f} "
                            f"bid={touch_values.get('bid', 0.0):.2f} "
                            f"ask={touch_values.get('ask', 0.0):.2f} "
                            f"touch_limit={touch_limit:.2f} usable={touch_ok} reason={touch_reason}"
                        )
                    if use_touch:
                        if touch_ok and touch_limit > 0:
                            limit_price = touch_limit
                            anchor = "touch"
                        elif strict:
                            logger.warning(
                                f"Touch-quote execution blocked {side} {symbol}: {touch_reason}"
                            )
                            return None
                self._last_execution_preview = {
                    "symbol": symbol,
                    "side": side,
                    "reference_price": float(price or 0.0),
                    "ltp_limit_price": ltp_limit,
                    "touch_limit_price": touch_limit,
                    "limit_price": limit_price,
                    "anchor": anchor,
                    "touch_reason": touch_reason,
                }

            params = {
                "variety": "NORMAL",
                "tradingsymbol": symbol,
                "symboltoken": token,
                "transactiontype": side, # BUY or SELL
                "exchange": exchange,
                "ordertype": order_type,
                "producttype": product_type,
                "duration": "DAY",
                "quantity": str(qty)
            }
            if order_type == "LIMIT":
                params["price"] = f"{limit_price:.2f}"

            logger.info(f"PLACING REAL ORDER: {side} {qty} {symbol} on {exchange} | Type: {order_type} | Price: {limit_price:.2f}")

            try:
                place_full = getattr(self.api, "placeOrderFullResponse", None)
                response = self._api_call(
                    "place entry order",
                    lambda: place_full(params) if callable(place_full) else self.api.placeOrder(params),
                )
            except Exception as api_err:
                logger.error(f"Broker API error during placeOrder: {api_err}")
                raise BrokerOrderAmbiguous(
                    f"Broker acknowledgement ambiguous after placeOrder exception: {api_err}"
                ) from api_err

            if isinstance(response, dict):
                if response.get("status") is False or response.get("errorcode"):
                    logger.error(f"Broker rejected order: {response}")
                    return None
                data = response.get("data", response)
                if isinstance(data, dict):
                    order_id = str(
                        data.get("orderid")
                        or data.get("uniqueorderid")
                        or ""
                    )
                else:
                    order_id = str(data or "")
            else:
                order_id = str(response or "")

            if not order_id or order_id == "None":
                raise BrokerOrderAmbiguous(
                    f"Broker acknowledgement ambiguous; no order id returned: {response}"
                )

            logger.success(f"REAL ORDER PLACED: {order_id} ({exchange})")
            return order_id
        except Exception as e:
            if isinstance(e, BrokerOrderAmbiguous):
                raise
            logger.error(f"Order placement failed on {exchange if 'exchange' in locals() else 'Unknown'}: {e}")
            return None

    def close_order(self, symbol: str, token: str, qty: int, side: str, product_type: str = "CARRYFORWARD", price: float = 0.0) -> Optional[str]:
        """Place a risk-reducing MARKET exit outside the entry-rate kill-switch."""
        params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": side.upper(),
            "exchange": self._exchange_for_symbol(symbol),
            "ordertype": "MARKET",
            "producttype": product_type,
            "duration": "DAY",
            "quantity": str(qty),
        }
        try:
            place_full = getattr(self.api, "placeOrderFullResponse", None)
            response = self._api_call(
                "place exit order",
                lambda: place_full(params) if callable(place_full) else self.api.placeOrder(params),
            )
            if isinstance(response, dict):
                if response.get("status") is False or response.get("errorcode"):
                    return None
                data = response.get("data", response)
                return str(data.get("orderid") or data.get("uniqueorderid") or "") if isinstance(data, dict) else str(data or "")
            return str(response or "")
        except Exception as exc:
            logger.error(f"Exit order placement failed for {symbol}: {exc}")
            raise BrokerOrderAmbiguous(f"Broker exit acknowledgement ambiguous: {exc}") from exc

    def get_order_status(self, order_id: str) -> Optional[Dict]:
        if not order_id:
            return None
        try:
            response = self._api_call("fetch order book", self.api.orderBook, timeout=2.0)
            rows = response.get("data", []) if isinstance(response, dict) else []
            for row in rows or []:
                if str(row.get("orderid") or "") == str(order_id) or str(row.get("uniqueorderid") or "") == str(order_id):
                    return dict(row)
            return None
        except Exception as exc:
            logger.warning(f"Order-status poll failed for {order_id}: {exc}")
            return None

    def find_working_protective_stop(self, symbol: str, token: str, side: str) -> str:
        """Return the order id of an existing working exchange-side STOPLOSS for this
        contract, or "" if none. Keeps protective-stop placement idempotent so a lost
        acknowledgement or a concurrent caller can never create a duplicate SL-M."""
        if not symbol:
            return ""
        try:
            response = self._api_call("scan order book for stop", self.api.orderBook, timeout=2.0)
        except Exception as exc:
            logger.warning(f"Protective-stop order-book scan failed for {symbol}: {exc}")
            return ""
        rows = response.get("data", []) if isinstance(response, dict) else []
        terminal = {"complete", "completed", "filled", "cancelled", "canceled", "rejected", "expired", "lapsed"}
        want_side = str(side or "").upper()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("tradingsymbol") or "") != str(symbol):
                continue
            row_token = str(row.get("symboltoken") or "")
            if token and row_token and row_token != str(token):
                continue
            variety = str(row.get("variety") or "").upper()
            ordertype = str(row.get("ordertype") or "").upper()
            if "STOPLOSS" not in variety and "STOPLOSS" not in ordertype:
                continue
            if want_side and str(row.get("transactiontype") or "").upper() != want_side:
                continue
            status = str(row.get("orderstatus") or row.get("status") or "").strip().lower().replace("_", " ")
            if status in terminal:
                continue
            oid = str(row.get("orderid") or row.get("uniqueorderid") or "")
            if oid:
                return oid
        return ""

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        if not order_id:
            return True
        try:
            response = self._api_call(
                "cancel order",
                lambda: self.api.cancelOrder(str(order_id), str(variety or "NORMAL").upper()),
            )
            return not isinstance(response, dict) or (
                response.get("status") is not False and not response.get("errorcode")
            )
        except Exception as exc:
            logger.error(f"Order cancellation failed for {order_id}: {exc}")
            return False

    def place_protective_stop(
        self, symbol: str, token: str, qty: int, side: str, trigger_price: float,
        product_type: str = "CARRYFORWARD",
    ) -> Optional[str]:
        trigger = self._tick_price(trigger_price)
        params = {
            "variety": "STOPLOSS",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": side.upper(),
            "exchange": self._exchange_for_symbol(symbol),
            "ordertype": "STOPLOSS_MARKET",
            "producttype": product_type,
            "duration": "DAY",
            "triggerprice": str(trigger),
            "quantity": str(qty),
        }
        try:
            place_full = getattr(self.api, "placeOrderFullResponse", None)
            response = self._api_call(
                "place protective stop",
                lambda: place_full(params) if callable(place_full) else self.api.placeOrder(params),
            )
        except Exception as exc:
            # Audit fix: a STOPLOSS_MARKET can reach and be accepted by the exchange
            # even when the acknowledgement is lost (timeout / 429 / socket drop). Do
            # NOT return None here — that is indistinguishable from a clean reject, so
            # the caller re-places and TWO SL-M orders rest at the exchange; when both
            # trigger, the second opens a naked reversed leg. Signal ambiguity exactly
            # like place_order so the caller reconciles instead of blindly re-placing.
            logger.error(f"Protective stop placement failed for {symbol}: {exc}")
            raise BrokerOrderAmbiguous(
                f"Protective stop acknowledgement ambiguous after exception: {exc}"
            ) from exc
        if isinstance(response, dict):
            if response.get("status") is False or response.get("errorcode"):
                logger.error(f"Broker rejected protective stop: {response}")
                return None
            data = response.get("data", response)
            order_id = (
                str(data.get("orderid") or data.get("uniqueorderid") or "")
                if isinstance(data, dict)
                else str(data or "")
            )
        else:
            order_id = str(response or "")
        if not order_id or order_id == "None":
            raise BrokerOrderAmbiguous(
                f"Protective stop acknowledgement ambiguous; no order id returned: {response}"
            )
        return order_id

    def modify_protective_stop(
        self, order_id: str, symbol: str, token: str, qty: int, side: str,
        trigger_price: float, product_type: str = "CARRYFORWARD",
    ) -> bool:
        trigger = self._tick_price(trigger_price)
        params = {
            "variety": "STOPLOSS",
            "orderid": str(order_id),
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": side.upper(),
            "exchange": self._exchange_for_symbol(symbol),
            "ordertype": "STOPLOSS_MARKET",
            "producttype": product_type,
            "duration": "DAY",
            "triggerprice": str(trigger),
            "quantity": str(qty),
        }
        try:
            response = self._api_call(
                "modify protective stop",
                lambda: self.api.modifyOrder(params),
            )
            return not isinstance(response, dict) or (
                response.get("status") is not False and not response.get("errorcode")
            )
        except Exception as exc:
            logger.error(f"Protective stop modification failed for {symbol}: {exc}")
            return False

    def get_positions(self) -> Optional[Dict]:
        remaining = float(getattr(self, "_position_backoff_until", 0.0) or 0.0) - time.time()
        if remaining > 0:
            logger.debug(f"Skipping broker positions; backoff active for {remaining:.1f}s")
            return None
        try:
            response = self._api_call("fetch positions", self.api.position, timeout=2.0)
            if isinstance(response, dict):
                if response.get("status") is False or response.get("errorcode"):
                    if self._position_should_backoff(response):
                        self._mark_position_backoff(response)
                    logger.error(f"Broker returned error for positions: {response}")
                    return None
            self._position_backoff_failures = 0
            self._position_backoff_until = 0.0
            return response
        except Exception as e:
            if self._position_should_backoff(e):
                self._mark_position_backoff(e)
            logger.error(f"Failed to fetch position book: {e}")
            return None
