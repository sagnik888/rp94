"""Market data from Zerodha Kite: quotes, depth, historical candles, instruments.

Two things here are genuine upgrades over the AngelOne path, not just translations:

1. Contracts are RESOLVED BY LOOKUP, never built by string formatting. AngelOne symbols
   were assembled as f"{name}{expiry}{strike}{CE|PE}", which is how the system spent
   2026-07-31 asking for SENSEX26JUL77700CE — an expiry that had already died — and
   collecting 887 AB4046 "symbol token not found" errors. Kite publishes the full
   contract master, so `resolve_option()` picks a real, live row or returns nothing.
   A contract that does not exist can no longer be requested.

2. Kite quotes carry real market depth. Every option entry on 2026-07-31 logged
   "bid=0.00 ask=0.00 usable=False reason=missing_bid_ask" and fell back to a blind
   LTP mark-up, because the AngelOne path never supplied a book. `get_depth()` returns
   the top bid/ask, so touch-quote execution has something true to anchor to.
"""
from __future__ import annotations

import threading
import time as _time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pytz
from loguru import logger
from data.kite_auth import is_kite_auth_failure, kite_auth_failure_reason

IST = pytz.timezone("Asia/Kolkata")

# System interval names -> Kite's.
_INTERVALS = {
    "1min": "minute",
    "3min": "3minute",
    "5min": "5minute",
    "15min": "15minute",
    "30min": "30minute",
    "60min": "60minute",
    "1hour": "60minute",
    "1day": "day",
}

# Index option roots live on the derivative segments; the index itself on the cash one.
INDEX_EXCHANGE = {
    "NIFTY": ("NFO", "NSE"),
    "BANKNIFTY": ("NFO", "NSE"),
    "FINNIFTY": ("NFO", "NSE"),
    "MIDCPNIFTY": ("NFO", "NSE"),
    "SENSEX": ("BFO", "BSE"),
    "BANKEX": ("BFO", "BSE"),
}


