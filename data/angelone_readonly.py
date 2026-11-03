"""Streaming, history and bounded quote capabilities for the read-only adapter."""
from __future__ import annotations

from collections import deque
from datetime import timedelta
import json
import math
import threading
import time
import weakref

import pandas as pd


class ReadOnlyBudget:
    """Per-client budgets survive reauthentication and are shared by adapter instances."""
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = {"quote": deque(), "history": deque()}

    def reserve(self, endpoint: str, *, wait: float = 0.0) -> bool:
        deadline = time.monotonic() + wait
        while True:
            with self.lock:
                now = time.monotonic()
                calls = self.calls[endpoint]
                while calls and now - calls[0] >= 3600:
                    calls.popleft()
                delay = max(0.0, 1.05 - (now-calls[-1])) if calls else 0.0
                if endpoint == "history":
                    recent = [stamp for stamp in calls if now-stamp < 60]
                    if len(recent) >= 150:
                        delay = max(delay, 60-(now-recent[0]))
                    if len(calls) >= 5000:
                        delay = max(delay, 3600-(now-calls[0]))
                if delay <= 0:
                    calls.append(now)
                    return True
            if now + delay > deadline:
                return False
            time.sleep(delay)


_budgets = weakref.WeakValueDictionary()
_budgets_lock = threading.Lock()


def client_budget(client: str, api_key: str) -> ReadOnlyBudget:
    with _budgets_lock:
        key = client.strip().upper() or api_key
        budget = _budgets.get(key)
        if budget is None:
            budget = ReadOnlyBudget()
            _budgets[key] = budget
        return budget


_ANGEL_ROOT = "https://apiconnect.angelone.in"
_ANGEL_ROUTES = {
    "api.market.data": "/rest/secure/angelbroking/market/v1/quote",
    "api.candle.data": "/rest/secure/angelbroking/historical/v1/getCandleData",
}

_pooled_session_lock = threading.Lock()
_pooled_session_ref = None


def _pooled_session():
    """One keep-alive HTTP session for every Angel One REST call.

    SmartApi's ``_request`` ignores the ``reqsession`` it builds and calls the
    module-level ``requests.request``, so every quote opened a fresh TCP+TLS
    connection. On 2026-10-30 that was the entire 158-sample ``ssl_wrap_socket``
    cost, reached from the premium refresh worker through ``get_full_quotes``.
    Requests over one pooled session reuse the connection and skip the handshake.
    """
    global _pooled_session_ref
    if _pooled_session_ref is None:
        import requests
        from requests.adapters import HTTPAdapter

        with _pooled_session_lock:
            if _pooled_session_ref is None:
                session = requests.Session()
                session.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=64))
                _pooled_session_ref = session
    return _pooled_session_ref


