"""Zerodha Kite Connect session lifecycle.

Kite cannot be logged into with an id and password. The only supported flow is:

    1. send the operator to login_url()  (kite.zerodha.com/connect/login?v=3&api_key=…)
    2. they authenticate + 2FA in a browser, Zerodha redirects back with request_token
    3. complete_login(request_token) exchanges it for an access_token, using
       checksum SHA-256(api_key + request_token + api_secret)

The access token expires every morning, so this is a once-per-trading-day manual step.
The account password is deliberately never accepted, stored, or used anywhere here:
Kite has no password-based API login, so holding one would be pure liability.

Mirrors the shape of the existing Fyers auth helpers (get_fyers_auth_status /
get_fyers_auth_url) so the dashboard and the runtime watchdog can treat both the same.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import pytz
from loguru import logger

# ── force Kite traffic onto IPv4 ────────────────────────────────────────────
# Kite validates the TCP source address against the app's allowed-IP list and refuses
# EVERY order from an address that is not on it, while leaving reads working — so the
# failure looks like "the strategy stopped trading", not "the network is wrong".
#
# This machine has both families, and api.kite.trade resolves to IPv6 FIRST, so orders
# left on the IPv6 address (2026-08-04: three signals dead, 12:36-12:38). A whitelisted
# IPv4 does not cover that. Worse, an IPv6 egress is usually a PRIVACY/temporary address
# that the OS rotates on its own schedule, so it cannot be usefully whitelisted at all.
#
# Pinning only the Kite hostnames keeps every other integration (Fyers, Yahoo) untouched.
_KITE_IPV4_ONLY_HOSTS = {"api.kite.trade", "kite.zerodha.com", "kite.trade"}
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_IPV4_PIN_INSTALLED = False


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == 0 and str(host or "").lower() in _KITE_IPV4_ONLY_HOSTS:
        family = socket.AF_INET
    return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)


def pin_kite_to_ipv4(enabled: bool = True) -> bool:
    """Make Kite calls leave on IPv4 so one whitelistable address is used. Idempotent."""
    global _IPV4_PIN_INSTALLED
    if not enabled or _IPV4_PIN_INSTALLED:
        return _IPV4_PIN_INSTALLED
    socket.getaddrinfo = _ipv4_only_getaddrinfo
    _IPV4_PIN_INSTALLED = True
    logger.info(
        "Kite API pinned to IPv4 so orders leave on the address you can whitelist "
        "(an IPv6 egress is typically a rotating privacy address and cannot be)."
    )
    return True


def kite_proxies() -> Optional[Dict[str, str]]:
    """Proxy map for Kite REST calls, or None.

    Kite validates the TCP source address on ORDER PLACEMENT only, so the address that
    has to be stable is the one the REST client uses — not the one the tick socket uses.
    Routing just these calls through a host with a fixed address therefore fixes the
    whitelist problem without putting the market-data feed behind an extra hop, which
    matters here: the tick path is on a 1s loop and has already been the source of one
    latency regression.

    KiteConnect passes this straight to requests, so any form requests understands works
    (http://host:port, socks5h://host:port with PySocks installed).
    """
    try:
        from config.settings import get_settings

        proxy = str(getattr(get_settings(), "kite_http_proxy", "") or "").strip()
    except Exception:
        return None
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def current_egress_ip(family: str = "v4", timeout: float = 4.0) -> str:
    """This machine's public address as a server sees it, or "" when unavailable.

    Kite reports the address it saw only inside a refusal, which means the first time you
    learn the whitelist is stale is when an order has already been rejected. This lets the
    readiness gate say so before the session starts.
    """
    import urllib.request

    url = "https://api4.ipify.org" if family == "v4" else "https://api6.ipify.org"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace").strip()
    except Exception as exc:
        logger.debug(f"Egress {family} lookup unavailable: {exc}")
        return ""

IST = pytz.timezone("Asia/Kolkata")

# Kite invalidates access tokens early each morning. Treat anything issued before the
# most recent cutoff as dead rather than discovering it on the first order of the day.
TOKEN_CUTOFF = dt_time(6, 0)


def most_recent_token_cutoff(now: Optional[datetime] = None) -> datetime:
    """The boundary before which any access token has certainly expired."""
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = IST.localize(now)
    today_cutoff = now.replace(
        hour=TOKEN_CUTOFF.hour, minute=TOKEN_CUTOFF.minute, second=0, microsecond=0
    )
    return today_cutoff if now >= today_cutoff else today_cutoff - timedelta(days=1)


_SHARED: Optional["KiteAuth"] = None
_SHARED_GUARD = __import__("threading").Lock()


_AUTH_FAILURE_MARKERS = (
    "invalid access token",
    "incorrect api key or access token",
    "invalid api key",
    "invalid token",
    "token expired",
    "token is invalid or has expired",
    "access token has expired",
    "expired access token",
    "session expired",
    "invalid session",
    "authentication failed",
    "unauthorized",
    "login required",
)


def is_kite_auth_failure(exc: Exception) -> bool:
    """Return true only when a Kite error proves the session is unusable.

    Kite's SDK has emitted both TokenException and InputException for dead sessions.
    InputException alone is not enough: malformed orders and unknown order IDs are
    ordinary terminal rejections and must not tear down a healthy session.
    """
    name = type(exc).__name__
    if name == "TokenException":
        return True
    if name != "InputException":
        return False
    message = str(exc or "").strip().lower()
    for character in ("`", "_", "-"):
        message = message.replace(character, " ")
    message = " ".join(message.split())
    return any(marker in message for marker in _AUTH_FAILURE_MARKERS)


def kite_auth_failure_reason(context: str, exc: Exception) -> str:
    return f"{context}: {type(exc).__name__}: {exc}"


def get_kite_auth(settings=None) -> "KiteAuth":
    """Process-wide Kite session.

    One session per process on purpose: the dashboard login route, the data layer and
    the broker must all observe the same token, or a re-login through the browser
    would leave the broker still holding a dead one.
    """
    global _SHARED
    with _SHARED_GUARD:
        if _SHARED is None:
            if settings is None:
                from config.settings import get_settings

                settings = get_settings()
            _SHARED = KiteAuth(
                api_key=getattr(settings, "kite_api_key", ""),
                api_secret=getattr(settings, "kite_api_secret", ""),
                token_file=getattr(settings, "kite_token_file", "kite_token.json"),
            )
            # A cached token from earlier today makes the daily login a no-op.
            _SHARED.load_cached_session()
        return _SHARED


def reset_kite_auth() -> None:
    """Test hook; also used when credentials change at runtime."""
    global _SHARED
    with _SHARED_GUARD:
        _SHARED = None


class KiteAuth:
    """Owns the Kite access token: issue, cache, validate, expire."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        token_file: str = "kite_token.json",
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self._api_secret = str(api_secret or "").strip()
        self.token_file = Path(token_file or "kite_token.json")
        # Installed before any KiteConnect client exists, so the very first call — the
        # login itself — already leaves on the whitelistable address.
        try:
            from config.settings import get_settings

            pin_kite_to_ipv4(
                bool(getattr(get_settings(), "kite_force_ipv4", True))
            )
        except Exception as exc:
            logger.debug(f"Could not pin Kite to IPv4: {exc}")
        self._kite = None
        self._auth_lock = threading.RLock()
        # Disk parsing and broker verification form one cache-restore attempt.  A
        # second verifier must wait: otherwise a provisional network result can win
        # first and make a later broker-proven invalid verdict look stale.
        self._cache_load_lock = threading.Lock()
        # Monotonic authority epoch.  Cached-token verification happens over the
        # network, outside the lock; this generation prevents its late result from
        # overwriting (or clearing) a browser login completed in the meantime.
        self._auth_generation = 0
        self._auth_failure_latched = False
        self._auth_required = True
        self._auth_reason = "no Kite session yet"
        self._token_issued_at: Optional[datetime] = None
        self._user_id = ""

    # ── credentials ──────────────────────────────────────────────────────────
    @staticmethod
    def _malformed_reason(value: str) -> str:
        """Why this cannot be a Kite credential, or "" if it looks like one.

        A Kite api_secret is a compact alphanumeric token. Checking only for emptiness
        and a placeholder let a 96-character shell command sit in KITE_API_SECRET —
        it passed every guard and surfaced only as "Invalid `checksum`" at login,
        which reads like a broken integration rather than a bad paste.
        """
        text = str(value or "").strip().strip("'").strip('"')
        if not text:
            return ""
        if any(ch.isspace() for ch in text):
            return "contains whitespace — looks like a pasted command, not a credential"
        if any(ch in text for ch in ('/', '\\', '"', "'")):
            return "contains path or quote characters — looks like a pasted command"
        if len(text) > 64:
            return f"is {len(text)} characters — far longer than a Kite credential"
        if not text.replace("-", "").replace("_", "").isalnum():
            return "contains characters a Kite credential never uses"
        return ""

    @staticmethod
    def _is_placeholder(value: str) -> bool:
        """Catch an un-substituted template rather than sending it to Zerodha.

        A value like "<regenerate it first>" is not blank, so a plain emptiness check
        treats it as a real credential and the failure surfaces as an opaque login
        error much later. Name it here instead.
        """
        text = str(value or "").strip()
        if not text:
            return False
        if text.startswith("<") or text.endswith(">"):
            return True
        lowered = text.lower()
        return any(
            marker in lowered
            for marker in ("your_", "paste", "regenerate", "changeme", "xxxx", "todo")
        )

    def missing_credentials(self) -> list:
        missing = []
        for name, value in (("KITE_API_KEY", self.api_key), ("KITE_API_SECRET", self._api_secret)):
            if not str(value or "").strip():
                missing.append(name)
            elif self._is_placeholder(value):
                missing.append(f"{name} (still a placeholder, not a real value)")
            else:
                malformed = self._malformed_reason(value)
                if malformed:
                    missing.append(f"{name} ({malformed})")
        return missing

    def _mark_auth_required(self, reason: str) -> None:
        with self._auth_lock:
            self._auth_generation += 1
            self._auth_required = True
            self._auth_reason = str(reason or "Kite login required")
            self._kite = None

    def _discard_cached_token_locked(self) -> None:
        """Remove a token Kite has proved unusable (caller holds ``_auth_lock``)."""
        try:
            self.token_file.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(f"Could not remove invalid Kite token cache: {exc}")

    def _finish_cached_load_failure(
        self,
        generation: int,
        reason: str,
        *,
        discard_cache: bool = False,
        latch_failure: bool = False,
    ) -> bool:
        """Apply a cache-load failure only if no newer authority event won the race."""
        with self._auth_lock:
            if self._auth_generation != generation:
                return bool(self._kite is not None and not self._auth_required)
            if discard_cache:
                self._discard_cached_token_locked()
            self._auth_generation += 1
            self._auth_required = True
            self._auth_reason = str(reason or "Kite login required")
            self._kite = None
            if latch_failure:
                self._auth_failure_latched = True
            return False

    # ── login ────────────────────────────────────────────────────────────────
    def login_url(self) -> str:
        """The URL the operator must open in a browser to start a session."""
        missing = self.missing_credentials()
        if missing:
            message = f"Missing Kite credentials in .env: {', '.join(missing)}"
            self._mark_auth_required(message)
            raise ValueError(message)
        from kiteconnect import KiteConnect

        return KiteConnect(api_key=self.api_key, proxies=kite_proxies()).login_url()

    def complete_login(self, request_token: str) -> Dict[str, Any]:
        """Exchange a browser-issued request_token for a daily access token."""
        request_token = str(request_token or "").strip()
        if not request_token:
            raise ValueError("request_token is required to complete the Kite login")
        missing = self.missing_credentials()
        if missing:
            raise ValueError(f"Missing Kite credentials in .env: {', '.join(missing)}")

        from kiteconnect import KiteConnect

        client = KiteConnect(api_key=self.api_key, proxies=kite_proxies())
        # generate_session computes the SHA-256 checksum internally; the api_secret
        # never leaves this process and is never written to the token cache.
        session = client.generate_session(request_token, api_secret=self._api_secret)
        access_token = str(session.get("access_token") or "")
        if not access_token:
            raise RuntimeError(f"Kite returned no access_token: {session}")

        client.set_access_token(access_token)
        with self._auth_lock:
            self._auth_generation += 1
            self._kite = client
            self._user_id = str(session.get("user_id") or "")
            self._token_issued_at = datetime.now(IST)
            self._auth_required = False
            self._auth_failure_latched = False
            self._auth_reason = ""
            self._persist(access_token, session)
            user_id = self._user_id
            issued_at = self._token_issued_at
        logger.success(f"Kite session established for {user_id or 'user'}")
        return {
            "user_id": user_id,
            "issued_at": issued_at.isoformat(),
        }

    def _persist(self, access_token: str, session: Dict[str, Any]) -> None:
        """Cache the token atomically. Never writes api_secret or any password."""
        payload = {
            "access_token": access_token,
            "public_token": session.get("public_token", ""),
            "user_id": session.get("user_id", ""),
            "issued_at": (self._token_issued_at or datetime.now(IST)).isoformat(),
            "api_key": self.api_key,
        }
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename: a crash mid-write must not leave a half token that
            # reads as a valid session and fails on the first order of the day.
            handle, tmp = tempfile.mkstemp(
                dir=str(self.token_file.parent or "."), suffix=".tmp"
            )
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.token_file)
        except Exception as exc:
            logger.error(f"Failed to cache the Kite token: {exc}")

    def load_cached_session(self) -> bool:
        """Single-flight wrapper for restoring one still-valid cached session."""
        with self._cache_load_lock:
            return self._load_cached_session_once()

    def _load_cached_session_once(self) -> bool:
        """Restore a still-valid token from disk. False when a login is required."""
        with self._auth_lock:
            if self._kite is not None and not self._auth_required:
                return True
            if self._auth_failure_latched:
                # invalidate() removed the proven-dead token.  Do not let a later
                # connect() re-read/reinstall it provisionally after a network error.
                return False
            generation = self._auth_generation
        if not self.token_file.exists():
            return self._finish_cached_load_failure(
                generation, "no cached Kite token"
            )
        try:
            payload = json.loads(self.token_file.read_text(encoding="utf-8"))
        except Exception as exc:
            return self._finish_cached_load_failure(
                generation,
                f"unreadable Kite token cache: {exc}",
                discard_cache=True,
            )

        access_token = str(payload.get("access_token") or "")
        if not access_token:
            return self._finish_cached_load_failure(
                generation,
                "cached Kite token has no access_token",
                discard_cache=True,
            )
        if str(payload.get("api_key") or "") != self.api_key:
            # A regenerated app or a swapped key leaves a token that authenticates as
            # nothing. Say so plainly instead of failing later inside an order.
            return self._finish_cached_load_failure(
                generation,
                "cached Kite token belongs to a different api_key",
                discard_cache=True,
            )

        issued_at = self._parse_ist(payload.get("issued_at"))
        if issued_at is None:
            return self._finish_cached_load_failure(
                generation,
                "cached Kite token has no issue time",
                discard_cache=True,
            )

        from kiteconnect import KiteConnect

        client = KiteConnect(api_key=self.api_key, proxies=kite_proxies())
        client.set_access_token(access_token)

        # Ask Kite whether the token is alive rather than inferring it from the clock.
        # The heuristic was wrong in both directions: it declared a token issued at
        # 04:48 dead at 06:16 the same morning (forcing a second login before the
        # open), and it called an already-revoked token "fresh" because it happened to
        # sit on the right side of the cutoff. One cheap call settles it.
        verdict, reason = self._verify_live(client)
        if verdict is False:
            return self._finish_cached_load_failure(
                generation,
                reason,
                discard_cache=True,
                latch_failure=True,
            )
        if verdict is None:
            # Could not reach Kite. Fall back to the cutoff so an obviously stale token
            # is still refused offline, but do not fail a plausibly-good one.
            if issued_at < most_recent_token_cutoff():
                return self._finish_cached_load_failure(
                    generation,
                    f"Kite unreachable and the cached token from {issued_at:%Y-%m-%d %H:%M} "
                    f"predates the {TOKEN_CUTOFF:%H:%M} IST daily cutoff",
                    discard_cache=True,
                )
            logger.warning(f"Could not verify the Kite token ({reason}); using it provisionally.")

        with self._auth_lock:
            if self._auth_generation != generation or self._auth_failure_latched:
                return bool(self._kite is not None and not self._auth_required)
            self._auth_generation += 1
            self._kite = client
            self._user_id = str(payload.get("user_id") or "")
            self._token_issued_at = issued_at
            self._auth_required = False
            self._auth_failure_latched = False
            self._auth_reason = ""
        return True

    @staticmethod
    def _verify_live(client):
        """(True|False|None, reason). None means 'could not tell', not 'invalid'."""
        try:
            client.profile()
            return True, ""
        except Exception as exc:
            if is_kite_auth_failure(exc):
                return False, f"Kite rejected the cached token: {exc}"
            # A network blip must not force a browser login the operator cannot do
            # from a headless start.
            return None, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _parse_ist(value: Any) -> Optional[datetime]:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        return IST.localize(parsed) if parsed.tzinfo is None else parsed.astimezone(IST)

    # ── runtime surface ──────────────────────────────────────────────────────
    @property
    def kite(self):
        """The authenticated client, or None when a login is required."""
        with self._auth_lock:
            return self._kite

    @contextmanager
    def current_client_guard(self, expected_client):
        """Linearize a client-bound SDK call against session replacement.

        The caller may hold this context through the network dispatch.  A browser
        login can perform its token exchange concurrently, but cannot install the new
        client between this final identity check and the old client's SDK call.
        """
        with self._auth_lock:
            yield bool(
                expected_client is not None
                and self._kite is expected_client
                and not self._auth_required
                and not self._auth_failure_latched
            )

    def invalidate(self, reason: str, failed_client=None) -> bool:
        """Drop the failed session once, without invalidating a newer login.

        ``failed_client`` is the exact SDK client whose request failed. An old request
        may return after a browser re-login installed a new client; identity checking
        prevents that late response from clearing the replacement session.
        """
        with self._auth_lock:
            if failed_client is not None and self._kite is not failed_client:
                return False
            if self._auth_failure_latched:
                return False
            self._auth_failure_latched = True
            logger.error(f"Kite session invalidated: {reason}")
            self._mark_auth_required(reason)
            # A token that just produced a terminal auth error must not survive a
            # reconnect/restart path and be accepted provisionally on network doubt.
            self._discard_cached_token_locked()
            return True

    def status(self) -> Dict[str, Any]:
        with self._auth_lock:
            return {
                "connected": self._kite is not None,
                "auth_required": self._auth_required,
                "reason": self._auth_reason,
                "user_id": self._user_id,
                "token_updated_at": (
                    self._token_issued_at.isoformat() if self._token_issued_at else None
                ),
            }
