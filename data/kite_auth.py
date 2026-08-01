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
import tempfile
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import pytz
from loguru import logger

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
        self._kite = None
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
        self._auth_required = True
        self._auth_reason = str(reason or "Kite login required")
        self._kite = None

    # ── login ────────────────────────────────────────────────────────────────
    def login_url(self) -> str:
        """The URL the operator must open in a browser to start a session."""
        missing = self.missing_credentials()
        if missing:
            message = f"Missing Kite credentials in .env: {', '.join(missing)}"
            self._mark_auth_required(message)
            raise ValueError(message)
        from kiteconnect import KiteConnect

        return KiteConnect(api_key=self.api_key).login_url()

    def complete_login(self, request_token: str) -> Dict[str, Any]:
        """Exchange a browser-issued request_token for a daily access token."""
        request_token = str(request_token or "").strip()
        if not request_token:
            raise ValueError("request_token is required to complete the Kite login")
        missing = self.missing_credentials()
        if missing:
            raise ValueError(f"Missing Kite credentials in .env: {', '.join(missing)}")

        from kiteconnect import KiteConnect

        client = KiteConnect(api_key=self.api_key)
        # generate_session computes the SHA-256 checksum internally; the api_secret
        # never leaves this process and is never written to the token cache.
        session = client.generate_session(request_token, api_secret=self._api_secret)
        access_token = str(session.get("access_token") or "")
        if not access_token:
            raise RuntimeError(f"Kite returned no access_token: {session}")

        client.set_access_token(access_token)
        self._kite = client
        self._user_id = str(session.get("user_id") or "")
        self._token_issued_at = datetime.now(IST)
        self._auth_required = False
        self._auth_reason = ""
        self._persist(access_token, session)
        logger.success(f"Kite session established for {self._user_id or 'user'}")
        return {
            "user_id": self._user_id,
            "issued_at": self._token_issued_at.isoformat(),
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
        """Restore a still-valid token from disk. False when a login is required."""
        if not self.token_file.exists():
            self._mark_auth_required("no cached Kite token")
            return False
        try:
            payload = json.loads(self.token_file.read_text(encoding="utf-8"))
        except Exception as exc:
            self._mark_auth_required(f"unreadable Kite token cache: {exc}")
            return False

        access_token = str(payload.get("access_token") or "")
        if not access_token:
            self._mark_auth_required("cached Kite token has no access_token")
            return False
        if str(payload.get("api_key") or "") != self.api_key:
            # A regenerated app or a swapped key leaves a token that authenticates as
            # nothing. Say so plainly instead of failing later inside an order.
            self._mark_auth_required("cached Kite token belongs to a different api_key")
            return False

        issued_at = self._parse_ist(payload.get("issued_at"))
        if issued_at is None:
            self._mark_auth_required("cached Kite token has no issue time")
            return False

        from kiteconnect import KiteConnect

        client = KiteConnect(api_key=self.api_key)
        client.set_access_token(access_token)

        # Ask Kite whether the token is alive rather than inferring it from the clock.
        # The heuristic was wrong in both directions: it declared a token issued at
        # 04:48 dead at 06:16 the same morning (forcing a second login before the
        # open), and it called an already-revoked token "fresh" because it happened to
        # sit on the right side of the cutoff. One cheap call settles it.
        verdict, reason = self._verify_live(client)
        if verdict is False:
            self._mark_auth_required(reason)
            return False
        if verdict is None:
            # Could not reach Kite. Fall back to the cutoff so an obviously stale token
            # is still refused offline, but do not fail a plausibly-good one.
            if issued_at < most_recent_token_cutoff():
                self._mark_auth_required(
                    f"Kite unreachable and the cached token from {issued_at:%Y-%m-%d %H:%M} "
                    f"predates the {TOKEN_CUTOFF:%H:%M} IST daily cutoff"
                )
                return False
            logger.warning(f"Could not verify the Kite token ({reason}); using it provisionally.")

        self._kite = client
        self._user_id = str(payload.get("user_id") or "")
        self._token_issued_at = issued_at
        self._auth_required = False
        self._auth_reason = ""
        return True

    @staticmethod
    def _verify_live(client):
        """(True|False|None, reason). None means 'could not tell', not 'invalid'."""
        try:
            from kiteconnect.exceptions import TokenException
        except ImportError:
            return None, "kiteconnect exceptions unavailable"
        try:
            client.profile()
            return True, ""
        except TokenException as exc:
            return False, f"Kite rejected the cached token: {exc}"
        except Exception as exc:
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
        return self._kite

    def invalidate(self, reason: str) -> None:
        """Drop the session after Kite reports the token is dead (TokenException)."""
        logger.error(f"Kite session invalidated: {reason}")
        self._mark_auth_required(reason)

    def status(self) -> Dict[str, Any]:
        return {
            "connected": self._kite is not None,
            "auth_required": self._auth_required,
            "reason": self._auth_reason,
            "user_id": self._user_id,
            "token_updated_at": (
                self._token_issued_at.isoformat() if self._token_issued_at else None
            ),
        }
