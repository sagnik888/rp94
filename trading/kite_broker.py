"""Zerodha Kite Connect execution, behind the shared Broker interface.

Kite reports failures as typed exceptions rather than the error dicts AngelOne
returned, so the whole "which envelope is this?" problem disappears. What replaces it
is a classification that matters more, and gets its own table below: some Kite
exceptions prove the order never reached the exchange, and some prove nothing at all.

The invariant carried over from the AngelOne incident (2026-07-31): we may never
conclude "not placed" from a call we could not complete. A lost acknowledgement that we
treat as a clean failure becomes a duplicate live order on the retry.

Rows returned to the rest of the system are normalised to the AngelOne-ish field names
(`orderstatus`, `orderid`, `netqty`, `tradingsymbol`, `transactiontype`) that
trade_manager and the Broker base class already read, so this is a drop-in.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

from loguru import logger

from trading.account_execution_lease import AccountExecutionLease, lease_owner_description
from trading.broker import Broker
from trading.order_retry import BrokerOrderAmbiguous, BrokerOrderRejected
from data.kite_auth import is_kite_auth_failure, kite_auth_failure_reason

try:
    from kiteconnect import KiteConnect
    from kiteconnect.exceptions import (
        DataException,
        GeneralException,
        InputException,
        KiteException,
        NetworkException,
        OrderException,
        PermissionException,
        TokenException,
    )
    HAS_KITE = True
except ImportError:  # pragma: no cover - exercised only on a machine without the SDK
    KiteConnect = None
    HAS_KITE = False

    class KiteException(Exception): pass
    class DataException(KiteException): pass
    class GeneralException(KiteException): pass
    class InputException(KiteException): pass
    class NetworkException(KiteException): pass
    class OrderException(KiteException): pass
    class PermissionException(KiteException): pass
    class TokenException(KiteException): pass


# ── exception classification ─────────────────────────────────────────────────
# The distinction that protects live money:
#
#   AMBIGUOUS  the call did not complete, so the order MAY be resting at the exchange.
#              Never retried — a duplicate entry is worse than a missed one.
#   TERMINAL   Kite refused the request outright; retrying cannot change the outcome
#              (dead session, malformed order, permission denied).
#   RETRYABLE  the exchange/RMS rejected it for a condition that can clear on its own,
#              e.g. margin. The order is definitively not live, so a retry is safe.
_AMBIGUOUS_EXCEPTIONS = (NetworkException, DataException, GeneralException)
_TERMINAL_EXCEPTIONS = (TokenException, PermissionException, InputException)

# Broker-neutral product names used across this system -> Kite products.
_PRODUCT_MAP = {
    "CARRYFORWARD": "NRML",
    "NRML": "NRML",
    "INTRADAY": "MIS",
    "MIS": "MIS",
    "DELIVERY": "CNC",
    "CNC": "CNC",
}

# Kite order states that mean the order is still working at the exchange.
# "modify validation pending", "cancel pending", "amo req received" and "modified" were
# missing against the v3 lifecycle list (2026-08-16). An order sitting in any of them is
# live: a cancel that has not been confirmed has NOT removed the order.
_WORKING_STATUSES = {"open", "trigger pending", "open pending", "validation pending",
                     "put order req received", "modify pending",
                     "modify validation pending", "cancel pending",
                     "amo req received", "modified"}

# The only states in which an order is definitively finished.
_TERMINAL_STATUSES = {"complete", "cancelled", "rejected"}

# Exchange quantity-freeze limits per index, in UNITS (not lots). An order above the
# limit is rejected outright by the exchange, so a system that sizes up would fail with
# an opaque broker error rather than a diagnosable one.
#
# WARN ONLY, deliberately. Two reasons this must never refuse an order:
#  * the exchange is the real authority and these values are revised periodically, so a
#    stale entry here would block orders that are in fact perfectly valid;
#  * the spec's own remedy, the `autoslice` order parameter, does not exist in the
#    installed pykiteconnect 5.2.1 — its place_order signature ends at
#    market_protection — so passing it would raise TypeError on every entry.
# Currently unreachable in any case: max 4 lots caps the largest order at 480 units
# (MIDCPNIFTY), against a 4,200 limit. This exists so that raising the lot cap surfaces
# a named warning instead of an exchange rejection nobody can trace.
_FREEZE_QUANTITY_UNITS = {
    "NIFTY": 1800,
    "BANKNIFTY": 900,
    "FINNIFTY": 1800,
    "MIDCPNIFTY": 4200,
    "SENSEX": 1000,
    "BANKEX": 900,
}


def _warn_if_above_freeze_limit(symbol: str, qty: int) -> None:
    """Log when an order would breach the exchange freeze limit. Never blocks."""
    try:
        units = int(qty or 0)
    except (TypeError, ValueError):
        return
    if units <= 0:
        return
    name = str(symbol or "").upper()
    # Longest name first: "BANKNIFTY" must not be matched as "NIFTY".
    for index in sorted(_FREEZE_QUANTITY_UNITS, key=len, reverse=True):
        if name.startswith(index):
            limit = _FREEZE_QUANTITY_UNITS[index]
            if units > limit:
                logger.warning(
                    f"{symbol} order quantity {units} exceeds the {index} exchange "
                    f"freeze limit of {limit} units. The exchange will reject this "
                    "outright. Split it across orders, or reduce the lot count."
                )
            return


def _status_is_working(status: str) -> bool:
    """True when this order may still be live at the exchange.

    Anything that is not explicitly terminal counts as working, INCLUDING a status this
    build has never seen. The v3 docs describe the status list as open-ended, and both
    callers use this to decide whether to place another order: concluding "absent" from
    an unrecognised state is what puts a second entry, or a second SL-M, on a position
    that already has one. The duplicate is the expensive error, not the missed placement.

    An empty status is NOT working: `_normalise_order` always sets the key, so a blank
    one means the row carried no status at all rather than an unfamiliar one.
    """
    text = str(status or "").strip().lower()
    if not text:
        return False
    return text not in _TERMINAL_STATUSES


def classify_kite_exception(exc: Exception) -> str:
    """AMBIGUOUS / TERMINAL / RETRYABLE for one Kite SDK exception."""
    # Internal broker exceptions already encode whether placement was known to
    # occur.  Classify them before SDK types so a pre-dispatch session-change guard
    # is never mislabeled as a possibly-live order.
    if isinstance(exc, BrokerOrderAmbiguous):
        return "AMBIGUOUS"
    if isinstance(exc, BrokerOrderRejected):
        return "TERMINAL" if exc.terminal else "RETRYABLE"
    if isinstance(exc, _TERMINAL_EXCEPTIONS):
        return "TERMINAL"
    if isinstance(exc, OrderException):
        return "RETRYABLE"
    if isinstance(exc, _AMBIGUOUS_EXCEPTIONS):
        # A 4xx is Kite REFUSING the request, not failing to answer it (2026-08-16).
        #
        # pykiteconnect resolves the error class by NAME — connect.py:991 does
        # `getattr(ex, data["error_type"], ex.GeneralException)` — and it defines no
        # MarginException, HoldingException or UserException. So the single commonest
        # real rejection, insufficient funds, arrives as GeneralException and landed in
        # this AMBIGUOUS tuple: an order that provably never reached the exchange was
        # reported as "it may be live". The cost is not a duplicate (the direction is
        # fail-safe) but a leak — the entry-rate slot is held, the margin reservation is
        # never released, and the trade sits in open_trades as RECOVERY_REQUIRED
        # consuming one of max_concurrent_positions until a human clears it.
        #
        # The SDK only constructs these after parsing a complete HTTP response, and
        # every exception carries that status as `.code`. A 4xx therefore means the
        # request was received, understood and declined — definitively not at the
        # exchange, which is exactly what RETRYABLE means here. 5xx, a timeout, and the
        # SDK's default code=500 all stay AMBIGUOUS, so the safe direction is unchanged
        # for everything that genuinely did not complete.
        code = getattr(exc, "code", None)
        if isinstance(code, int) and 400 <= code < 500:
            return "RETRYABLE"
        return "AMBIGUOUS"
    # An unrecognised failure is treated as ambiguous on purpose: assuming the order
    # did not reach the market is the assumption that can duplicate a live position.
    return "AMBIGUOUS"


class KiteBroker(Broker):
    """Live execution through Zerodha Kite Connect."""

    requires_contract_master_validation = True
    requires_protective_stop_metadata_validation = True
    requires_entry_margin_preflight = True

    def __init__(self, kite=None, account_id: str = "", instance_id: str = "", auth=None):
        super().__init__()
        self.api = kite
        self._auth = auth           # KiteAuth, so a dead token can be invalidated
        self._order_lock = threading.Lock()
        self._last_call_at = 0.0
        # 0.34s (~2.9 req/s) until 2026-08-16, on a comment reading "Kite allows ~3 order
        # req/s". That is the HISTORICAL-CANDLE row of the rate-limit table; order
        # placement is documented at 10 req/s. Since every broker call is serialised
        # behind this floor, the wrong row throttled exits as well as reads to a third of
        # the allowance. 0.12s is ~8.3 req/s, keeping clear headroom under 10.
        self._min_call_interval = 0.12
        self._entry_order_times: deque = deque()
        # Cash this process has committed to orders Kite's own margin figure has not yet
        # deducted. Guarded by its own lock: preflight runs on the entry workers, and two
        # of them reading the same free cash is the exact over-commit this prevents.
        self._margin_lock = threading.Lock()
        self._margin_reservations: Dict[str, Dict[str, Any]] = {}
        self._margin_reservation_ttl_seconds = 90.0
        self._entry_kill_latched_until = 0.0
        # Entries are placed from background threads (one per trade), so the window
        # bookkeeping below is genuinely concurrent. Without this lock two threads can
        # both read len()==limit-1, both pass, and both place — overshooting the cap the
        # kill-switch exists to enforce.
        self._entry_rate_lock = threading.Lock()
        self._position_backoff_until = 0.0
        self._position_backoff_failures = 0
        self._execution_lease = (
            AccountExecutionLease(account_id, instance_id)
            if str(account_id or "").strip()
            else None
        )

    # ── session ──────────────────────────────────────────────────────────────
    def replace_api(self, kite) -> None:
        """Rebind to a freshly authenticated client after a daily re-login."""
        with self._order_lock:
            self.api = kite

    def _require_api(self, action: str):
        # Some recovery/test harnesses construct a broker around an already-owned
        # client without the KiteAuth coordinator.  Keep that supported while the
        # normal runtime still follows the shared auth object's client identity.
        auth = getattr(self, "_auth", None)
        if auth is not None and hasattr(auth, "kite"):
            shared_api = getattr(auth, "kite", None)
            if shared_api is None:
                self.api = None
            elif shared_api is not None and self.api is not shared_api:
                # Re-login may have completed before the provider callback reached
                # this broker. Adopt only the shared authenticated client; never keep
                # submitting through a stale private reference.
                self.api = shared_api
        if self.api is None:
            raise BrokerOrderRejected(
                f"{action} blocked: no authenticated Kite session. "
                "Complete the daily login at /api/kite_login.",
                terminal=True,
            )
        return self.api

    def _api_call(self, context: str, fn, timeout: float = 5.0, *, client=None):
        # `timeout` is advisory and deliberately NOT applied to the SDK client.
        # KiteConnect.timeout is a single mutable attribute on a client this process
        # SHARES with data/kite_market_data.py, which guards it with a different lock.
        # Setting it here per-call raced the data path: a quote in flight could inherit
        # this call's 2s budget instead of the client default, and the restore could put
        # back a value another thread had just changed. The SDK default (7s) applies to
        # every call; bound a slow broker at a higher level, not by mutating shared state.
        # The lock deliberately still spans the network round trip. Releasing it around
        # fn() would stop an emergency exit queueing behind a slow read, but pykiteconnect
        # drives one requests.Session that is not documented thread-safe, and this process
        # already SHARES that client with data/kite_market_data.py. Trading a bounded,
        # measurable queueing delay for concurrent use of a non-thread-safe client in the
        # order path is the wrong side of that bargain. The queueing was mostly the 0.34s
        # pacing bug above, not the network time; the real fix for the rest is a second
        # client dedicated to reads, which is a larger change than this audit warrants.
        with self._order_lock:
            initial_api = self._require_api(context)
            failed_api = client if client is not None else initial_api
            gap = time.monotonic() - self._last_call_at
            if gap < self._min_call_interval:
                time.sleep(self._min_call_interval - gap)
            # Pacing is deliberately inside the serialized order lane.  Re-check
            # authority *after* that sleep because a browser login can replace the
            # shared client while this request is waiting its turn.
            current_api = self._require_api(context)
            if current_api is not failed_api:
                raise BrokerOrderRejected(
                    f"{context} deferred: Kite session changed before the API call",
                    terminal=False,
                )
            auth = getattr(self, "_auth", None)
            guard = getattr(auth, "current_client_guard", None)
            dispatched = False
            try:
                if callable(guard):
                    # The auth RLock establishes a total order: either this exact
                    # client dispatches first, or the replacement installs first and
                    # this call fails locally without reaching the broker.
                    with guard(failed_api) as is_current:
                        if not is_current:
                            raise BrokerOrderRejected(
                                f"{context} deferred: Kite session changed before the API call",
                                terminal=False,
                            )
                        dispatched = True
                        return fn()
                # Compatibility for isolated test/recovery auth doubles. Production
                # KiteAuth always exposes current_client_guard.
                if auth is not None and hasattr(auth, "kite"):
                    if getattr(auth, "kite", None) is not failed_api:
                        raise BrokerOrderRejected(
                            f"{context} deferred: Kite session changed before the API call",
                            terminal=False,
                        )
                dispatched = True
                return fn()
            except Exception as exc:
                # The daily token died mid-session. Tell the auth layer so the
                # watchdog surfaces a re-login instead of every call failing blindly.
                if is_kite_auth_failure(exc):
                    auth = getattr(self, "_auth", None)
                    if auth is not None:
                        reason = kite_auth_failure_reason(context, exc)
                        try:
                            auth.invalidate(reason, failed_client=failed_api)
                        except TypeError:
                            auth.invalidate(reason)
                    # invalidate() nulls the client inside KiteAuth, but this broker
                    # holds a separate reference. Clear it so subsequent calls fail
                    # locally until replace_api() receives the fresh login client.
                    if self.api is failed_api:
                        self.api = None
                raise
            finally:
                if dispatched:
                    self._last_call_at = time.monotonic()

    # ── execution lease (unchanged semantics from the AngelOne broker) ────────
    def acquire_execution_lease(self) -> bool:
        lease = self._execution_lease
        return True if lease is None else bool(lease.acquire())

    def execution_lease_established(self) -> bool:
        """Whether an account lease could be built at all.

        A lease is only constructed when an account id is configured, and the
        accessors above answer True when there is none — so the control that
        stops two instances trading one Kite account is absent AND reports
        success. That is deliberate for non-REAL use, where there is no account
        to collide on and every test fixture builds a broker without one.

        REAL startup is the case where it matters, and it is the caller that can
        tell: main.py refuses REAL unless this is True. Kept as a separate
        predicate rather than folded into acquire_execution_lease so the
        non-REAL path is unaffected.
        """
        return self._execution_lease is not None

    def release_execution_lease(self) -> None:
        lease = self._execution_lease
        if lease is not None:
            lease.release()

    def has_execution_lease(self) -> bool:
        lease = self._execution_lease
        return True if lease is None else bool(lease.held)

    def execution_lease_block_reason(self) -> str:
        lease = self._execution_lease
        if lease is None or lease.held:
            return ""
        return "REAL broker account is owned by " + lease_owner_description(lease.owner_metadata)

    def _require_account_execution_lease(self, action: str) -> None:
        if self.has_execution_lease():
            return
        if self.acquire_execution_lease():
            logger.warning(
                f"Acquired the REAL account execution lease on demand before {action}."
            )
            return
        raise PermissionError(f"{action} blocked: {self.execution_lease_block_reason()}")

    def set_execution_authority(self, provider) -> None:
        """Wire the SYSTEM-mode authority every mutation here must satisfy.

        `provider` is a zero-argument callable returning True only while the system is
        genuinely in REAL mode. It is read at the moment of the send, which is the whole
        point: a job queued while REAL and dispatched after a switch to HISTORICAL asks
        again and is refused, where a check taken at enqueue time could not help.
        """
        self._authority_provider = provider

    def _authorize_mutation(self, action: str) -> None:
        """The single gate every broker mutation passes: session, lease, mode.

        These three used to be checked in different places, or not at all. The lease was
        asked at seven separate call sites; the system MODE was never asked here, so the
        adapter would happily transact for a caller that had stopped being allowed to
        ask -- a queued exit, a hot-swap window, a background reconcile on a timer.

        Strictness is keyed to the account lease rather than to a default, because the
        default is where this keeps going wrong. A broker with an established lease is
        by construction the production one (main.py builds it with the account id), and
        it MUST have an authority wired -- an unwired one is refused, not waved through.
        A broker without a lease is a fixture: 50-odd test files build them deliberately
        and none can reach a real account, so the mode question does not arise.
        """
        self._require_api(action)
        self._require_account_execution_lease(action)

        provider = getattr(self, "_authority_provider", None)

        # An explicitly wired authority is honoured ALWAYS, lease or no lease. The
        # lease-established test below decides only whether an authority is REQUIRED,
        # never whether a supplied one may be ignored -- skipping it for a blank
        # account id meant an adapter whose authority said False was still authorised,
        # which is a fail-open path in the one place built to fail closed.
        if callable(provider):
            try:
                permitted = bool(provider())
            except Exception as exc:
                raise BrokerOrderRejected(
                    f"{action} blocked: execution authority could not be read ({exc}).",
                    terminal=True,
                ) from exc
            if not permitted:
                raise BrokerOrderRejected(
                    f"{action} blocked: the system is not in REAL mode.",
                    terminal=True,
                )
            return

        if not self.execution_lease_established():
            return

        if not callable(provider):
            raise BrokerOrderRejected(
                f"{action} blocked: this broker holds a live account lease but no "
                "execution authority is wired; refusing rather than assuming REAL.",
                terminal=True,
            )
        try:
            permitted = bool(provider())
        except Exception as exc:
            # Cannot tell is not permission.
            raise BrokerOrderRejected(
                f"{action} blocked: execution authority could not be read ({exc}).",
                terminal=True,
            ) from exc
        if not permitted:
            raise BrokerOrderRejected(
                f"{action} blocked: the system is not in REAL mode.",
                terminal=True,
            )

    # ── runaway-entry kill switch ────────────────────────────────────────────
    def _entry_rate_allowed(self) -> bool:
        """Latch entries off once too many fire inside the configured window.

        Nothing else in the system bounds how many live entry orders a signal loop
        can send, so this is the only backstop against a runaway. It counts entries
        only — risk-reducing exits and protective stops are never gated by it,
        because blocking those would turn a runaway into an unprotected position.
        """
        from config.settings import get_settings

        settings = get_settings()
        window = max(1.0, float(getattr(settings, "entry_order_rate_window_seconds", 60.0) or 60.0))
        limit = max(1, int(getattr(settings, "max_entry_orders_per_minute", 6) or 6))
        latch = max(10.0, float(getattr(settings, "entry_order_kill_latch_seconds", 300.0) or 300.0))
        # Read-decide-record must be atomic, or concurrent entry threads each see room
        # under the cap and collectively exceed it.
        with self._entry_rate_lock:
            now = time.monotonic()
            if now < self._entry_kill_latched_until:
                return False
            while self._entry_order_times and now - self._entry_order_times[0] >= window:
                self._entry_order_times.popleft()
            if len(self._entry_order_times) >= limit:
                self._entry_kill_latched_until = now + latch
                logger.critical(
                    f"ENTRY ORDER RATE KILL-SWITCH: {len(self._entry_order_times)} orders "
                    f"inside {window:.0f}s; entries blocked for {latch:.0f}s."
                )
                return False
            self._entry_order_times.append(now)
            return True

    def _release_entry_reservation(self) -> None:
        """Give back the slot taken by an order that provably never reached the exchange.

        Entries retry through execute_order_with_retry — 7 attempts over ~36s, all inside
        the 60s window. Counting each ATTEMPT means a single RMS refusal on one trade
        consumes the whole default budget of 6 and latches every entry off for 300s,
        without a single order having reached the market. The cap is on live orders, so
        only a definitive non-placement releases; an ambiguous one keeps its slot,
        because that order may well be resting.
        """
        with self._entry_rate_lock:
            try:
                self._entry_order_times.pop()
            except IndexError:
                # Racing releases on the last element would otherwise raise IndexError
                # out of place_order's except-block, replacing the classified
                # BrokerOrderRejected/BrokerOrderAmbiguous the caller depends on.
                pass

    # ── mapping helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _exchange_for_symbol(symbol: str) -> str:
        sym = str(symbol or "").upper()
        return "BFO" if "SENSEX" in sym or "BANKEX" in sym else "NFO"

    @staticmethod
    def _product(product_type: str) -> str:
        return _PRODUCT_MAP.get(str(product_type or "").upper(), "NRML")

    @staticmethod
    def _tick_price(value: float) -> float:
        return round(max(0.05, round(float(value) / 0.05) * 0.05), 2)

    @staticmethod
    def _entry_limit_confidence_threshold() -> float:
        """The conviction floor for a LIMIT entry, normalised to the 0-1 scale.

        Every other confidence setting in this system is a fraction (0.5, 0.62, 0.7,
        0.72 ...) and `trade.confidence` is a fraction too — it is printed as
        `{confidence:.0%}` throughout the engine. This one shipped as 95.0, so
        `confidence >= threshold` was comparing at most 1.0 against 95.0 and was ALWAYS
        false: the LIMIT path was unreachable and every entry went out as MARKET,
        however strong the signal.

        A value above 1 is read as a percentage so an existing 95.0 in .env or in a
        persisted settings file keeps meaning "95%" rather than silently disabling the
        gate again.
        """
        try:
            from config.settings import get_settings

            raw = float(getattr(get_settings(), "entry_limit_confidence_threshold", 0.95) or 0.95)
        except Exception:
            return 0.95
        if raw <= 0:
            return 0.95
        return raw / 100.0 if raw > 1.0 else raw

    @staticmethod
    def _entry_limit_ttl_minutes() -> int:
        try:
            from config.settings import get_settings

            return max(0, int(getattr(get_settings(), "kite_entry_limit_ttl_minutes", 0) or 0))
        except Exception:
            return 0

    @staticmethod
    def order_tag(trade_id: str, leg: str = "ENTRY", attempt: int = 0) -> str:
        """A <=20 char Kite tag that maps ONE order leg back to one trade exactly.

        Kite echoes the tag on the order book, on trades, and on every postback. That
        turns order->trade matching from a heuristic (symbol + side + quantity, which
        is ambiguous the moment two trades sit on one strike) into an exact lookup —
        the precise ambiguity that stranded two trades on 2026-07-31.

        Trade ids look like T0005_MIDCPNIFTY_07311451 (25 chars), so the instrument is
        dropped: the counter and timestamp are what make it unique.

        Every tag is ALPHANUMERIC. The v3 docs are explicit — "An optional tag to apply
        to an order to identify it (alphanumeric, max 20 chars)" — and every entry
        carries one, so an invalid charset would fail the whole entry path. New entry
        placement therefore uses the alphanumeric form introduced on 2026-08-16. The
        reconciliation path still recognises the previous underscore-bearing attempt-0
        form as an owned alias, so an order resting across that upgrade is not mistaken
        for another trade's order. Every other leg gets a distinct U1-prefixed tag —
        without that, an exit and its entry would collide on one string and the lookup
        would return AMBIGUOUS exactly when it is needed most. The attempt code keeps a
        replaced entry distinct from the one it replaced, so a cancel/replace race
        cannot make two live orders share a tag.
        """
        raw = str(trade_id or "").strip()
        if not raw:
            return ""
        leg_code = {
            # ENTRY/EXIT/PROTECTIVE/STOP are the original four. The rest were rejected
            # outright, and _broker_order_tag swallows the ValueError into "" -- so a
            # scale-in ADD went to the exchange untagged while the code read as if it
            # were tagging it. Distinct exit legs matter for the same reason an exit and
            # its entry must not collide: a partial, a full and a recovery exit on one
            # contract are different orders and an exact lookup has to tell them apart.
            "ENTRY": "E", "EXIT": "X", "PROTECTIVE": "S", "STOP": "S",
            "ADD": "A", "PARTIAL_EXIT": "P", "FULL_EXIT": "F", "RECOVERY_EXIT": "R",
        }.get(str(leg or "").strip().upper())
        if not leg_code:
            raise ValueError(f"unsupported order-tag leg: {leg!r}")
        parts = raw.split("_")
        if leg_code == "E" and int(attempt or 0) == 0:
            # The underscore is gone as of 2026-08-16. The v3 docs are explicit —
            # "An optional tag to apply to an order to identify it (alphanumeric, max
            # 20 chars)" — and EVERY entry carries this tag, so if Kite enforces the
            # charset the whole entry path fails on the first live order.
            #
            # Placement must not revive the underscore-bearing legacy form. Read-side
            # reconciliation preserves that upgrade compatibility separately in
            # _own_entry_tags, without sending an invalid legacy tag on a new order.
            # Uniqueness is unchanged: the counter and timestamp are what carry it.
            candidate = f"{parts[0]}{parts[-1]}" if len(parts) >= 3 else raw
            return re.sub(r"[^A-Za-z0-9]", "", candidate)[:20]
        compact = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
        if not compact or len(compact) > 15:
            compact = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:15].upper()
        attempt_code = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"[max(0, int(attempt or 0)) % 36]
        return f"U1{leg_code}{attempt_code}{compact[:14]}"[:20]

    def inspect_order_by_tag(self, tag: str) -> Dict[str, Any]:
        """Find an order by its EXACT echoed tag. The point of tagging at all.

        Tags were already being written on entry orders and read by nothing, so an
        unacknowledged submission still had to be treated as unknowable — which is what
        every latch and withhold in trade_manager works around. This closes that loop:
        after a lost acknowledgement, ask the order book whether the tag exists.

        Tri-state on purpose, and ABSENT is never inferred from a failed read: an order
        book we could not fetch is UNKNOWN. Concluding "the order was never placed" from
        a scan that errored is precisely how a live position gets duplicated.
        """
        wanted = str(tag or "").strip()
        if not wanted:
            return {"state": "UNKNOWN", "order_id": "", "matches": [],
                    "reason": "order tag is empty"}
        rows = self._order_book()
        if rows is None:
            return {"state": "UNKNOWN", "order_id": "", "matches": [],
                    "reason": "order book could not be read"}
        matches = [row for row in rows if str(row.get("tag") or "").strip() == wanted]
        if not matches:
            return {"state": "ABSENT", "order_id": "", "matches": [],
                    "reason": "exact order tag not found"}
        if len(matches) != 1:
            return {"state": "AMBIGUOUS", "order_id": "", "matches": matches,
                    "reason": f"{len(matches)} orders share exact tag {wanted}"}
        row = matches[0]
        return {
            "state": "FOUND",
            "order_id": str(row.get("orderid") or ""),
            "status": str(row.get("orderstatus") or ""),
            "order": row,
            "matches": matches,
            "reason": "exact order tag found",
        }

    def inspect_entry_order_for_trade(self, trade_id: str, attempts: int = 8) -> Dict[str, Any]:
        """Find exactly one ENTRY order across every tag format this trade owns.

        Attempt-zero entries placed before the alphanumeric-tag upgrade used an
        underscore-bearing tag.  A process restarted after that upgrade has no order id
        to query, so looking only for the new tag makes a still-working legacy order
        invisible until a *new* entry is submitted.  Read all owned entry aliases from
        one order-book snapshot instead.  More than one match (including one current
        and one legacy tag) is deliberately AMBIGUOUS: choosing either could duplicate
        or cancel the wrong exposure.
        """
        try:
            tags = self._own_entry_tags(trade_id, attempts=attempts)
        except Exception:
            tags = set()
        if not tags:
            return {
                "state": "UNKNOWN", "order_id": "", "matches": [],
                "reason": "entry trade id has no recoverable tag aliases",
            }
        rows = self._order_book()
        if rows is None:
            return {
                "state": "UNKNOWN", "order_id": "", "matches": [],
                "reason": "order book could not be read",
            }
        matches = [row for row in rows if str(row.get("tag") or "").strip() in tags]
        if not matches:
            return {
                "state": "ABSENT", "order_id": "", "matches": [],
                "reason": "no owned entry tag found",
            }
        if len(matches) != 1:
            return {
                "state": "AMBIGUOUS", "order_id": "", "matches": matches,
                "reason": f"{len(matches)} orders match this trade's entry-tag aliases",
            }
        row = matches[0]
        return {
            "state": "FOUND",
            "order_id": str(row.get("orderid") or ""),
            "status": str(row.get("orderstatus") or ""),
            "tag": str(row.get("tag") or "").strip(),
            "order": row,
            "matches": matches,
            "reason": "owned entry tag found",
        }

    @staticmethod
    def _normalise_order(row: Dict[str, Any]) -> Dict[str, Any]:
        """Kite order row -> the field names the rest of this system reads."""
        row = dict(row or {})
        row.setdefault("orderid", row.get("order_id", ""))
        row.setdefault("orderstatus", str(row.get("status", "")).lower())
        row.setdefault("transactiontype", row.get("transaction_type", ""))
        row.setdefault("ordertype", row.get("order_type", ""))
        row.setdefault("producttype", row.get("product", ""))
        row.setdefault("filledshares", row.get("filled_quantity", 0))
        row.setdefault("averageprice", row.get("average_price", 0.0))
        row.setdefault("triggerprice", row.get("trigger_price", 0.0))
        row.setdefault("text", row.get("status_message", "") or "")
        return row

    def _raise_for(self, action: str, symbol: str, exc: Exception):
        """Translate a Kite exception into this system's order-failure contract."""
        kind = classify_kite_exception(exc)
        detail = f"{type(exc).__name__}: {exc}"
        if kind == "AMBIGUOUS":
            raise BrokerOrderAmbiguous(
                f"{action} for {symbol} did not complete, so the order may be live at "
                f"the exchange ({detail}). Reconcile before any further action."
            ) from exc
        logger.error(f"Kite refused {action} for {symbol}: {detail}")
        self._escalate_if_ip_blocked(detail)
        raise BrokerOrderRejected(detail, terminal=(kind == "TERMINAL")) from exc

    # Markers for the one refusal that is not about the order at all. Two shapes: an
    # address Kite rejected, and an app with an EMPTY allow-list. The second is the
    # nastier one — an empty list denies every order rather than allowing all, so
    # "clear the list" looks like a fix for a dynamic connection and is the opposite.
    _IP_BLOCK_MARKERS = (
        "is not allowed to place orders",
        "allowed ips",
        "no ips configured",
    )

    def _escalate_if_ip_blocked(self, detail: str) -> None:
        """Shout once when the broker is refusing EVERY order on account configuration.

        An IP-permission refusal is not an order problem: nothing about the symbol, size
        or price can change it, and it will refuse every order until a human edits the
        Kite developer console. It was logged at ERROR next to ordinary per-order
        refusals, so on 2026-08-04 three dead signals read as three unrelated failures
        rather than one blanket block, and the cause was only findable by grepping logs.

        Latched on the ADDRESS rather than a bare flag, so a changed egress IP -- the
        usual way this shows up -- escalates again instead of being swallowed by the latch.
        """
        text = str(detail or "").lower()
        if not any(marker in text for marker in self._IP_BLOCK_MARKERS):
            return
        # Two published shapes, and only the parenthesised one was matched until
        # 2026-08-16. This project's own 2026-08-04 incident read
        #   IP (2405:201:...:1b61) is not allowed to place orders for this app
        # but the Kite docs print it bare:
        #   IP xxx.xxx.xx.xxx is not allowed to place orders for this app
        # On the bare form `refused` came back empty, which sent the alert down the
        # "the app has NO allowed IPs configured" branch — telling the operator to add
        # an address when the real cause was an address that simply is not on the list.
        # The fallback accepts IPv4 and IPv6 alike: it must start and end on a hex
        # digit so the sentence's trailing punctuation is not swallowed.
        detail_text = str(detail)
        match = re.search(r"\bIP\s+\(([^)]+)\)", detail_text) or re.search(
            r"\bIP\s+([0-9A-Fa-f](?:[0-9A-Fa-f:.]*[0-9A-Fa-f])?)", detail_text
        )
        refused = match.group(1) if match else ""
        # The "no IPs configured" variant names no address at all, so the operator is
        # told to add an IP without being told WHICH. Look up what this machine actually
        # presents, so the alert carries the value to paste into the console.
        own = ""
        if not refused:
            try:
                from data.kite_auth import current_egress_ip

                own = current_egress_ip("v4")
            except Exception as exc:
                logger.debug(f"Egress lookup for the IP-block alert failed: {exc}")
        key = refused or own or "unknown"
        if getattr(self, "_ip_block_alerted", "") == key:
            return
        self._ip_block_alerted = key
        if refused:
            logger.critical(
                f"ORDERS BLOCKED AT THE BROKER: Kite is refusing EVERY order because "
                f"this machine's outbound address {refused} is not whitelisted for the "
                "app. No order can reach the exchange until it is added on the Kite "
                "developer console. Check the address family: a whitelisted IPv4 does "
                "NOT cover an IPv6 egress, and the address above is what Kite saw."
            )
        else:
            logger.critical(
                "ORDERS BLOCKED AT THE BROKER: the Kite app has NO allowed IPs "
                "configured, and an empty list DENIES every order rather than allowing "
                f"all. Add {own or 'this machine public IPv4'} on the Kite developer "
                "console. Keep kite_registered_ip in step with it so the readiness gate "
                "warns when this connection's address drifts."
            )

    # ── entry ────────────────────────────────────────────────────────────────
    def place_order(
        self,
        symbol: str,
        token: str,
        qty: int,
        side: str,
        product_type: str = "CARRYFORWARD",
        price: float = 0.0,
        # A fresh bid/ask lets option entries use a bounded marketable LIMIT instead of
        # exposing a thin contract to an unbounded market fill.
        touch_quote: Optional[Dict[str, Any]] = None,
        trade_id: str = "",
        confidence: float = 0.0,
        momentum_urgency: float = 0.0,
        # close_order and place_protective_stop both take an explicit tag; this one did
        # not, so _tag_kwargs -- which only passes a tag when the callee accepts one --
        # silently sent nothing here. Every place_order therefore went out tagged
        # ENTRY|0 from trade_id alone, whatever leg it actually was: the stale-entry
        # REPLACEMENT and the scale-in ADD both collided with the original entry's tag,
        # and the fixes that thought they were tagging them were no-ops against the real
        # broker. Left optional so existing callers are unchanged.
        tag: str = "",
    ) -> Optional[str]:
        try:
            self._authorize_mutation("entry order")
        except PermissionError as exc:
            raise BrokerOrderRejected(str(exc), terminal=True) from exc
        api = self._require_api("entry order")
        # Checked after the session, so a dead-token failure does not consume the
        # entry budget for an order that was never going to be sent.
        if not self._entry_rate_allowed():
            return None

        try:
            return self._submit_entry(
                api, symbol, token, qty, side, product_type, price, trade_id, confidence,
                momentum_urgency, tag, touch_quote,
            )
        except (BrokerOrderRejected, BrokerOrderAmbiguous):
            raise
        except Exception:
            # Anything unclassified between taking the slot and submitting means no
            # order went out, so the reservation must go back — otherwise a repeated
            # local failure latches the kill-switch with nothing at the exchange.
            self._release_entry_reservation()
            raise

    def _submit_entry(
        self, api, symbol: str, token: str, qty: int, side: str, product_type: str,
        price: float, trade_id: str, confidence: float,
        momentum_urgency: float = 0.0, tag: str = "",
        touch_quote: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        exchange = self._exchange_for_symbol(symbol)
        # Take the market unless the signal is strong enough to justify waiting for a
        # price. On 2026-07-31 every entry went out as a blind LTP mark-up limit
        # against an empty book — a limit is only meaningful when the conviction is
        # high enough that missing the fill beats paying the spread.
        # ...and unless the move is running away. The rule above weighs how GOOD the
        # setup is and never how fast the premium is leaving the bid, so a strong signal
        # into an accelerating move rested a limit behind the market. MACD acceleration
        # raises the bar a limit has to clear; it never lowers it.
        from config.settings import get_settings as _cfg
        from engine.order_pricing import limit_confidence_penalty

        threshold = self._entry_limit_confidence_threshold()
        threshold += limit_confidence_penalty(momentum_urgency, _cfg())
        use_limit = float(price or 0.0) > 0 and float(confidence or 0.0) >= threshold
        limit_price = float(price or 0.0)
        settings = _cfg()
        symbol_upper = str(symbol or "").upper()
        is_option = symbol_upper.endswith(("CE", "PE"))
        quote = touch_quote if isinstance(touch_quote, dict) else {}
        quote_supplied = bool(quote)
        quote_usable = bool(quote.get("usable", False))
        use_touch = bool(
            is_option
            and quote_usable
            and (
                getattr(settings, "execution_use_touch_quote", True)
                or getattr(settings, "execution_touch_quote_strict", True)
            )
        )
        if is_option and quote_supplied and not quote_usable and bool(
            getattr(settings, "execution_touch_quote_strict", True)
        ):
            self._release_entry_reservation()
            raise BrokerOrderRejected(
                f"Option entry refused: executable touch quote "
                f"{quote.get('reason') or 'unusable'} for {symbol}",
                terminal=True,
            )
        if use_touch:
            try:
                bid = float(quote.get("bid") or 0.0)
                ask = float(quote.get("ask") or 0.0)
                spread = ask - bid
                mid = (ask + bid) / 2.0
                spread_pct = spread / mid * 100.0 if mid > 0 else float("inf")
                max_pct = float(
                    getattr(settings, "option_touch_quote_max_spread_pct", 1.0) or 1.0
                )
                max_ticks = int(
                    getattr(settings, "option_touch_quote_max_spread_ticks", 40) or 40
                )
            except (TypeError, ValueError):
                bid = ask = 0.0
                spread = spread_pct = float("inf")
                max_pct, max_ticks = 1.0, 40
            invalid_book = bid <= 0 or ask <= 0 or ask < bid
            too_wide = (
                (max_pct > 0 and spread_pct > max_pct)
                or (max_ticks > 0 and spread > max_ticks * 0.05 + 1e-9)
            )
            if invalid_book or too_wide:
                if bool(getattr(settings, "execution_touch_quote_strict", True)):
                    self._release_entry_reservation()
                    detail = "invalid" if invalid_book else f"too wide ({spread_pct:.2f}%)"
                    raise BrokerOrderRejected(
                        f"Option entry refused: executable book {detail} for {symbol}",
                        terminal=True,
                    )
            else:
                extra_ticks = max(
                    0, int(getattr(settings, "option_touch_limit_extra_ticks", 1) or 0)
                )
                limit_price = (
                    ask + extra_ticks * 0.05
                    if str(side or "").upper() == "BUY"
                    else max(0.05, bid - extra_ticks * 0.05)
                )
                use_limit = True
        order_type = "LIMIT" if use_limit else "MARKET"
        _warn_if_above_freeze_limit(symbol, qty)
        params: Dict[str, Any] = {
            "variety": "regular",
            "exchange": exchange,
            "tradingsymbol": str(symbol),
            "transaction_type": str(side).upper(),
            "quantity": int(qty),
            "product": self._product(product_type),
            "order_type": order_type,
            "validity": "DAY",
        }
        if order_type == "MARKET":
            # Unbounded market fills in a thin option book are the cost of this policy;
            # Zerodha's automatic protection band is what makes it acceptable.
            params["market_protection"] = -1
        if order_type == "LIMIT":
            params["price"] = self._tick_price(limit_price)
            ttl = self._entry_limit_ttl_minutes()
            if ttl > 0 and str(exchange or "").upper() != "BFO":
                params["validity"] = "TTL"
                params["validity_ttl"] = ttl
        # An explicit tag names the LEG and attempt; trade_id alone can only ever
        # produce ENTRY|0, which is right for a first entry and wrong for everything
        # else that reaches this method.
        tag = str(tag or "") or self.order_tag(trade_id)
        if tag:
            params["tag"] = tag

        logger.info(
            f"PLACING REAL ORDER: {side} {qty} {symbol} on {exchange} | "
            f"Type: {order_type} | Price: {params.get('price', 0.0):.2f}"
        )
        try:
            order_id = self._api_call(
                "place entry order", lambda: api.place_order(**params), client=api
            )
        except Exception as exc:
            if classify_kite_exception(exc) != "AMBIGUOUS":
                # Refused outright: nothing is live, so this attempt must not count
                # against the runaway-entry budget it never used.
                self._release_entry_reservation()
            self._raise_for("entry order", symbol, exc)

        order_id = str(order_id or "")
        if not order_id or order_id == "None":
            # Kite returns an id on success, so an empty one is unexplained. Ask the
            # order book whether anything actually reached the exchange; only a
            # completed scan that finds nothing may be called a clean failure.
            # Ask the TAG first — this call wrote it a few lines above, and it is the
            # only exact identity available. inspect_recent_entry_order matches on
            # contract, side and quantity, so on a strike several trades share it can
            # hand back a DIFFERENT trade's order and this entry then adopts it.
            # inspect_order_by_tag exists for precisely this case ("after a lost
            # acknowledgement, ask the order book whether the tag exists") and is
            # already used for the protective leg and for exits — the entry path was
            # the one that still guessed.
            #
            # Additive: only a definite FOUND short-circuits. ABSENT and UNKNOWN fall
            # through to the contract scan exactly as before, so a tag the broker did
            # not echo cannot make a placed order look unplaced.
            if tag:
                try:
                    tagged = self.inspect_order_by_tag(tag)
                except Exception as exc:
                    logger.debug(f"Tag lookup failed for {symbol} ({tag}): {exc}")
                    tagged = {}
                if str(tagged.get("state") or "").upper() == "FOUND":
                    by_tag = str(tagged.get("order_id") or "")
                    if by_tag:
                        logger.warning(
                            f"Recovered entry order id {by_tag} for {symbol} by exact tag {tag}."
                        )
                        return by_tag

            resting = self.inspect_recent_entry_order(symbol, token, side, qty)
            state = str(resting.get("state") or "UNKNOWN").upper()
            if state == "ABSENT":
                self._release_entry_reservation()
                raise BrokerOrderRejected(
                    "Kite returned no order id and the order book confirms nothing was placed",
                    terminal=False,
                )
            if state == "FOUND":
                found = str(resting.get("order_id") or "")
                # The scan matches on contract + side + quantity and has no tag term, so
                # on a strike another trade also touched it can hand back a STRANGER'S
                # order -- which this entry would then adopt as its own: protection armed
                # for quantity it does not hold, and the true owner's fill row rewritten
                # (order_fills is INSERT OR REPLACE on order_id). The concurrency guard
                # does not close this, because it only inspects open_trades while Kite's
                # order book returns the whole day, including an earlier CLOSED trade.
                #
                # A row carrying a tag that is not one of OURS is positively somebody
                # else's. An UNTAGGED row stays adoptable -- that is the case this whole
                # fallback exists for, a broker that did not echo the tag back.
                if self._entry_row_is_foreign(resting, found, trade_id):
                    raise BrokerOrderAmbiguous(
                        f"Kite returned no order id for {symbol} and the only matching "
                        f"order in the book ({found}) carries another trade's tag. Not "
                        "adopting it; reconcile this contract before acting."
                    )
                logger.warning(f"Recovered entry order id {found} for {symbol} from the order book.")
                return found or None
            raise BrokerOrderAmbiguous(
                f"Kite returned no order id for {symbol} and the order-book scan was "
                f"inconclusive ({resting.get('reason') or 'unknown'})"
            )

        logger.success(f"REAL ORDER PLACED: {order_id} ({exchange})")
        return order_id

    # ── exit ─────────────────────────────────────────────────────────────────
    def close_order(
        self, symbol: str, token: str, qty: int, side: str,
        product_type: str = "CARRYFORWARD", price: float = 0.0,
        # Optional so simulation brokers and older call sites keep working. When set, an
        # exit whose acknowledgement is lost stops being unknowable — inspect_order_by_tag
        # can say whether it reached the exchange, instead of the caller having to latch
        # and wait for a human.
        tag: str = "",
    ) -> Optional[str]:
        self._authorize_mutation("exit order")
        api = self._require_api("exit order")
        _warn_if_above_freeze_limit(symbol, qty)
        try:
            order_id = self._api_call(
                "place exit order",
                lambda: api.place_order(
                    variety="regular",
                    exchange=self._exchange_for_symbol(symbol),
                    tradingsymbol=str(symbol),
                    transaction_type=str(side).upper(),
                    quantity=int(qty),
                    product=self._product(product_type),
                    order_type="MARKET",
                    validity="DAY",
                    # Exits are MARKET orders, so in a thin option book they can fill
                    # arbitrarily far from the touch. -1 asks Zerodha to apply its
                    # automatic protection band, capping that slippage. AngelOne had
                    # no equivalent; every exit was unbounded.
                    market_protection=-1,
                    **({"tag": str(tag)[:20]} if tag else {}),
                ),
                client=api,
            )
        except Exception as exc:
            # An exit that failed is live risk. Say so loudly, and keep the ambiguous
            # case ambiguous so nothing double-exits the position.
            kind = classify_kite_exception(exc)
            detail = f"{type(exc).__name__}: {exc}"
            logger.critical(f"Kite refused EXIT for {symbol} [{kind}]: {detail}")
            if kind == "AMBIGUOUS":
                raise BrokerOrderAmbiguous(
                    f"Exit for {symbol} did not complete; it may be live ({exc})"
                ) from exc
            # Until 2026-08-16 this returned None here and threw away the classification
            # it had just computed. Two costs. This was the ONLY order path that never
            # escalated an IP-permission block — place_order, place_protective_stop and
            # modify_protective_stop all do — so a block that first showed itself on an
            # exit stayed a per-order error line. And a TERMINAL refusal (dead session,
            # permission denied) was indistinguishable from a transient one, so the
            # caller retried the full attempt schedule against a broker that could only
            # keep saying no, with real risk open the whole time.
            # Safe to raise: all four call sites already catch BrokerOrderRejected
            # (trade_manager.py:2546, 3702, 5497, 5923) and 3703 states outright that
            # close_order "raises just like place_order".
            self._escalate_if_ip_blocked(detail)
            raise BrokerOrderRejected(detail, terminal=(kind == "TERMINAL")) from exc
        order_id = str(order_id or "")
        if not order_id:
            raise BrokerOrderAmbiguous(f"Kite exit for {symbol} returned no order id")
        return order_id

    # ── protective stop ──────────────────────────────────────────────────────
    def place_protective_stop(
        self, symbol: str, token: str, qty: int, side: str, trigger_price: float,
        product_type: str = "CARRYFORWARD", tag: str = "",
    ) -> Optional[str]:
        self._authorize_mutation("protective stop")
        api = self._require_api("protective stop")
        _warn_if_above_freeze_limit(symbol, qty)
        trigger = self._tick_price(trigger_price)
        try:
            order_id = self._api_call(
                "place protective stop",
                lambda: api.place_order(
                    variety="regular",
                    exchange=self._exchange_for_symbol(symbol),
                    tradingsymbol=str(symbol),
                    transaction_type=str(side).upper(),
                    quantity=int(qty),
                    product=self._product(product_type),
                    order_type="SL-M",
                    trigger_price=trigger,
                    validity="DAY",
                    # SL-M is the OTHER order type the spec's market-protection rule
                    # names, and this one was missing it (2026-08-16). When this stop
                    # triggers it becomes a market order into whatever book exists at
                    # the moment the trade is going against us — the single worst time
                    # to be unbounded. Same -1 (automatic band) as the market exit.
                    market_protection=-1,
                    **({"tag": str(tag)[:20]} if tag else {}),
                ),
                client=api,
            )
        except Exception as exc:
            if classify_kite_exception(exc) == "AMBIGUOUS":
                # Same rule as the entry: a second SL-M on the same position means one
                # of them fires against a flat book and opens a naked reversed leg.
                raise BrokerOrderAmbiguous(
                    f"Protective stop for {symbol} did not complete; it may be resting ({exc})"
                ) from exc
            logger.error(f"Kite rejected protective stop for {symbol}: {exc}")
            # An IP-permission refusal is not an order problem and will refuse every
            # order until a human edits the allow-list. This path returns None and the
            # position is left with no server-side stop, so it is the LAST place that
            # should stay quiet about the cause. The latch de-duplicates per address.
            self._escalate_if_ip_blocked(f"{type(exc).__name__}: {exc}")
            return None
        order_id = str(order_id or "")
        if not order_id or order_id == "None":
            # Kite returns an id on success, so an empty one is unexplained — NOT a
            # refusal. Returning None here is audit BLOCKER-1 (2026-07-24) verbatim:
            # the caller cannot tell it from a clean reject, re-places, and two SL-M
            # orders rest at the exchange. When both trigger, the second fires against
            # a flat book and opens a naked reversed leg.
            raise BrokerOrderAmbiguous(
                f"Protective stop for {symbol} returned no order id; it may be resting "
                "at the exchange. Reconcile before placing another."
            )
        return order_id

    # ── GTT: protection that outlives this process ───────────────────────────
    # A regular SL-M lives in the order book, so it dies with the session and cannot be
    # placed at all while the app is down. A GTT trigger sits on Zerodha's servers and
    # fires whether or not this machine is running. That is the direct answer to the
    # 2026-07-31 failure mode: positions left unprotected because the process could not
    # act — "emergency flatten unavailable" looped while real risk sat open.
    def place_gtt_stop(
        self, symbol: str, qty: int, side: str, trigger_price: float,
        last_price: float, product_type: str = "CARRYFORWARD",
    ) -> Optional[str]:
        """Create a single-leg GTT stop. Returns the trigger id, or None."""
        # A GTT is a real resting order on the account, so it needs the same lease as
        # every other write. Without this a process that does NOT own the account could
        # arm a trigger on top of the stop the true holder is managing, and both fire.
        self._authorize_mutation("GTT stop")
        api = self._require_api("GTT stop")
        trigger = self._tick_price(trigger_price)
        exchange = self._exchange_for_symbol(symbol)
        # The GTT leg is a LIMIT order at the trigger. Zerodha rejects MARKET legs, and
        # a limit AT the trigger can miss in a gap — so this complements, and does not
        # replace, in-session stop management.
        orders = [{
            "exchange": exchange,
            "tradingsymbol": str(symbol),
            "transaction_type": str(side).upper(),
            "quantity": int(qty),
            "order_type": "LIMIT",
            "product": self._product(product_type),
            "price": trigger,
        }]
        try:
            result = self._api_call(
                "place GTT stop",
                lambda: api.place_gtt(
                    trigger_type=api.GTT_TYPE_SINGLE,
                    tradingsymbol=str(symbol),
                    exchange=exchange,
                    trigger_values=[trigger],
                    last_price=float(last_price or trigger),
                    orders=orders,
                ),
                client=api,
            )
        except Exception as exc:
            # A GTT is a resting order on Zerodha's servers, so a call that did not
            # complete may still have created one. Reporting that as a clean refusal
            # leaves gtt_trigger_id empty and the next cycle arms a SECOND trigger:
            # one fires and exits, the other fires against a flat book and opens a
            # naked reversed leg. Ask the server what actually exists before deciding.
            if classify_kite_exception(exc) == "AMBIGUOUS":
                existing = self.find_gtt_stop(symbol)
                if str(existing.get("state") or "") == "FOUND":
                    found_id = str(existing.get("trigger_id") or "")
                    logger.warning(
                        f"Recovered GTT {found_id} for {symbol} after a lost "
                        "acknowledgement; not arming a second trigger."
                    )
                    return found_id or None
                if str(existing.get("state") or "") == "UNKNOWN":
                    raise BrokerOrderAmbiguous(
                        f"GTT for {symbol} did not complete and the trigger list could "
                        f"not be read ({exc}); it may be armed."
                    ) from exc
            logger.error(f"Kite refused GTT stop for {symbol}: {type(exc).__name__}: {exc}")
            return None
        trigger_id = str((result or {}).get("trigger_id") or "")
        if trigger_id:
            logger.success(
                f"GTT stop armed for {symbol} at {trigger:.2f} (id {trigger_id}); "
                "it survives a restart or an outage of this process."
            )
        return trigger_id or None

    def cancel_gtt_stop(self, trigger_id: str) -> bool:
        if not trigger_id:
            return True
        # The last account write without the lease, and the worst polarity of the set:
        # this STRIPS protection rather than adding or rewriting it.
        try:
            self._authorize_mutation("GTT cancellation")
        except PermissionError as exc:
            logger.error(f"GTT cancellation refused for {trigger_id}: {exc}")
            return False
        try:
            api = self._require_api("GTT cancellation")
        except BrokerOrderRejected:
            return False
        try:
            self._api_call(
                "delete GTT", lambda: api.delete_gtt(str(trigger_id)), client=api
            )
            return True
        except Exception as exc:
            # False on refusal, same polarity rule as cancel_order: a caller that reads
            # True will flatten on top of a still-armed trigger.
            logger.error(f"Kite refused GTT deletion for {trigger_id}: {exc}")
            return False

    def find_gtt_stop(self, symbol: str) -> Dict[str, Any]:
        """Tri-state lookup of an existing GTT for this contract."""
        try:
            api = self._require_api("GTT lookup")
        except BrokerOrderRejected:
            return {"state": "UNKNOWN", "trigger_id": "", "reason": "no session"}
        try:
            triggers = self._api_call(
                "list GTTs", lambda: api.get_gtts(), timeout=3.0, client=api
            )
        except Exception as exc:
            # Never NONE from a failed scan — that would authorise a duplicate trigger.
            return {"state": "UNKNOWN", "trigger_id": "", "reason": f"GTT scan failed: {exc}"}
        wanted = str(symbol or "").upper()
        for row in triggers or []:
            condition = (row or {}).get("condition") or {}
            if str(condition.get("tradingsymbol") or "").upper() != wanted:
                continue
            # "triggered" is FOUND, not absent (2026-08-16). A GTT firing is not a
            # fill: it places a LIMIT order that can rest unfilled or be rejected, and
            # the trigger row stays on Zerodha's books either way. Reading it as NONE
            # let _arm_gtt_fallback arm a SECOND trigger on a contract that already had
            # one — the same duplicate-protection hazard the SL-M path guards by name.
            status = str((row or {}).get("status") or "").lower()
            if status not in ("active", "triggered"):
                continue
            return {
                "state": "FOUND",
                "trigger_id": str(row.get("id") or ""),
                "reason": f"{status} GTT found",
                "trigger_values": condition.get("trigger_values") or [],
            }
        return {"state": "NONE", "trigger_id": "", "reason": "no active or triggered GTT"}

    def modify_protective_stop(
        self, order_id: str, symbol: str, token: str, qty: int, side: str,
        trigger_price: float, product_type: str = "CARRYFORWARD",
    ) -> bool:
        # Same lease as every other account write. SmartApiBroker required it here and
        # KiteBroker did not, so a second instance that did NOT own the account could
        # rewrite the trigger and quantity of the stop the true holder was managing —
        # and the holder's local protective_trigger would keep its own stale value, so
        # it would never notice or correct the divergence.
        self._authorize_mutation("protective stop modification")
        api = self._require_api("protective stop modify")
        try:
            self._api_call(
                "modify protective stop",
                lambda: api.modify_order(
                    variety="regular",
                    order_id=str(order_id),
                    quantity=int(qty),
                    order_type="SL-M",
                    trigger_price=self._tick_price(trigger_price),
                    # Carried on the modify too: Kite replaces the order's parameters
                    # rather than merging them, so omitting it here would strip the
                    # protection band off the stop the first time it is tightened.
                    market_protection=-1,
                ),
                client=api,
            )
            return True
        except Exception as exc:
            # Must be False on refusal: a True here records a trigger the exchange
            # never accepted, and every later tightening check trusts that value.
            logger.error(f"Kite refused protective-stop modify for {symbol}: {exc}")
            self._escalate_if_ip_blocked(f"{type(exc).__name__}: {exc}")
            return False

    # ── cancellation ─────────────────────────────────────────────────────────
    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        if not order_id:
            return True
        self._authorize_mutation("order cancellation")
        api = self._require_api("order cancellation")
        try:
            self._api_call(
                "cancel order",
                lambda: api.cancel_order(variety="regular", order_id=str(order_id)),
                client=api,
            )
            return True
        except Exception as exc:
            # False on refusal. emergency_flatten_trade() reads a True as proof the
            # SL-M is gone and market-exits on top of it.
            logger.error(f"Kite refused cancellation of {order_id}: {exc}")
            self._escalate_if_ip_blocked(f"{type(exc).__name__}: {exc}")
            return False

    # ── reads ────────────────────────────────────────────────────────────────
    def _order_book(self) -> Optional[List[Dict[str, Any]]]:
        try:
            api = self._require_api("order-book read")
        except BrokerOrderRejected:
            return None
        try:
            rows = self._api_call(
                "fetch order book", lambda: api.orders(), timeout=2.0, client=api
            )
        except Exception as exc:
            logger.warning(f"Kite order-book read failed: {exc}")
            return None
        if not isinstance(rows, list):
            return None
        return [self._normalise_order(row) for row in rows]

    def get_order_status(self, order_id: str) -> Optional[Dict]:
        if not order_id:
            return None
        for row in self._order_book() or []:
            if str(row.get("orderid") or "") == str(order_id):
                return row
        return None

    @staticmethod
    def _position_should_backoff(payload) -> bool:
        text = str(payload or "").lower()
        markers = (
            "too many requests",
            "exceeding access rate",
            "access rate",
            "rate limit",
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

    def get_positions(self) -> Optional[Dict]:
        """Normalised to {"data": [...]} with netqty, as the reconciler expects."""
        try:
            api = self._require_api("positions read")
        except BrokerOrderRejected:
            return None
        # Without this cooldown a throttled positions endpoint is re-hit on every
        # reconcile tick, which is what keeps the throttle alive.
        remaining = float(getattr(self, "_position_backoff_until", 0.0) or 0.0) - time.time()
        if remaining > 0:
            logger.debug(f"Skipping broker positions; backoff active for {remaining:.1f}s")
            return None
        try:
            book = self._api_call(
                "fetch positions", lambda: api.positions(), timeout=2.0, client=api
            )
        except Exception as exc:
            if self._position_should_backoff(exc):
                self._mark_position_backoff(exc)
            logger.error(f"Kite positions read failed: {exc}")
            return None
        self._position_backoff_failures = 0
        self._position_backoff_until = 0.0
        net = (book or {}).get("net", []) if isinstance(book, dict) else []
        rows = []
        for row in net or []:
            row = dict(row)
            row.setdefault("netqty", row.get("quantity", 0))
            # The fill-price sync in trade_manager reads the AngelOne field names.
            # Kite calls these buy_price/sell_price, so without this mapping the
            # lookup silently returns nothing and the local entry price is never
            # reconciled against the broker's actual average.
            row.setdefault("buyavgprice", row.get("buy_price", 0.0))
            row.setdefault("sellavgprice", row.get("sell_price", 0.0))
            row.setdefault("producttype", row.get("product", ""))
            # `ltp` was the one legacy name this mapping missed (2026-08-16). Kite sends
            # last_price, so every consumer reading position["ltp"] — the orphan row on
            # the dashboard among them — silently got 0.0.
            row.setdefault("ltp", row.get("last_price", 0.0))
            rows.append(row)
        return {"data": rows}

    @staticmethod
    def _quantity_could_match(row: Dict[str, Any], wanted_qty: int) -> bool:
        """False ONLY when the row's size is known and differs.

        Asymmetric on purpose. Adopting a differently-sized order books this trade
        against a stranger's position and leaves the difference unprotected — but
        answering "no match" from a size we could not read is the worse error, because
        it reaches the caller as ABSENT and re-places a possibly-live order. An
        unparseable size therefore stays adoptable.
        """
        if wanted_qty <= 0:
            return True
        raw = row.get("quantity")
        if raw is None or raw == "":
            return True
        try:
            return int(float(raw)) == wanted_qty
        except (TypeError, ValueError):
            return True

    def _own_entry_tags(self, trade_id: str, attempts: int = 8) -> set:
        """Every ENTRY tag this trade could have written, across replace attempts.

        A cancel/replace stamps the replacement with attempt = entry_replace_count + 1,
        so one trade legitimately owns more than one entry tag and a naive
        "tag != mine means foreign" test would disown its own earlier order. Attempt 0
        also owns the underscore-bearing tag written before the 2026-08-16 alphanumeric
        upgrade. It is a read-side alias only; order_tag() remains the placement format.
        """
        tags = set()
        for attempt in range(max(1, int(attempts))):
            try:
                tag = str(self.order_tag(trade_id, "ENTRY", attempt) or "")
            except Exception:
                continue
            if tag:
                tags.add(tag)
        raw = str(trade_id or "").strip()
        if raw:
            # Exact former attempt-0 implementation. Keep this deliberately separate
            # from order_tag(): Kite should never receive the legacy underscore on a
            # new placement, but a row already resting at the broker still belongs to
            # this trade and must survive reconciliation across the upgrade.
            parts = raw.split("_")
            legacy = f"{parts[0]}_{parts[-1]}" if len(parts) >= 3 else raw
            if legacy:
                tags.add(legacy[:20])
        return tags

    def _entry_row_is_foreign(
        self, resting: Dict[str, Any], order_id: str, trade_id: str,
    ) -> bool:
        """Does the matched order carry a tag that belongs to a DIFFERENT trade?

        False whenever the question cannot be answered — an untagged row, a row we cannot
        find, or a trade with no derivable tags. Only a positively foreign tag counts, so
        this can never turn a broker that does not echo tags into a refusal.
        """
        if not order_id or not str(trade_id or "").strip():
            return False
        row = next(
            (
                r for r in (resting.get("matches") or [])
                if str(r.get("orderid") or "") == str(order_id)
            ),
            None,
        )
        if not row:
            return False
        row_tag = str(row.get("tag") or "").strip()
        if not row_tag:
            return False
        own = self._own_entry_tags(trade_id)
        return bool(own) and row_tag not in own

    def inspect_recent_entry_order(
        self, symbol: str, token: str = "", side: str = "", quantity: int = 0,
    ) -> Dict[str, Any]:
        """Tri-state. ABSENT only ever comes from a scan that actually completed."""
        rows = self._order_book()
        if rows is None:
            return {
                "state": "UNKNOWN", "order_id": "", "matches": [],
                "reason": "order book could not be read",
            }
        if not symbol:
            # Cannot identify the contract, so nothing can be concluded. Falling through
            # would match no rows and answer ABSENT — "definitively not placed" — from a
            # scan that never had a chance to look.
            return {
                "state": "UNKNOWN", "order_id": "", "matches": [],
                "reason": "entry symbol is empty",
            }
        wanted_symbol = str(symbol or "").upper()
        wanted_side = str(side or "").upper()
        wanted_qty = max(0, int(quantity or 0))
        matches = [
            row for row in rows
            if str(row.get("tradingsymbol") or "").upper() == wanted_symbol
            and (not wanted_side or str(row.get("transactiontype") or "").upper() == wanted_side)
            and str(row.get("ordertype") or "").upper() not in {"SL", "SL-M"}
            and self._quantity_could_match(row, wanted_qty)
        ]
        if not matches:
            return {"state": "ABSENT", "order_id": "", "reason": "no such order", "matches": []}
        # Prefer a live/filled order over a rejected one when several exist.
        for row in matches:
            status = str(row.get("orderstatus") or "").lower()
            if _status_is_working(status) or status == "complete":
                return {
                    "state": "FOUND", "order_id": str(row.get("orderid") or ""),
                    "reason": f"order is {status}", "matches": matches,
                }
        return {
            "state": "ABSENT", "order_id": "",
            "reason": "only terminal non-fill orders found", "matches": matches,
        }

    def inspect_working_protective_stop(
        self, symbol: str, token: str, side: str, quantity: int = 0,
        product_type: str = "CARRYFORWARD", trigger_price: float = 0.0,
    ) -> Dict[str, Any]:
        rows = self._order_book()
        if rows is None:
            return {
                "state": "UNKNOWN", "order_id": "", "matches": [],
                "reason": "order book could not be read",
            }
        if not symbol:
            return {
                "state": "UNKNOWN", "order_id": "", "matches": [],
                "reason": "protective-order symbol is empty",
            }
        wanted_symbol = str(symbol or "").upper()
        wanted_side = str(side or "").upper()
        wanted_qty = max(0, int(quantity or 0))
        wanted_product = self._product(product_type) if product_type else ""
        working = [
            row for row in rows
            if str(row.get("tradingsymbol") or "").upper() == wanted_symbol
            and str(row.get("ordertype") or "").upper() in {"SL", "SL-M"}
            and _status_is_working(str(row.get("orderstatus") or ""))
            and (not wanted_side or str(row.get("transactiontype") or "").upper() == wanted_side)
        ]
        if not working:
            return {"state": "NONE", "order_id": "", "reason": "no working stop", "matches": []}

        # Split on metadata rather than taking working[0]. A caller that cancels the
        # first match and then acts as if the contract is clear will cancel ONE of two
        # resting stops and exit over the other — the duplicate-SL-M hazard this whole
        # subsystem exists to prevent. The AngelOne broker made these distinctions and
        # they were lost when it was deleted; T2 in trade_manager depends on them.
        wanted_token = str(token or "").strip()
        wanted_trigger = self._tick_price(trigger_price) if float(trigger_price or 0.0) > 0 else 0.0
        exact, conflicts, stale_trigger = [], [], []
        for row in working:
            # One rule for "is this row my size", shared with the entry scan. The
            # inline version here read a missing quantity as 0 and so as a CONFLICT,
            # while an unparseable string counted as a match — two contradictory
            # answers to the same question in one file.
            ok = self._quantity_could_match(row, wanted_qty)
            if ok and wanted_product:
                row_product = str(row.get("producttype") or "").upper()
                ok = not row_product or row_product == wanted_product
            if ok and wanted_token:
                row_token = str(row.get("instrument_token") or row.get("symboltoken") or "").strip()
                ok = not row_token or row_token == wanted_token
            if not ok:
                conflicts.append(row)
                continue
            # The stop is the right one but armed at the wrong level. That is neither a
            # clean FOUND (adopting it silently accepts a trigger the caller did not ask
            # for) nor a CONFLICT (it IS this position's stop, and modifying it is the
            # correct repair). Callers that pass no trigger_price never see this state.
            if wanted_trigger:
                row_trigger = row.get("triggerprice")
                try:
                    matches_level = abs(float(row_trigger) - wanted_trigger) <= 0.051
                except (TypeError, ValueError):
                    matches_level = True        # unreadable level is not proof of drift
                if not matches_level:
                    stale_trigger.append(row)
                    continue
            exact.append(row)

        if (
            len(exact) > 1
            or len(stale_trigger) > 1
            or (exact and (conflicts or stale_trigger))
            or (stale_trigger and conflicts)
        ):
            return {
                "state": "AMBIGUOUS", "order_id": "", "matches": working,
                "reason": f"{len(working)} working protective stops for this contract",
            }
        if stale_trigger:
            row = stale_trigger[0]
            return {
                "state": "FOUND_STALE_TRIGGER",
                "order_id": str(row.get("orderid") or ""),
                "trigger_price": row.get("triggerprice"),
                "reason": "this position's stop is armed at a different trigger",
                "matches": stale_trigger,
            }
        if exact:
            return {
                "state": "FOUND", "order_id": str(exact[0].get("orderid") or ""),
                "reason": "working protective stop found", "matches": exact,
            }
        if len(conflicts) > 1:
            return {
                "state": "AMBIGUOUS", "order_id": "", "matches": conflicts,
                "reason": "multiple working stops, none matching this position",
            }
        return {
            "state": "CONFLICT", "order_id": "", "matches": conflicts,
            "reason": "a working stop exists but its quantity/product does not match",
        }

    def find_working_protective_stop(
        self, symbol: str, token: str, side: str, quantity: int = 0,
        product_type: str = "",
    ) -> str:
        # product_type is forwarded rather than left to the inspector's CARRYFORWARD
        # default: with an INTRADAY/MIS position that default classifies a perfectly
        # good MIS stop as CONFLICT, which reads here as "no stop".
        result = self.inspect_working_protective_stop(
            symbol, token, side, quantity=quantity, product_type=product_type,
        )
        return str(result.get("order_id") or "") if result.get("state") == "FOUND" else ""

    # ── fills: fact instead of inference ─────────────────────────────────────
    def get_fill(self, order_id: str) -> Dict[str, Any]:
        """Actual fill for an order: quantity, average price, exchange timestamp.

        Replaces inferring a fill from the order book. `order_trades` returns the real
        executions, so entry price and slippage stop being estimates — which is what
        made the 2026-07-31 rows carry a phantom -80 P&L on trades that never filled.
        """
        if not order_id:
            return {"state": "UNKNOWN", "reason": "no order id"}
        try:
            api = self._require_api("fill lookup")
        except BrokerOrderRejected:
            return {"state": "UNKNOWN", "reason": "no session"}
        try:
            fills = self._api_call(
                "order trades", lambda: api.order_trades(str(order_id)),
                timeout=3.0, client=api,
            )
        except Exception as exc:
            # A failed lookup is UNKNOWN, never "not filled" — concluding no-fill from a
            # scan we could not complete is what strands or duplicates a live position.
            return {"state": "UNKNOWN", "reason": f"fill lookup failed: {exc}"}
        rows = [row for row in (fills or []) if row]
        if not rows:
            return {"state": "NO_FILL", "quantity": 0, "average_price": 0.0,
                    "reason": "no executions recorded"}
        total_qty, notional = 0, 0.0
        for row in rows:
            try:
                qty = float(row.get("quantity") or 0.0)
                price = float(row.get("average_price") or row.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            total_qty += int(qty)
            notional += qty * price
        return {
            "state": "FILLED",
            "quantity": total_qty,
            "average_price": round(notional / total_qty, 2) if total_qty else 0.0,
            "fills": len(rows),
            "last_fill_at": str(rows[-1].get("fill_timestamp") or rows[-1].get("exchange_timestamp") or ""),
            "reason": "",
        }

    def get_order_lifecycle(self, order_id: str) -> Dict[str, Any]:
        """Every state an order passed through, newest last.

        This is what settles an ambiguous acknowledgement definitively. The order-book
        scan can only answer "is it there now"; the history says whether it was ever
        accepted, and why it was rejected when it was.
        """
        if not order_id:
            return {"state": "UNKNOWN", "reason": "no order id", "transitions": []}
        try:
            api = self._require_api("order-history lookup")
        except BrokerOrderRejected:
            return {"state": "UNKNOWN", "reason": "no session", "transitions": []}
        try:
            history = self._api_call(
                "order history", lambda: api.order_history(str(order_id)),
                timeout=3.0, client=api,
            )
        except Exception as exc:
            return {"state": "UNKNOWN", "reason": f"history lookup failed: {exc}",
                    "transitions": []}
        rows = [row for row in (history or []) if row]
        if not rows:
            return {"state": "UNKNOWN", "reason": "no history returned", "transitions": []}
        transitions = [
            {
                "status": str(row.get("status") or "").upper(),
                "filled": row.get("filled_quantity"),
                "average_price": row.get("average_price"),
                "message": str(row.get("status_message") or ""),
                "at": str(row.get("order_timestamp") or ""),
            }
            for row in rows
        ]
        final = transitions[-1]["status"]
        rejection = next(
            (t["message"] for t in reversed(transitions) if t["status"] == "REJECTED" and t["message"]),
            "",
        )
        return {
            "state": final or "UNKNOWN",
            "reason": rejection,
            "transitions": transitions,
        }

    # ── margin ───────────────────────────────────────────────────────────────
    def available_funds(self) -> Optional[float]:
        """Free cash, or None when Kite will not answer.

        /user/margins answers "UNKNOWN_REQUEST Request not registered" outside market
        hours — Zerodha's RMS limits are not published until the session initialises.
        That is a normal condition, not a broken integration, so it returns None for
        the caller to interpret rather than raising.
        """
        try:
            api = self._require_api("margin-balance read")
        except BrokerOrderRejected:
            return None
        try:
            funds = self._api_call(
                "fetch margins", lambda: api.margins(), timeout=2.0, client=api
            )
        except Exception as exc:
            logger.debug(f"Kite margins unavailable: {exc}")
            return None
        equity = (funds or {}).get("equity") or {}
        if not equity and isinstance(funds, dict) and "available" in funds:
            equity = funds
        available = (equity.get("available") or {})
        # ABSENT and ZERO are different answers and used to be conflated (2026-08-16).
        # `if value:` is a truthiness test, so a live_balance of exactly 0.0 — the state
        # of a fully deployed account, i.e. precisely when the funds gate must bite —
        # fell through to `cash` and then to `opening_balance`. Neither of those
        # decreases as margin is consumed (`opening_balance` is the day-start figure;
        # utilisation is reported separately under `utilised.debits`), so the gate read
        # back the morning's balance and passed. Only a MISSING key may fall through.
        for key in ("live_balance", "cash", "opening_balance"):
            raw = available.get(key)
            if raw is None or raw == "":
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        try:
            return float(equity.get("net") or 0.0)
        except (TypeError, ValueError):
            return None

    def required_entry_margin(
        self, symbol: str, qty: int, side: str, product_type: str = "CARRYFORWARD",
        price: float = 0.0,
    ) -> Optional[float]:
        """Exact margin Kite will charge for this order, via /margins/orders.

        `price` (2026-08-16): when the entry is going out as a LIMIT, price the margin
        request the same way. Previously this always asked Kite to price a MARKET order
        with no price field, so the figure the funds gate used was Kite's own LTP-derived
        estimate rather than the debit the LIMIT will actually incur. Omitted or zero
        keeps the MARKET form, which is right for a market entry.
        """
        try:
            api = self._require_api("entry-margin quote")
        except BrokerOrderRejected:
            return None
        limit_price = 0.0
        try:
            limit_price = float(price or 0.0)
        except (TypeError, ValueError):
            limit_price = 0.0
        payload = [{
            "exchange": self._exchange_for_symbol(symbol),
            "tradingsymbol": str(symbol),
            "transaction_type": str(side).upper(),
            "variety": "regular",
            "product": self._product(product_type),
            "order_type": "LIMIT" if limit_price > 0 else "MARKET",
            "quantity": int(qty),
        }]
        if limit_price > 0:
            payload[0]["price"] = self._tick_price(limit_price)
        try:
            rows = self._api_call(
                "order margins", lambda: api.order_margins(payload),
                timeout=3.0, client=api,
            )
        except Exception as exc:
            logger.warning(f"Kite order-margin preflight failed for {symbol}: {exc}")
            return None
        if not rows:
            return None
        try:
            raw = (rows[0] or {}).get("total")
        except (TypeError, IndexError):
            return None
        if raw is None or raw == "":
            return None
        try:
            total = float(raw)
        except (TypeError, ValueError):
            return None
        # A zero total is an UNPRICED order, not a free one: buying an option always
        # costs at least the premium, so Kite answering 0 means it did not price this.
        # `or 0.0` used to turn that into a real requirement of 0.0, and preflight then
        # computed committed=0.0, passed unconditionally and stored a zero-value
        # reservation — the funds gate silently disabled instead of degrading to the
        # MARGIN_UNVERIFIED state that exists for exactly this case (2026-08-16).
        if total <= 0.0:
            return None
        return total

    @staticmethod
    def _entry_margin_refusal_cache_key(
        symbol: str, qty: int, side: str, product_type: str, price: float
    ) -> tuple:
        return (
            str(symbol or "").upper(), int(qty or 0), str(side or "BUY").upper(),
            str(product_type or "").upper(), round(float(price or 0.0), 2),
        )

    def _cache_entry_margin_refusal(self, key: tuple, verdict: Dict[str, Any]) -> None:
        """Remember only a definitive shortfall for the scanner's non-blocking gate."""
        if not isinstance(verdict, dict) or verdict.get("ok", True):
            return
        cache = getattr(self, "_entry_margin_refusal_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._entry_margin_refusal_cache = cache
        cache[key] = {"at": time.monotonic(), "verdict": dict(verdict)}

    def cached_entry_margin_refusal(self, **kwargs) -> Optional[Dict[str, Any]]:
        """Return a very recent exact funds refusal without performing I/O.

        This is deliberately one-sided.  A cached approval is not authority to spend,
        because account funds and in-flight reservations can change immediately.  A
        cached refusal only avoids repeating a known doomed entry while the background
        preflight retains the authoritative check and reservation.
        """
        try:
            key = self._entry_margin_refusal_cache_key(
                str(kwargs.get("symbol") or ""),
                int(kwargs.get("qty") or kwargs.get("quantity") or 0),
                str(kwargs.get("side") or "BUY"),
                str(kwargs.get("product_type") or "CARRYFORWARD"),
                float(kwargs.get("price") or 0.0),
            )
        except (TypeError, ValueError):
            return None
        record = getattr(self, "_entry_margin_refusal_cache", {}).get(key)
        if not isinstance(record, dict) or time.monotonic() - float(record.get("at", 0.0) or 0.0) > 2.0:
            return None
        verdict = record.get("verdict")
        return dict(verdict) if isinstance(verdict, dict) and not verdict.get("ok", True) else None

    def preflight_entry_margin(self, **kwargs) -> Dict[str, Any]:
        """Compare the exact required margin against free cash, where both are known.

        Uses /margins/orders for the requirement rather than deriving it: it is the
        number Kite actually charges, premium and charges included. Free cash comes
        from /user/margins, which is frequently unavailable outside market hours — so a
        missing balance degrades to a stated limitation instead of blocking entries on
        a Zerodha endpoint quirk.
        """
        try:
            self._require_api("entry-margin preflight")
        except BrokerOrderRejected:
            return {"ok": False, "reservation_id": "", "basis": "NO_SESSION",
                    "limitation": "no authenticated Kite session"}

        symbol = str(kwargs.get("symbol") or "")
        qty = int(kwargs.get("qty") or kwargs.get("quantity") or 0)
        side = str(kwargs.get("side") or "BUY")
        product_type = str(kwargs.get("product_type") or "CARRYFORWARD")
        # Accepted from the caller and silently dropped until 2026-08-16, so the gate
        # priced a MARKET order even when the entry was about to go out as a LIMIT.
        try:
            price = float(kwargs.get("price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        reserve = bool(kwargs.get("reserve", True))
        cache_key = self._entry_margin_refusal_cache_key(
            symbol, qty, side, product_type, price
        )

        required = (
            self.required_entry_margin(symbol, qty, side, product_type, price=price)
            if symbol and qty > 0 else None
        )
        available = self.available_funds()

        if required is None:
            return {"ok": True, "reservation_id": "", "basis": "MARGIN_UNVERIFIED",
                    "available": available, "required": None,
                    "limitation": "Kite did not return an order margin; entry not verified"}
        if available is None:
            # Failing OPEN here is deliberate and correct outside market hours:
            # /user/margins answers "Request not registered" until Zerodha's RMS
            # publishes the day's limits, and blocking every entry on a broker quirk is
            # worse than proceeding with the requirement known.
            #
            # DURING market hours the same silence is not a quirk. It means the funds
            # gate is inert at precisely the moment it is supposed to bite, and the
            # first symptom is an RMS rejection on a live order. Detected here rather
            # than left to someone to check at a particular minute (2026-08-16) — the
            # honest trigger is "an entry is being priced and we cannot see the cash",
            # not a clock reading.
            from datetime import datetime as _dt, time as _t

            import pytz as _pytz

            _now = _dt.now(_pytz.timezone("Asia/Kolkata"))
            _in_session = _now.weekday() < 5 and _t(9, 15) <= _now.time() <= _t(15, 30)
            if _in_session and not getattr(self, "_funds_gate_inert_alerted", False):
                # Latched: one alert per process, not one per entry attempt.
                self._funds_gate_inert_alerted = True
                logger.critical(
                    "FUNDS GATE INERT: /user/margins is not answering during market "
                    "hours, so free cash is unknown and every entry preflight is "
                    "passing unchecked. Order margins still price correctly, so the "
                    "requirement is known — what is missing is the balance to check it "
                    "against. Expect RMS rejections rather than a clean refusal if the "
                    "account is short."
                )
            return {"ok": True, "reservation_id": "",
                    "basis": "FUNDS_UNCHECKED_IN_SESSION" if _in_session else "REQUIREMENT_ONLY",
                    "available": None, "required": required,
                    "limitation": (
                        "free cash unavailable DURING market hours — the funds gate is "
                        "not checking anything; requirement known but unverified"
                        if _in_session else
                        "free cash unavailable (RMS limits are not published outside "
                        "market hours); requirement known but unchecked"
                    )}

        # Reserve against CONCURRENT entries. Without this, two entry workers reading the
        # same free cash both pass and the account over-commits: /user/margins does not
        # reflect an order that was placed moments ago, so the second check sees money the
        # first has already spent. The reservation is this process's own memory of what it
        # has committed but not yet seen deducted.
        #
        # reservation_id and release_entry_margin_reservation were already declared on the
        # base Broker (broker.py:173-183) and returned "" here — the contract was designed
        # and never implemented. entry_margin_buffer_pct (settings.py:423, with a 0-100
        # validator) and requires_entry_margin_preflight (line 103 of this file) were both
        # likewise sitting unread.
        buffer_pct = self._entry_margin_buffer_pct()
        committed = required * (1.0 + buffer_pct / 100.0)
        with self._margin_lock:
            self._expire_margin_reservations()
            outstanding = sum(r["amount"] for r in self._margin_reservations.values())
            spendable = available - outstanding
            if spendable < committed:
                verdict = {
                    "ok": False, "reservation_id": "", "basis": "KITE_ORDER_MARGIN",
                    "available": available, "required": required,
                    "reserved": outstanding,
                    "limitation": (
                        f"insufficient funds: {committed:,.2f} required "
                        f"(incl {buffer_pct:.0f}% buffer), {available:,.2f} available"
                        + (f", {outstanding:,.2f} already reserved by in-flight entries"
                           if outstanding else "")
                    ),
                }
                self._cache_entry_margin_refusal(cache_key, verdict)
                return verdict
            reservation_id = ""
            if reserve:
                reservation_id = f"{symbol}:{time.time():.6f}"
                self._margin_reservations[reservation_id] = {
                    "amount": committed, "at": time.time(), "symbol": symbol,
                }
        return {
            "ok": True,
            "reservation_id": reservation_id,
            "basis": "KITE_ORDER_MARGIN",
            "available": available,
            "required": required,
            "reserved": outstanding,
            "limitation": "",
        }

    def _entry_margin_buffer_pct(self) -> float:
        try:
            from config.settings import get_settings

            value = float(getattr(get_settings(), "entry_margin_buffer_pct", 10.0) or 0.0)
        except Exception:
            return 10.0
        return min(100.0, max(0.0, value))

    def _expire_margin_reservations(self) -> None:
        """Drop reservations older than the TTL. Caller holds _margin_lock.

        A reservation only covers the window between placing an order and Kite's own
        margin figure reflecting it. Without expiry, an entry that died in a way no
        release path caught would hold cash hostage for the rest of the session — which
        would turn a safety feature into a self-inflicted trading halt.
        """
        cutoff = time.time() - self._margin_reservation_ttl_seconds
        for key in [k for k, r in self._margin_reservations.items() if r["at"] < cutoff]:
            logger.warning(
                f"Margin reservation {key} expired after "
                f"{self._margin_reservation_ttl_seconds:.0f}s without release."
            )
            self._margin_reservations.pop(key, None)

    def release_entry_margin_reservation(self, reservation_id: str) -> None:
        """Give the cash back once the entry is definitively not going to consume it.

        Deliberately NOT called on an ambiguous acknowledgement: an order that may be
        live must keep holding its margin, or the next entry spends money this one is
        about to.
        """
        key = str(reservation_id or "")
        if not key:
            return
        with self._margin_lock:
            if self._margin_reservations.pop(key, None) is not None:
                logger.debug(f"Released margin reservation {key}")