class AngelReadOnlyCapabilities:
    @staticmethod
    def exchange_epoch(value):
        if value is None or value == "":
            return None
        try:
            if isinstance(value, (int, float)):
                stamp = float(value)
                return stamp/1000 if stamp > 100_000_000_000 else stamp
            stamp = pd.Timestamp(value)
            stamp = stamp.tz_localize("Asia/Kolkata") if stamp.tzinfo is None else stamp
            return stamp.timestamp()
        except (ValueError, TypeError, OverflowError):
            return None

    def _init_capabilities(self):
        self._budget = client_budget(self._client_id, self._api_key)
        self._stream_lock = threading.RLock()
        self._stream_stop = threading.Event()
        self._stream_thread = None
        self._stream_socket = None
        self._stream_generation = 0
        self._stream_connected = False
        self._stream_last_tick = 0.0
        self._stream_cache = {}
        self._jwt_token = ""
        self._feed_token = ""
        self._market_master = {}
        self._history_last_success = 0.0
        self._history_error = ""

    def _angel_post(self, route: str, params: dict, sdk_call):
        """POST one Angel One REST route over the shared keep-alive session.

        The pooled path carries the same headers, body and error surface as
        SmartApi's ``_request`` without the connection it opens per call. Any
        transport or decode failure falls back to ``sdk_call``, so this cannot
        take quotes or candles away.
        """
        api = getattr(self, "_api", None)
        token = str(getattr(self, "_jwt_token", "") or getattr(api, "access_token", "") or "")
        # ``generateSession`` answers with the JWT already prefixed ("Bearer eyJ...")
        # while SmartApi's own ``access_token`` is the bare token. Prepending "Bearer"
        # to the prefixed form sends "Bearer Bearer ...", which Angel One answers with
        # HTTP 200 + AG8001 "Invalid Token" on every route. Callers read that as a dead
        # session, so they re-authenticated every ~2s and no quote or candle ever
        # landed. Normalise to exactly one prefix.
        if token[:7].lower() == "bearer ":
            token = token[7:]
        headers_fn = getattr(api, "requestHeaders", None)
        if token and callable(headers_fn):
            try:
                headers = dict(headers_fn())
                headers["Authorization"] = f"Bearer {token}"
                response = _pooled_session().post(
                    _ANGEL_ROOT + _ANGEL_ROUTES[route],
                    data=json.dumps(params),
                    headers=headers,
                    timeout=float(getattr(self, "_timeout_seconds", 7.0)),
                    proxies=getattr(api, "proxies", None) or None,
                )
                payload = response.json()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                if payload.get("error_type"):
                    # The SDK raises on ``error_type``; callers translate that into
                    # their own failure status, so keep the same contract.
                    raise RuntimeError("angel_rest_error_type")
                return payload
        return sdk_call()

    def get_full_quotes(self, exchange_tokens: dict, *, wait: float = 0.0) -> dict:
        """Return partial responses honestly, keyed by exchange AND Angel token."""
        if (not self.connected or self._api is None) and not self.connect():
            return {}
        wanted = list(dict.fromkeys((str(exchange), str(token)) for exchange, tokens in exchange_tokens.items() for token in tokens))
        result = {}
        for offset in range(0, len(wanted), 50):
            batch = {}
            for exchange, token in wanted[offset:offset+50]:
                batch.setdefault(exchange, []).append(token)
            response = None
            for attempt in range(2 if wait > 0 else 1):
                if not self._budget.reserve("quote", wait=wait):
                    self.last_error = "quote_budget_exhausted"
                    break
                with self._lock:
                    try:
                        response = self._angel_post(
                            "api.market.data",
                            {"mode": "FULL", "exchangeTokens": batch},
                            lambda: self._api.getMarketData(mode="FULL", exchangeTokens=batch),
                        )
                    except Exception:
                        self.last_error = "market_data_failed"
                if isinstance(response, dict) and (response.get("status") or (response.get("errorcode") or response.get("errorCode")) == "AG8001"):
                    break
            if not isinstance(response, dict) or not response.get("status"):
                if isinstance(response, dict) and str(response.get("errorcode") or response.get("errorCode") or "") == "AG8001":
                    self.connected = False
                self.last_error = "market_data_rejected"
                break
            payload = response.get("data")
            if not isinstance(payload, dict):
                self.last_error = "market_data_invalid"
                break
            for row in payload.get("fetched") or []:
                if not isinstance(row, dict):
                    continue
                token = str(row.get("symbolToken") or row.get("symboltoken") or row.get("token") or "")
                exchange = str(row.get("exchange") or "")
                candidates = [(e, t) for e, t in wanted[offset:offset+50] if t == token and (not exchange or e == exchange)]
                if len(candidates) == 1:
                    result[candidates[0]] = dict(row, received_at=time.time(),
                        exchange_at=self.exchange_epoch(row.get("exchFeedTime")))
        return result

    def resolve_market_instruments(self, keys) -> list[dict]:
        """Resolve cash and auxiliary instruments from Angel's own current master."""
        from data.angelone_market_data import ANGELONE_SCRIP_MASTER_URL
        import requests
        day = self._master_day()
        with self._option_master_lock:
            if getattr(self, "_market_master_day", "") != day:
                try:
                    response = requests.get(ANGELONE_SCRIP_MASTER_URL, timeout=20)
                    response.raise_for_status()
                    rows = response.json()
                    master = {}
                    for row in rows:
                        exchange, symbol = str(row.get("exch_seg") or ""), str(row.get("symbol") or "")
                        if exchange in {"NSE", "BSE"} and row.get("token"):
                            master[(exchange, symbol)] = {"exchange": exchange, "symbol": symbol, "token": str(row["token"])}
                    self._market_master = master
                    self._market_master_day = day
                except Exception:
                    self.last_error = "cash_master_unavailable"
                    return []
            result = []
            for exchange, symbol in keys:
                # NSE master often appends -EQ; never reuse a Kite instrument token.
                row = self._market_master.get((exchange, symbol)) or self._market_master.get((exchange, symbol + "-EQ"))
                if row:
                    result.append(dict(row, canonical_symbol=symbol))
            return result

    def get_history(self, exchange: str, token: str, interval: str, start, end) -> pd.DataFrame:
        intervals = {"1min": "ONE_MINUTE", "5min": "FIVE_MINUTE", "15min": "FIFTEEN_MINUTE"}
        if interval not in intervals or exchange not in {"NSE", "BSE", "NFO", "BFO"} or not self.connect():
            return pd.DataFrame()
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        def local(stamp):
            return stamp.tz_convert("Asia/Kolkata").tz_localize(None) if stamp.tzinfo is not None else stamp
        start, end = local(start), local(end)
        frames = []
        cursor = start
        while cursor < end:
            if not self._budget.reserve("history", wait=1.1):
                self._history_error = "history_budget_exhausted"
                return pd.DataFrame()
            until = min(cursor + timedelta(days=20), end)
            params = {"exchange": exchange, "symboltoken": str(token), "interval": intervals[interval],
                      "fromdate": cursor.strftime("%Y-%m-%d %H:%M"), "todate": until.strftime("%Y-%m-%d %H:%M")}
            with self._lock:
                try:
                    response = self._angel_post(
                        "api.candle.data",
                        params,
                        lambda: self._api.getCandleData(params),
                    )
                except Exception:
                    self._history_error = "history_failed"
                    return pd.DataFrame()
            if not isinstance(response, dict) or not response.get("status"):
                self._history_error = "history_rejected"
                if isinstance(response, dict) and str(response.get("errorcode") or response.get("errorCode") or "") == "AG8001":
                    self.connected = False
                return pd.DataFrame()
            rows = response.get("data") or []
            if rows:
                try:
                    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("timestamp")))
                    if frame.index.tz is not None:
                        frame.index = frame.index.tz_convert("Asia/Kolkata").tz_localize(None)
                    for column in frame:
                        frame[column] = pd.to_numeric(frame[column], errors="coerce")
                    frames.append(frame)
                except (ValueError, TypeError):
                    self._history_error = "history_invalid"
                    return pd.DataFrame()
            cursor = until
        if not frames:
            self._history_error = "history_empty"
            return pd.DataFrame()
        frame = pd.concat(frames).sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        now = pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None)
        minutes = int(interval.replace("min", ""))
        frame = frame.loc[(frame.index >= start) & (frame.index < end) &
                          (frame.index + pd.Timedelta(minutes=minutes) <= now)]
        frame = frame.between_time("09:15", "15:29")
        self._history_last_success = time.time() if not frame.empty else self._history_last_success
        self._history_error = "" if not frame.empty else "history_empty"
        return frame

    @staticmethod
    def _load_stream_sdk():
        # Keep every SmartAPI import behind the same sanitized SDK boundary.
        from data.angelone_market_data import AngelOneIndexQuoteSource
        return AngelOneIndexQuoteSource._load_stream_sdk()

    def start_stream(self, instruments: list[dict], callback) -> bool:
        """One supervised read-only stream; generation guards reject retired callbacks."""
        with self._stream_lock:
            if self._stream_thread is not None and self._stream_thread.is_alive():
                return False
            self._stream_stop.clear()
            self._stream_generation += 1
            generation = self._stream_generation
        identities = {(int(row["exchange_type"]), str(row["token"])): dict(row) for row in instruments}
        if not identities or len(identities) > 1000:
            self.last_error = "stream_subscription_invalid"
            return False

        def run():
            retry = 0
            while not self._stream_stop.is_set() and generation == self._stream_generation:
                failure = "stream_disconnected"
                try:
                    if retry >= 3:
                        self.connected = False
                    if not self.connect():
                        failure = self.last_error or "stream_auth_unavailable"
                        raise RuntimeError("stream_auth_unavailable")
                    if not self._jwt_token or not self._feed_token:
                        failure = "stream_session_tokens_missing"
                        raise RuntimeError(failure)
                    socket_type = self._load_stream_sdk()
                    socket = socket_type(self._jwt_token, self._api_key, self._client_id, self._feed_token, max_retry_attempt=0)
                    socket.input_request_dict = {}
                    self._stream_socket = socket
                    def opened(ws, current_socket=socket):
                        if generation != self._stream_generation or self._stream_stop.is_set():
                            current_socket.close_connection()
                            return
                        self._stream_connected = True
                        self.last_error = ""
                        self._stream_last_tick = 0.0
                        self._stream_cache.clear()
                        batch = {}
                        for exchange_type, token in identities:
                            batch.setdefault(exchange_type, []).append(token)
                        current_socket.subscribe("readonly", 3, [{"exchangeType": e, "tokens": t} for e, t in batch.items()])
                    def data(ws, raw, current_socket=socket):
                        if (generation != self._stream_generation or self._stream_stop.is_set()
                                or self._stream_socket is not current_socket):
                            return
                        identity = identities.get((int(raw.get("exchange_type") or 0), str(raw.get("token") or "")))
                        if identity is None:
                            return
                        now = time.time()
                        try:
                            price = float(raw["last_traded_price"]) / 100.0
                            exchange_at = float(raw["exchange_timestamp"]) / 1000.0
                            if not math.isfinite(price) or price <= 0 or not -1.0 <= now-exchange_at <= 20:
                                return
                        except (ValueError, TypeError, KeyError):
                            return
                        row = {"ltp": price, "received_at": now, "time": now, "exchange_at": exchange_at,
                               "source": "angel_ws", "volume": raw.get("volume_trade_for_the_day"),
                               "previous_close": float(raw.get("closed_price") or 0.0)/100.0}
                        with self._stream_lock:
                            if generation != self._stream_generation or self._stream_stop.is_set():
                                return
                            self._stream_cache[(identity["exchange"], identity["symbol"])] = row
                            self._stream_last_tick = now
                        callback(identity, row)
                    socket.on_open, socket.on_data = opened, data
                    socket.on_error = lambda *args: setattr(self, "_stream_connected", False)
                    socket.on_close = lambda *args: setattr(self, "_stream_connected", False)
                    connected_at = time.time()
                    def watchdog(current_socket=socket, started_at=connected_at):
                        while not self._stream_stop.wait(5):
                            if self._stream_socket is not current_socket or generation != self._stream_generation:
                                return
                            if time.time()-max(started_at, self._stream_last_tick) > 90:
                                current_socket.close_connection()
                                return
                    threading.Thread(target=watchdog, daemon=True, name="AngelStreamWatchdog").start()
                    socket.connect()
                except Exception:
                    # connect() supplies fixed, sanitized reason codes. Preserve them
                    # without exposing SDK exceptions or broker request credentials.
                    self.last_error = failure
                finally:
                    self._stream_connected = False
                retry = min(retry+1, 6)
                self._stream_stop.wait(min(30, 2**retry))
        self._stream_thread = threading.Thread(target=run, daemon=True, name="AngelReadOnlyStream")
        self._stream_thread.start()
        return True

    def close(self):
        self.stop_stream()

    def stop_stream(self):
        with self._stream_lock:
            self._stream_stop.set()
            self._stream_generation += 1
            self._stream_connected = False
            socket = self._stream_socket
        if socket is not None:
            socket.close_connection()

    def stream_quote(self, exchange: str, symbol: str, max_age: float = 6.0):
        with self._stream_lock:
            row = dict(self._stream_cache.get((exchange, symbol)) or {})
        now = time.time()
        if row and self._stream_connected and -1.0 <= now-row["exchange_at"] <= max_age and 0 <= now-row["received_at"] <= max_age:
            return row
        return None