class KiteDataSource:
    """Read-only Kite market data. Holds no order rights."""

    def __init__(self, auth=None, kite=None) -> None:
        self._auth = auth
        self._kite = kite
        self._lock = threading.RLock()
        self._instruments: Dict[str, List[Dict[str, Any]]] = {}
        self._by_symbol: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._loaded_on: Optional[date] = None
        # Per-endpoint throttle cooldown. AngelOne had this (_mark_angel_rest_backoff and
        # the SharedApiRateLimiter stack); the Kite migration removed the broker and left
        # its rate-limit layer wired to dead code, so a 429 was swallowed into an empty
        # frame and the identical burst re-fired on the next scan tick, holding the
        # throttle open indefinitely.
        self._throttle_until: Dict[str, float] = {}
        self._throttle_failures: Dict[str, int] = {}
        self._historical_rest_last_success: float = 0.0
        self._historical_rest_last_failure: float = 0.0
        self._historical_rest_last_error_reason: str = ""
        self._historical_rest_failure_reason: str = ""
        self._historical_rest_failure_count: int = 0
        self._historical_rest_last_attempt: float = 0.0
        self._historical_rest_last_call_ok: Optional[bool] = None
        self._historical_rest_sequence: int = 0
        self._historical_rest_completed_sequence: int = 0
        self._historical_rest_lock = threading.RLock()

    @property
    def kite(self):
        auth = self._auth
        if auth is not None and hasattr(auth, "kite"):
            # The shared auth object is authoritative.  A locally pinned client must
            # disappear immediately when another component invalidates that session,
            # and a completed re-login must replace it before health/read calls run.
            try:
                shared = getattr(auth, "kite", None)
            except Exception:
                shared = None
            with self._lock:
                self._kite = shared
                return self._kite
        return self._kite

    @property
    def is_connected(self) -> bool:
        return self.kite is not None

    @staticmethod
    def _is_throttle(exc: Exception) -> bool:
        """True for a rate-limit refusal, as opposed to any other failure.

        Deliberately narrow. Backing off on the wrong exception starves the data path,
        which is a worse outcome than the hammering this exists to stop — so an
        unrecognised failure is NOT treated as a throttle.
        """
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(
            marker in text
            for marker in ("too many request", "rate limit", "429", "throttle")
        )

    def _throttled(self, context: str, now_ts: float) -> bool:
        return now_ts < float(self._throttle_until.get(context, 0.0) or 0.0)

    def _mark_throttled(self, context: str, now_ts: float) -> float:
        from config.settings import get_settings

        settings = get_settings()
        base = float(getattr(settings, "kite_rest_backoff_seconds", 1.0) or 1.0)
        ceiling = float(getattr(settings, "kite_rest_backoff_max_seconds", 8.0) or 8.0)
        failures = int(self._throttle_failures.get(context, 0)) + 1
        self._throttle_failures[context] = failures
        cooldown = min(max(0.1, ceiling), max(0.1, base) * (2 ** (failures - 1)))
        self._throttle_until[context] = now_ts + cooldown
        return cooldown

    def _clear_throttle(self, context: str) -> None:
        if context in self._throttle_failures:
            self._throttle_failures.pop(context, None)
            self._throttle_until.pop(context, None)

    def throttle_status(self) -> Dict[str, Any]:
        """What the health payload reports. The Angel equivalents it replaces were frozen
        at False/0 behind a permanently-None smart_api, so a throttled Kite feed showed a
        green pacing indicator while _call swallowed 429s into empty frames."""
        now_ts = _time.time()
        active = {
            ctx: round(until - now_ts, 2)
            for ctx, until in self._throttle_until.items()
            if until > now_ts
        }
        return {
            "kite_rest_backoff_active": bool(active),
            "kite_rest_backoff_contexts": active,
            "kite_rest_backoff_failures": sum(self._throttle_failures.values()),
        }

    def _begin_historical_rest_call(self, context: str) -> int:
        if context != "historical_data":
            return 0
        with self._historical_rest_lock:
            self._historical_rest_sequence += 1
            return self._historical_rest_sequence

    def _record_historical_rest_result(
        self,
        sequence: int,
        *,
        ok: bool,
        reason: str = "",
        observed_at: Optional[float] = None,
    ) -> None:
        if sequence <= 0:
            return
        stamp = _time.time() if observed_at is None else float(observed_at)
        with self._historical_rest_lock:
            if ok:
                self._historical_rest_last_success = max(
                    self._historical_rest_last_success, stamp
                )
            else:
                if stamp >= self._historical_rest_last_failure:
                    self._historical_rest_last_failure = stamp
                    self._historical_rest_last_error_reason = str(
                        reason or "historical REST failed"
                    )[:300]
            # A request that began before a newer one may finish later. It can add a
            # timestamp, but it must not overwrite the newer call's verdict.
            if sequence < self._historical_rest_completed_sequence:
                return
            self._historical_rest_completed_sequence = sequence
            self._historical_rest_last_attempt = stamp
            self._historical_rest_last_call_ok = bool(ok)
            if ok:
                self._historical_rest_failure_reason = ""
                self._historical_rest_failure_count = 0
            else:
                self._historical_rest_failure_reason = str(reason or "historical REST failed")[:300]
                self._historical_rest_failure_count += 1

    def _call(self, context: str, fn, default=None):
        historical_sequence = self._begin_historical_rest_call(context)
        if self._auth is not None:
            status_fn = getattr(self._auth, "status", None)
            if callable(status_fn):
                try:
                    status = status_fn() or {}
                    if status.get("auth_required") and getattr(self._auth, "kite", None) is None:
                        self._kite = None
                        self._record_historical_rest_result(
                            historical_sequence,
                            ok=False,
                            reason="authentication required",
                        )
                        return default
                except Exception:
                    pass
        api = self.kite
        if api is None:
            logger.debug(f"Kite {context} skipped: no authenticated session")
            self._record_historical_rest_result(
                historical_sequence,
                ok=False,
                reason="no authenticated session",
            )
            return default
        now_ts = _time.time()
        if self._throttled(context, now_ts):
            # Returning `default` is what the exception path already did on a throttle;
            # the difference is that the request is not sent, so the broker's window is
            # allowed to drain instead of being kept open by the retry.
            logger.debug(f"Kite {context} skipped: throttled for another "
                         f"{self._throttle_until[context] - now_ts:.1f}s")
            self._record_historical_rest_result(
                historical_sequence,
                ok=False,
                reason="historical REST throttled",
            )
            return default
        try:
            result = fn(api)
        except Exception as exc:
            # Pass the context: a bare "invalid token" from historical_data is Kite
            # refusing the instrument token, not the session (see is_kite_auth_failure).
            if is_kite_auth_failure(exc, context) and self._auth is not None:
                reason = kite_auth_failure_reason(context, exc)
                try:
                    self._auth.invalidate(reason, failed_client=api)
                except TypeError:
                    # Compatibility with lightweight test/dry-run auth facades.
                    self._auth.invalidate(reason)
                if self._kite is api:
                    self._kite = None
            self._record_historical_rest_result(
                historical_sequence,
                ok=False,
                reason=f"{type(exc).__name__}: {exc}",
            )
            if self._is_throttle(exc):
                cooldown = self._mark_throttled(context, now_ts)
                logger.warning(
                    f"Kite {context} throttled: {type(exc).__name__}: {exc} — "
                    f"backing off {cooldown:.1f}s"
                )
            else:
                logger.warning(f"Kite {context} failed: {type(exc).__name__}: {exc}")
            return default
        self._clear_throttle(context)
        self._record_historical_rest_result(historical_sequence, ok=True)
        return result

    def historical_rest_status(
        self,
        *,
        now_ts: Optional[float] = None,
        max_age_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Independent health for candle REST, never conflated with quote/ticks."""
        now = _time.time() if now_ts is None else float(now_ts)
        if max_age_seconds is None:
            try:
                from config.settings import get_settings

                max_age_seconds = float(
                    getattr(
                        get_settings(), "kite_historical_rest_max_age_seconds", 90.0
                    )
                    or 90.0
                )
            except Exception:
                max_age_seconds = 90.0
        max_age = max(0.0, float(max_age_seconds or 0.0))
        with self._historical_rest_lock:
            last_success = float(self._historical_rest_last_success or 0.0)
            last_error = float(self._historical_rest_last_failure or 0.0)
            last_attempt = float(self._historical_rest_last_attempt or 0.0)
            last_call_ok = self._historical_rest_last_call_ok
            failure_reason = str(self._historical_rest_failure_reason or "")
            last_error_reason = str(self._historical_rest_last_error_reason or "")
            failure_count = int(self._historical_rest_failure_count)
        success_age = max(0.0, now - last_success) if last_success > 0.0 else None
        error_age = max(0.0, now - last_error) if last_error > 0.0 else None
        attempt_age = max(0.0, now - last_attempt) if last_attempt > 0.0 else None
        available = self.kite is not None
        fresh = bool(
            available
            and
            last_call_ok is True
            and success_age is not None
            and success_age <= max_age
        )
        return {
            "kite_historical_rest_source": "kite_historical_rest",
            "kite_historical_rest_available": available,
            "kite_historical_rest_healthy": fresh,
            "kite_historical_rest_fresh": fresh,
            "kite_historical_rest_stale": bool(last_attempt > 0.0 and not fresh),
            "kite_historical_rest_last_call_ok": last_call_ok,
            "kite_historical_rest_max_age_seconds": max_age,
            "kite_historical_rest_last_attempt_at": last_attempt or None,
            "kite_historical_rest_last_attempt_age_seconds": (
                round(attempt_age, 2) if attempt_age is not None else None
            ),
            "kite_historical_rest_last_success_at": last_success or None,
            "kite_historical_rest_age_seconds": (
                round(success_age, 2) if success_age is not None else None
            ),
            "kite_historical_rest_last_error_at": last_error or None,
            "kite_historical_rest_last_error_age_seconds": (
                round(error_age, 2) if error_age is not None else None
            ),
            "kite_historical_rest_failure": failure_reason,
            "kite_historical_rest_last_error": last_error_reason,
            "kite_historical_rest_failures": failure_count,
        }

    # ── contract master ──────────────────────────────────────────────────────
    def load_instruments(self, exchanges: Tuple[str, ...] = ("NFO", "BFO", "NSE", "BSE"),
                         force: bool = False) -> int:
        """Cache Kite's contract dump. Refreshed once per day; contracts expire."""
        today = datetime.now(IST).date()
        with self._lock:
            if not force and self._loaded_on == today and self._instruments:
                return sum(len(rows) for rows in self._instruments.values())

        loaded: Dict[str, List[Dict[str, Any]]] = {}
        by_symbol: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for exchange in exchanges:
            rows = self._call(
                f"instruments({exchange})", lambda api, ex=exchange: api.instruments(ex), default=None
            )
            if rows is None:
                # A partial master is worse than none: a missing exchange looks
                # identical to "this contract does not exist" at the call site.
                logger.error(f"Kite instrument dump for {exchange} unavailable; keeping previous cache")
                return sum(len(r) for r in self._instruments.values())
            normalised = [self._normalise_instrument(row) for row in rows]
            loaded[exchange] = normalised
            for row in normalised:
                by_symbol[(row["tradingsymbol"].upper(), exchange)] = row

        with self._lock:
            self._instruments = loaded
            self._by_symbol = by_symbol
            self._loaded_on = today
        total = sum(len(rows) for rows in loaded.values())
        logger.info(f"Kite contract master loaded: {total} instruments across {', '.join(exchanges)}")
        return total

    @staticmethod
    def _normalise_instrument(row: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(row or {})
        expiry = row.get("expiry")
        if isinstance(expiry, datetime):
            expiry = expiry.date()
        elif isinstance(expiry, str) and expiry:
            try:
                expiry = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
            except ValueError:
                expiry = None
        row["expiry"] = expiry or None
        row["tradingsymbol"] = str(row.get("tradingsymbol") or "")
        row["name"] = str(row.get("name") or "").upper()
        row["instrument_type"] = str(row.get("instrument_type") or "").upper()
        try:
            row["strike"] = float(row.get("strike") or 0.0)
        except (TypeError, ValueError):
            row["strike"] = 0.0
        try:
            row["instrument_token"] = int(row.get("instrument_token") or 0)
        except (TypeError, ValueError):
            row["instrument_token"] = 0
        try:
            row["lot_size"] = int(row.get("lot_size") or 0)
        except (TypeError, ValueError):
            row["lot_size"] = 0
        return row

    def option_lot_size(self, underlying: str) -> Optional[int]:
        """Lot size the EXCHANGE currently uses for this index's options, or None.

        Position size is taken from config/instruments.json, a hand-maintained file, while
        this — the authoritative figure, refreshed daily with the contract master — is
        parsed into every row and then read by nobody. Exchanges revise index lot sizes
        periodically, and when they do the JSON goes stale silently: every order is then
        sized on the wrong multiple and the broker refuses it as an invalid quantity,
        which surfaces as an ordinary rejection with no hint of the cause.

        Reported rather than applied. Silently resizing positions because a file
        disagrees with a dump would be a worse failure than the one it fixes.
        """
        wanted = str(underlying or "").upper()
        if not wanted:
            return None
        sizes: Dict[int, int] = {}
        with self._lock:
            exchanges = list(self._instruments.items())
        for _exchange, rows in exchanges:
            for row in rows:
                if str(row.get("name") or "").upper() != wanted:
                    continue
                if str(row.get("instrument_type") or "").upper() not in {"CE", "PE"}:
                    continue
                size = int(row.get("lot_size") or 0)
                if size > 0:
                    sizes[size] = sizes.get(size, 0) + 1
        if not sizes:
            return None
        # Expiries mid-revision can carry two sizes at once; the most common one is the
        # size the bulk of the tradable chain uses.
        return max(sizes.items(), key=lambda item: item[1])[0]

    def lot_size_mismatches(self, configured: Dict[str, int]) -> List[Tuple[str, int, int]]:
        """(index, configured, exchange) for each index whose JSON lot size is wrong.

        An empty list means "no DISAGREEMENT was found" — which is not the same as
        "all four were checked". Ask lot_size_unresolved() for the difference.
        """
        out: List[Tuple[str, int, int]] = []
        for name, size in (configured or {}).items():
            try:
                mine = int(size or 0)
            except (TypeError, ValueError):
                continue
            actual = self.option_lot_size(name)
            if mine > 0 and actual and actual != mine:
                out.append((str(name), mine, int(actual)))
        return sorted(out)

    def lot_size_unresolved(self, configured: Dict[str, int]) -> List[str]:
        """Indices the contract master holds no option rows for, so nothing was checked.

        option_lot_size returns None when the master carries no CE/PE rows for a name,
        and the mismatch loop treats that falsy value as agreement. So a contract
        master that failed to load reported "clean" — the cross-check passing loudest
        exactly when it had no data. load_instruments keeps the previous cache on ANY
        exchange failure, and the checker's provider requests four exchanges where
        contract resolution requests two, so its master goes empty in cases where
        trading itself keeps working.
        """
        out: List[str] = []
        for name, size in (configured or {}).items():
            try:
                mine = int(size or 0)
            except (TypeError, ValueError):
                continue
            if mine > 0 and not self.option_lot_size(name):
                out.append(str(name))
        return sorted(out)

    def instrument_token(self, tradingsymbol: str, exchange: str) -> Optional[int]:
        with self._lock:
            row = self._by_symbol.get((str(tradingsymbol or "").upper(), str(exchange or "").upper()))
        token = int(row.get("instrument_token") or 0) if row else 0
        return token or None

    def expiries_for(self, index: str, on_or_after: Optional[date] = None) -> List[date]:
        """Live expiries only, soonest first. A dead contract cannot be selected."""
        index = str(index or "").upper()
        exchange = INDEX_EXCHANGE.get(index, ("NFO", "NSE"))[0]
        cutoff = on_or_after or datetime.now(IST).date()
        with self._lock:
            rows = list(self._instruments.get(exchange, []))
        found = {
            row["expiry"] for row in rows
            if row["name"] == index and row["instrument_type"] in {"CE", "PE"}
            and row["expiry"] is not None and row["expiry"] >= cutoff
        }
        return sorted(found)

    def resolve_option(
        self, index: str, strike: float, option_type: str,
        expiry: Optional[date] = None, on_or_after: Optional[date] = None,
    ) -> Optional[Dict[str, Any]]:
        """Find a real, live option contract. None when it does not exist.

        Returning None is the point: the caller must not fall back to inventing a
        symbol, which is what produced orders against expired contracts before.
        """
        index = str(index or "").upper()
        option_type = str(option_type or "").upper()
        exchange = INDEX_EXCHANGE.get(index, ("NFO", "NSE"))[0]
        if expiry is None:
            expiries = self.expiries_for(index, on_or_after=on_or_after)
            if not expiries:
                logger.error(f"No live {index} expiry in the Kite contract master")
                return None
            expiry = expiries[0]
        with self._lock:
            rows = list(self._instruments.get(exchange, []))
        target = round(float(strike), 2)
        for row in rows:
            if (
                row["name"] == index
                and row["instrument_type"] == option_type
                and row["expiry"] == expiry
                and abs(row["strike"] - target) < 0.01
            ):
                return row
        logger.warning(
            f"No Kite contract for {index} {int(target)} {option_type} expiring {expiry}"
        )
        return None

    def available_strikes(self, index: str, option_type: str = "CE",
                          expiry: Optional[date] = None) -> List[float]:
        index = str(index or "").upper()
        exchange = INDEX_EXCHANGE.get(index, ("NFO", "NSE"))[0]
        if expiry is None:
            expiries = self.expiries_for(index)
            if not expiries:
                return []
            expiry = expiries[0]
        with self._lock:
            rows = list(self._instruments.get(exchange, []))
        return sorted(
            row["strike"] for row in rows
            if row["name"] == index and row["instrument_type"] == str(option_type).upper()
            and row["expiry"] == expiry
        )

    # ── quotes ───────────────────────────────────────────────────────────────
    @staticmethod
    def _key(exchange: str, tradingsymbol: str) -> str:
        return f"{str(exchange).upper()}:{tradingsymbol}"

    def get_ltp(self, exchange: str, tradingsymbol: str) -> Optional[float]:
        key = self._key(exchange, tradingsymbol)
        payload = self._call("ltp", lambda api: api.ltp([key]), default=None)
        row = (payload or {}).get(key) if isinstance(payload, dict) else None
        if not isinstance(row, dict):
            return None
        try:
            price = float(row.get("last_price") or 0.0)
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    def get_ltps(self, instruments: List[Tuple[str, str]]) -> Dict[str, float]:
        """Batch LTP. Kite accepts up to 500 instruments per call."""
        keys = [self._key(ex, sym) for ex, sym in instruments]
        if not keys:
            return {}
        out: Dict[str, float] = {}
        for chunk in (keys[i:i + 500] for i in range(0, len(keys), 500)):
            payload = self._call("ltp batch", lambda api, c=chunk: api.ltp(c), default=None)
            for key, row in (payload or {}).items():
                try:
                    price = float((row or {}).get("last_price") or 0.0)
                except (TypeError, ValueError):
                    continue
                if price > 0:
                    out[key] = price
        return out

    def get_depth(self, exchange: str, tradingsymbol: str) -> Dict[str, Any]:
        """Top-of-book bid/ask. The AngelOne path never provided this at all.

        Returns usable=False with a reason rather than zeros, so the caller can tell
        "no book" apart from "book says zero" — that distinction is what made every
        2026-07-31 entry silently fall back to a blind LTP mark-up.
        """
        key = self._key(exchange, tradingsymbol)
        payload = self._call("quote", lambda api: api.quote([key]), default=None)
        row = (payload or {}).get(key) if isinstance(payload, dict) else None
        if not isinstance(row, dict):
            return {"bid": 0.0, "ask": 0.0, "usable": False, "reason": "quote unavailable",
                    "source": "kite", "last_price": 0.0}
        depth = row.get("depth") or {}
        buy = (depth.get("buy") or [{}])[0] if depth.get("buy") else {}
        sell = (depth.get("sell") or [{}])[0] if depth.get("sell") else {}

        def _f(value):
            try:
                return float(value or 0.0)
            except (TypeError, ValueError):
                return 0.0

        bid, ask = _f(buy.get("price")), _f(sell.get("price"))
        last = _f(row.get("last_price"))
        usable, reason = self._book_is_usable(bid, ask, last)
        return {
            "bid": bid,
            "ask": ask,
            "bid_qty": int(_f(buy.get("quantity"))),
            "ask_qty": int(_f(sell.get("quantity"))),
            "last_price": last,
            "oi": _f(row.get("oi")),
            "volume": _f(row.get("volume") or row.get("volume_traded")),
            "usable": usable,
            "reason": reason,
            "source": "kite",
        }

    # A book can be present, positive and uncrossed while still being nonsense to
    # execute against. Observed live on 2026-08-01: MIDCPNIFTY quoted ltp=421.55 with
    # bid=521.90 / ask=563.35 — a bid 24% ABOVE the last trade. Anchoring a marketable
    # limit to that ask would have paid ~33% over last traded price.
    MAX_SPREAD_PCT = 0.35          # wider than this is not a tradeable two-way market
    MAX_LTP_DEVIATION_PCT = 0.10   # last trade must sit near the quoted book

    @classmethod
    def _book_is_usable(cls, bid: float, ask: float, last: float):
        """(usable, reason) for a top-of-book quote."""
        if bid <= 0 or ask <= 0:
            return False, "empty book"
        if ask < bid:
            return False, "crossed book"
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return False, "empty book"
        spread_pct = (ask - bid) / mid
        if spread_pct > cls.MAX_SPREAD_PCT:
            return False, f"spread {spread_pct:.1%} too wide to execute against"
        if last > 0:
            low = bid * (1.0 - cls.MAX_LTP_DEVIATION_PCT)
            high = ask * (1.0 + cls.MAX_LTP_DEVIATION_PCT)
            if not (low <= last <= high):
                # Stale or one-sided book: trust the traded price over the quote.
                return False, (
                    f"last traded {last:.2f} sits outside the quoted book "
                    f"{bid:.2f}/{ask:.2f} — stale quote"
                )
        return True, ""

    # ── index futures activity ───────────────────────────────────────────────
    def index_futures_activity(self, index: str) -> Dict[str, Any]:
        """Near-month index futures volume and open interest, from the broker.

        Kite publishes NO volume for an index itself — an index is a computed number,
        not a traded instrument. Its FUTURES are traded, so they give a genuine,
        broker-sourced read on participation that can be cross-checked against the
        cash-market volume Fyers derives.

        The two are NOT interchangeable and the caller must not treat them as one
        series: this returns contracts traded in a single instrument, whereas an
        "index volume" is the summed share volume of every constituent. Measured on
        2026-07-31 the ratio was 0.006 for NIFTY and 0.001 for SENSEX — different
        quantities, not a scale factor.
        """
        index = str(index or "").upper()
        exchange = INDEX_EXCHANGE.get(index, ("NFO", "NSE"))[0]
        today = datetime.now(IST).date()
        with self._lock:
            rows = list(self._instruments.get(exchange, []))
        near = None
        for row in rows:
            if row["name"] != index or row["instrument_type"] != "FUT":
                continue
            if not row["expiry"] or row["expiry"] < today:
                continue
            if near is None or row["expiry"] < near["expiry"]:
                near = row
        if near is None:
            return {"available": False, "reason": f"no live {index} futures contract"}

        key = self._key(exchange, near["tradingsymbol"])
        payload = self._call("futures quote", lambda api: api.quote([key]), default=None)
        row = (payload or {}).get(key) if isinstance(payload, dict) else None
        if not isinstance(row, dict):
            return {"available": False, "reason": "futures quote unavailable",
                    "tradingsymbol": near["tradingsymbol"]}

        def _num(value):
            try:
                return float(value or 0.0)
            except (TypeError, ValueError):
                return 0.0

        return {
            "available": True,
            "tradingsymbol": near["tradingsymbol"],
            "instrument_token": near["instrument_token"],
            "expiry": near["expiry"],
            "volume": _num(row.get("volume") or row.get("volume_traded")),
            "oi": _num(row.get("oi")),
            "oi_day_high": _num(row.get("oi_day_high")),
            "oi_day_low": _num(row.get("oi_day_low")),
            "last_price": _num(row.get("last_price")),
            "unit": "contracts",
        }

    # ── option chain ─────────────────────────────────────────────────────────
    def get_option_chain(
        self, index: str, spot: float = 0.0, strikes_each_side: int = 15,
        expiry: Optional[date] = None,
    ) -> pd.DataFrame:
        """Live option chain with real open interest, straight from the broker.

        Emits the column names the intelligence layer already consumes (strike,
        call_oi, put_oi, call_volume, put_volume, call_ltp, put_ltp) so OITracker and
        the PCR path need no changes.

        OI here is the broker's own number for the contract being traded, rather than a
        third-party snapshot — the same source that fills the order. The *change* in OI
        is deliberately left at 0 with an explicit provenance marker: Kite reports a
        level, not a delta, and the existing snapshot store is what turns levels into
        changes. Fabricating a delta here would silently feed the OI signal.
        """
        index = str(index or "").upper()
        exchange = INDEX_EXCHANGE.get(index, ("NFO", "NSE"))[0]
        if expiry is None:
            expiries = self.expiries_for(index)
            if not expiries:
                logger.warning(f"No live {index} expiry for an option chain")
                return pd.DataFrame()
            expiry = expiries[0]

        strikes = self.available_strikes(index, "CE", expiry=expiry)
        if not strikes:
            return pd.DataFrame()
        if spot and spot > 0:
            centre = min(range(len(strikes)), key=lambda i: abs(strikes[i] - float(spot)))
        else:
            centre = len(strikes) // 2
        lo = max(0, centre - int(strikes_each_side))
        hi = min(len(strikes), centre + int(strikes_each_side) + 1)
        wanted = set(strikes[lo:hi])

        with self._lock:
            rows = list(self._instruments.get(exchange, []))
        contracts = {}
        for row in rows:
            if (
                row["name"] == index
                and row["expiry"] == expiry
                and row["instrument_type"] in {"CE", "PE"}
                and row["strike"] in wanted
            ):
                contracts[(row["strike"], row["instrument_type"])] = row["tradingsymbol"]
        if not contracts:
            return pd.DataFrame()

        keys = [self._key(exchange, sym) for sym in contracts.values()]
        quotes: Dict[str, Any] = {}
        for chunk in (keys[i:i + 250] for i in range(0, len(keys), 250)):
            payload = self._call("option chain quote", lambda api, c=chunk: api.quote(c), default=None)
            if payload:
                quotes.update(payload)

        def _num(row, key):
            try:
                return float((row or {}).get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        def _mid(row):
            """Top-of-book mid, or 0.0 when the book cannot be trusted.

            Implied volatility is solved from an option's price, so the input quality
            decides whether every Greek is real or fiction. Last-traded price can be
            minutes stale on an illiquid strike; a live mid is the standard estimate of
            fair value. Depth arrives in the same quote() call, so this is free.
            """
            depth = (row or {}).get("depth") or {}
            buy = (depth.get("buy") or [{}])[0] if depth.get("buy") else {}
            sell = (depth.get("sell") or [{}])[0] if depth.get("sell") else {}
            bid, ask = _num(buy, "price"), _num(sell, "price")
            usable, _ = self._book_is_usable(bid, ask, _num(row, "last_price"))
            return round((bid + ask) / 2.0, 2) if usable else 0.0

        records = []
        for strike in sorted(wanted):
            call = quotes.get(self._key(exchange, contracts.get((strike, "CE"), "")), {})
            put = quotes.get(self._key(exchange, contracts.get((strike, "PE"), "")), {})
            if not call and not put:
                continue
            records.append({
                "strike": strike,
                "call_mid": _mid(call),
                "put_mid": _mid(put),
                "call_oi": _num(call, "oi"),
                "put_oi": _num(put, "oi"),
                "call_oi_change": 0.0,
                "put_oi_change": 0.0,
                "call_volume": _num(call, "volume") or _num(call, "volume_traded"),
                "put_volume": _num(put, "volume") or _num(put, "volume_traded"),
                "call_ltp": _num(call, "last_price"),
                "put_ltp": _num(put, "last_price"),
            })
        frame = pd.DataFrame(records)
        if not frame.empty:
            frame.attrs["oi_change_source"] = "kite_quote_levels_only"
            frame.attrs["expiry"] = expiry
        return frame

    def put_call_ratio(self, chain: pd.DataFrame) -> float:
        """PCR by open interest. 0.0 when the chain carries no usable OI."""
        if chain is None or chain.empty:
            return 0.0
        calls = pd.to_numeric(chain.get("call_oi", 0), errors="coerce").fillna(0).sum()
        puts = pd.to_numeric(chain.get("put_oi", 0), errors="coerce").fillna(0).sum()
        return round(float(puts) / float(calls), 4) if calls > 0 else 0.0

    # ── historical ───────────────────────────────────────────────────────────
    def get_historical(
        self, instrument_token: int, interval: str, days_back: int = 15,
        oi: bool = False, to_date: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """OHLCV candles. Empty frame on failure — never partial or fabricated."""
        token = int(instrument_token or 0)
        if token <= 0:
            return pd.DataFrame()
        kite_interval = _INTERVALS.get(str(interval).lower())
        if not kite_interval:
            logger.warning(f"Unsupported candle interval for Kite: {interval}")
            return pd.DataFrame()
        end = to_date or datetime.now(IST)
        start = end - timedelta(days=max(1, int(days_back)))
        rows = self._call(
            "historical_data",
            lambda api: api.historical_data(token, start, end, kite_interval, oi=oi),
            default=None,
        )
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        if "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"])
            frame = frame.set_index("date").sort_index()
        for column in ("open", "high", "low", "close", "volume"):
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame
