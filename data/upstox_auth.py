"""Upstox API v2 session authentication and lifecycle management.

Handles OAuth2 code exchange, JWT token persistence, daily 03:30 IST
expiry lifecycle, live session probes against /v2/user/profile,
candidate redirect URI resolution, and CLI daily login helpers.
Modeled after MANTRA v5.12 battle-tested Upstox engine.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / "upstox_token.json"
IST = ZoneInfo("Asia/Kolkata")

TOKEN_CUTOFF = dt_time(3, 30)

AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
PROFILE_URL = "https://api.upstox.com/v2/user/profile"


def most_recent_token_cutoff(now: Optional[datetime] = None) -> datetime:
    """Return the most recent 03:30 IST cutoff datetime."""
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    today = now.replace(hour=TOKEN_CUTOFF.hour, minute=TOKEN_CUTOFF.minute, second=0, microsecond=0)
    return today if now >= today else today - timedelta(days=1)


def next_token_expiry(now: Optional[datetime] = None) -> datetime:
    """Return the next 03:30 IST expiry datetime."""
    return most_recent_token_cutoff(now) + timedelta(days=1)


def decode_jwt_expiry(token: str) -> Optional[datetime]:
    """Decode the JWT payload to extract expiration timestamp."""
    try:
        import base64
        parts = token.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            exp = data.get("exp")
            if exp:
                return datetime.fromtimestamp(int(exp), tz=ZoneInfo("UTC")).astimezone(IST)
    except Exception:
        pass
    return None


def read_upstox_env() -> Dict[str, str]:
    """Read Upstox configuration from .env, settings, and os.environ."""
    env: Dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip().strip('"').strip("'")
        except Exception:
            pass

    try:
        from config.settings import get_settings
        settings = get_settings()
        for k in ("upstox_api_key", "upstox_api_secret", "upstox_redirect_uri", "upstox_user_id"):
            val = getattr(settings, k, "")
            if val:
                env[k.upper()] = str(val).strip()
    except Exception:
        pass

    for key in (
        "UPSTOX_USER_ID",
        "UPSTOX_PASSWORD",
        "UPSTOX_API_KEY",
        "UPSTOX_API_SECRET",
        "UPSTOX_REDIRECT_URI",
        "UPSTOX_ACCESS_TOKEN",
        "UPSTOX_PUBLIC_IP",
    ):
        if os.getenv(key):
            env[key] = os.getenv(key, "").strip()

    if not env.get("UPSTOX_REDIRECT_URI"):
        env["UPSTOX_REDIRECT_URI"] = "http://127.0.0.1:8500/api/upstox/callback"
    if not env.get("UPSTOX_USER_ID"):
        env["UPSTOX_USER_ID"] = "7JATYL"

    return env


class UpstoxAuth:
    """Thread-safe Upstox OAuth session manager with daily refresh support."""

    def __init__(self, token_file: Path = TOKEN_FILE) -> None:
        self.token_file = token_file
        self._lock = threading.RLock()
        self._access_token: Optional[str] = None
        self._user_id: str = ""
        self._user_name: str = ""
        self._email: str = ""
        self._token_issued_at: Optional[datetime] = None
        self._token_expires_at: Optional[datetime] = None
        self._auth_required: bool = True
        self._auth_reason: str = "no token loaded yet"
        self._last_probe: Optional[datetime] = None
        self._last_probe_ok: bool = False
        self.load_cached_session()

    @property
    def access_token(self) -> Optional[str]:
        with self._lock:
            return self._access_token

    def has_valid_token(self) -> bool:
        with self._lock:
            return bool(self._access_token and not self._auth_required)

    def missing_credentials(self) -> List[str]:
        env = read_upstox_env()
        missing = []
        for key in ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI"):
            if not env.get(key):
                missing.append(key)
        return missing

    def login_url(self) -> str:
        """Construct the browser URL to initiate Upstox authorization."""
        env = read_upstox_env()
        missing = self.missing_credentials()
        if missing:
            raise ValueError(f"Cannot generate login URL: missing {', '.join(missing)}")
        params = {
            "response_type": "code",
            "client_id": env["UPSTOX_API_KEY"],
            "redirect_uri": env["UPSTOX_REDIRECT_URI"],
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def extract_code(raw: str) -> str:
        """Extract authorization code from a raw string or full redirect URL."""
        raw = (raw or "").strip().strip('"').strip("'")
        if not raw:
            return ""
        if "code=" in raw:
            try:
                parsed = urllib.parse.urlparse(raw)
                qs = urllib.parse.parse_qs(parsed.query or parsed.fragment or "")
                if qs.get("code"):
                    return qs["code"][0].strip()
                return raw.split("code=", 1)[1].split("&")[0].strip()
            except Exception:
                return raw.split("code=", 1)[1].split("&")[0].strip()
        return raw

    def complete_login(self, code_or_url: str) -> Dict[str, Any]:
        """Exchange authorization code or accept raw access token and persist."""
        extracted = self.extract_code(code_or_url)
        if not extracted:
            raise ValueError("An authorization code, full redirect URL, or token is required")

        # Direct JWT token input
        if extracted.startswith("eyJ"):
            ok, reason, profile = self._verify_live(extracted)
            if not ok:
                raise RuntimeError(f"Upstox rejected access token: {reason}")
            user_id = profile.get("user_id") or "7JATYL"
            user_name = profile.get("user_name", "")
            exp = decode_jwt_expiry(extracted) or next_token_expiry()
            with self._lock:
                self._access_token = extracted
                self._user_id = user_id
                self._user_name = user_name
                self._email = profile.get("email", "")
                self._token_issued_at = datetime.now(IST)
                self._token_expires_at = exp
                self._auth_required = False
                self._auth_reason = ""
                os.environ["UPSTOX_ACCESS_TOKEN"] = extracted
            self._persist(extracted, {"user_id": user_id, "user_name": user_name})
            return {
                "status": "success",
                "user_id": user_id,
                "user_name": user_name,
                "access_token": extracted,
                "expires_at": exp.isoformat(),
                "connected": True,
            }

        missing = self.missing_credentials()
        if missing:
            raise ValueError(f"Missing Upstox credentials: {', '.join(missing)}")

        env = read_upstox_env()
        base_uri = env.get("UPSTOX_REDIRECT_URI", "http://127.0.0.1:8500/api/upstox/callback")

        # Candidate redirect URIs to prevent URI mismatch errors
        candidates = [base_uri]
        if base_uri.endswith("/"):
            candidates.append(base_uri.rstrip("/"))
        else:
            candidates.append(base_uri + "/")
        if "127.0.0.1" in base_uri:
            candidates.append(base_uri.replace("127.0.0.1", "localhost"))
            candidates.append(base_uri.replace("127.0.0.1", "localhost").rstrip("/") + "/")
        elif "localhost" in base_uri:
            candidates.append(base_uri.replace("localhost", "127.0.0.1"))
            candidates.append(base_uri.replace("localhost", "127.0.0.1").rstrip("/") + "/")
        candidates.extend([
            "http://127.0.0.1:8500/api/upstox/callback",
            "http://localhost:8500/api/upstox/callback",
            "http://127.0.0.1:8500",
            "http://localhost:8500",
            "http://127.0.0.1:5100",
        ])

        seen = set()
        deduped_candidates = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                deduped_candidates.append(c)

        result = None
        first_error = ""
        last_error = ""

        for uri in deduped_candidates:
            try:
                resp = requests.post(
                    TOKEN_URL,
                    headers={"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                    data={
                        "code": extracted,
                        "client_id": env["UPSTOX_API_KEY"],
                        "client_secret": env["UPSTOX_API_SECRET"],
                        "redirect_uri": uri,
                        "grant_type": "authorization_code",
                    },
                    timeout=20,
                )
                if resp.status_code == 200:
                    result = resp.json() or {}
                    break
                err_text = resp.text[:300]
                if not first_error:
                    first_error = f"HTTP {resp.status_code}: {err_text}"
                last_error = f"HTTP {resp.status_code}: {err_text}"
                # If error is not redirect URI mismatch, don't retry other candidates
                if "redirect" not in err_text.lower() and "uri" not in err_text.lower() and "100068" not in err_text:
                    break
            except Exception as e:
                if not first_error:
                    first_error = str(e)
                last_error = str(e)

        if not result or not result.get("access_token"):
            detail = first_error or last_error
            hint = ""
            low = detail.lower()
            if "udapi100016" in low:
                hint = (
                    " [UDAPI100016: Invalid Credentials. The authorization code expired (~2m) or was already used. "
                    "Open Incognito window, visit /api/upstox_login, do NOT scan QR code: enter Phone -> OTP -> PIN (173089).]"
                )
            elif "udapi100068" in low or "redirect_uri" in low:
                want = env.get("UPSTOX_REDIRECT_URI", "")
                hint = f" [UDAPI100068: Upstox requires redirect_uri to match '{want}' exactly in Developer Console.]"
            raise RuntimeError(f"Upstox rejected token exchange: {detail}{hint}")

        access_token = str(result.get("access_token") or "")
        ok, reason, profile = self._verify_live(access_token)
        if not ok:
            raise RuntimeError(f"Upstox token verification failed: {reason}")

        user_id = str(result.get("user_id") or profile.get("user_id") or env.get("UPSTOX_USER_ID", "7JATYL"))
        user_name = str(profile.get("user_name", ""))
        exp = decode_jwt_expiry(access_token) or next_token_expiry()

        with self._lock:
            self._access_token = access_token
            self._user_id = user_id
            self._user_name = user_name
            self._email = profile.get("email", "")
            self._token_issued_at = datetime.now(IST)
            self._token_expires_at = exp
            self._auth_required = False
            self._auth_reason = ""
            os.environ["UPSTOX_ACCESS_TOKEN"] = access_token
        self._persist(access_token, result)
        logger.info(f"Upstox session established for {user_id} (expires {exp:%Y-%m-%d %H:%M} IST)")
        return {
            "status": "success",
            "user_id": user_id,
            "user_name": user_name,
            "access_token": access_token,
            "issued_at": self._token_issued_at.isoformat(),
            "expires_at": exp.isoformat(),
            "connected": True,
        }

    def _persist(self, access_token: str, session: Dict[str, Any]) -> None:
        """Save active access token to JSON file."""
        now = datetime.now(IST)
        payload = {
            "access_token": access_token,
            "user_id": session.get("user_id", "") or self._user_id or "7JATYL",
            "user_name": session.get("user_name", "") or self._user_name,
            "issued_at": (self._token_issued_at or now).isoformat(),
            "expires_at": self._token_expires_at.isoformat() if self._token_expires_at else (now + timedelta(hours=24)).isoformat(),
            "saved_at": now.isoformat(),
        }
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.error(f"Failed to save Upstox token to {self.token_file}: {exc}")

    def load_cached_session(self) -> None:
        """Restore existing cached session if not expired."""
        token = ""
        user_id = ""
        user_name = ""
        issued_at = None
        expires_at = None

        if self.token_file.exists():
            try:
                data = json.loads(self.token_file.read_text(encoding="utf-8"))
                token = data.get("access_token", "")
                user_id = data.get("user_id", "")
                user_name = data.get("user_name", "")
                if data.get("issued_at") or data.get("saved_at"):
                    ts = data.get("issued_at") or data.get("saved_at")
                    issued_at = datetime.fromisoformat(ts)
                    if issued_at.tzinfo is None:
                        issued_at = issued_at.replace(tzinfo=IST)
                if data.get("expires_at"):
                    expires_at = datetime.fromisoformat(data["expires_at"])
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=IST)
            except Exception:
                pass

        if not token:
            env = read_upstox_env()
            token = env.get("UPSTOX_ACCESS_TOKEN", "")

        if token:
            exp = decode_jwt_expiry(token) or expires_at
            now = datetime.now(IST)
            if exp and now >= exp:
                self._auth_required = True
                self._auth_reason = f"token expired at {exp:%Y-%m-%d %H:%M} IST"
                return

            self._access_token = token
            self._user_id = user_id or "7JATYL"
            self._user_name = user_name
            self._token_issued_at = issued_at
            self._token_expires_at = exp
            os.environ["UPSTOX_ACCESS_TOKEN"] = token

            ok, reason, profile = self._verify_live(token)
            if ok:
                self._auth_required = False
                self._auth_reason = ""
                if profile.get("user_name"):
                    self._user_name = profile["user_name"]
                if profile.get("user_id"):
                    self._user_id = profile["user_id"]
                if profile.get("email"):
                    self._email = profile["email"]
            else:
                self._auth_required = True
                self._auth_reason = reason or "Upstox rejected cached token"

    def _verify_live(self, token: str) -> Tuple[bool, str, Dict[str, Any]]:
        """Verify token against Upstox profile endpoint."""
        try:
            r = requests.get(
                PROFILE_URL,
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                timeout=8,
            )
            if r.status_code == 200:
                body = r.json() or {}
                if body.get("status") == "success":
                    self._last_probe = datetime.now(IST)
                    self._last_probe_ok = True
                    return True, "", body.get("data", {})
            err = ""
            if r.text.strip().startswith("{"):
                try:
                    err_obj = r.json().get("errors", [{}])[0]
                    err = f"{err_obj.get('errorCode', '')} {err_obj.get('message', '')}".strip()
                except Exception:
                    pass
            err = err or r.text[:120]
            return False, f"HTTP {r.status_code}: {err}", {}
        except Exception as e:
            return False, f"Network error: {e}", {}

    def status(self) -> Dict[str, Any]:
        """Return rich diagnostic status dictionary."""
        with self._lock:
            token = self._access_token
            expires_at = self._token_expires_at
            now = datetime.now(IST)
            token_expired = bool(expires_at and expires_at <= now)
            if token_expired:
                self._auth_required = True
                self._auth_reason = "token expired"
            connected = bool(token and not self._auth_required and not token_expired)
            secs_left = int((expires_at - now).total_seconds()) if (connected and expires_at) else 0
            hours_left = round(secs_left / 3600.0, 1)
            expiring_soon = bool(connected and secs_left < 3600)

            url = None
            if not connected:
                try:
                    url = self.login_url()
                except Exception:
                    url = None

            return {
                "broker": "UPSTOX",
                "status": "LIVE" if connected else ("EXPIRED" if token_expired else "OFFLINE"),
                "authenticated": connected,
                "connected": connected,
                "auth_required": not connected,
                "reason": "" if connected else (self._auth_reason or ("token expired" if token_expired else "authentication required")),
                "user_id": self._user_id or "7JATYL",
                "user_name": self._user_name,
                "email": self._email,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "seconds_remaining": max(0, secs_left),
                "hours_remaining": max(0.0, hours_left),
                "expiring_soon": expiring_soon,
                "login_url": url,
            }


_UPSTOX_AUTH_SINGLETON: Optional[UpstoxAuth] = None
_AUTH_LOCK = threading.Lock()


def get_upstox_auth() -> UpstoxAuth:
    """Return the global UpstoxAuth singleton."""
    global _UPSTOX_AUTH_SINGLETON
    with _AUTH_LOCK:
        if _UPSTOX_AUTH_SINGLETON is None:
            _UPSTOX_AUTH_SINGLETON = UpstoxAuth()
        return _UPSTOX_AUTH_SINGLETON


def reset_upstox_auth() -> None:
    """Reset the singleton instance."""
    global _UPSTOX_AUTH_SINGLETON
    with _AUTH_LOCK:
        _UPSTOX_AUTH_SINGLETON = None


# Module-level convenience functions preserving existing contracts
def get_upstox_auth_url(api_key: str = "", redirect_uri: str = "") -> str:
    """Generate the OAuth authorization URL for Upstox."""
    if api_key and redirect_uri:
        params = {"client_id": api_key, "redirect_uri": redirect_uri, "response_type": "code"}
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    return get_upstox_auth().login_url()


def exchange_code_for_token(
    code: str,
    api_key: str = "",
    api_secret: str = "",
    redirect_uri: str = ""
) -> Dict[str, Any]:
    """Exchange authorization code or save direct token."""
    try:
        auth = get_upstox_auth()
        res = auth.complete_login(code)
        return res
    except Exception as exc:
        logger.error(f"Upstox token exchange error: {exc}")
        return {"status": "error", "message": str(exc)}


complete_upstox_login = exchange_code_for_token


def save_upstox_token(token: str, user_id: str = "7JATYL") -> None:
    """Save Upstox access token directly."""
    auth = get_upstox_auth()
    auth.complete_login(token)


def load_upstox_token_data() -> Optional[Dict[str, Any]]:
    """Read token data from cache or file."""
    auth = get_upstox_auth()
    st = auth.status()
    if auth.access_token:
        return {
            "access_token": auth.access_token,
            "user_id": st.get("user_id", "7JATYL"),
            "user_name": st.get("user_name", ""),
            "expires_at": st.get("expires_at"),
            "connected": st.get("connected", False),
        }
    return None


def get_upstox_token() -> Optional[str]:
    """Get active access token if available and valid."""
    auth = get_upstox_auth()
    return auth.access_token if auth.has_valid_token() else None


def get_upstox_auth_status() -> Dict[str, Any]:
    """Check whether Upstox session is live and valid."""
    return get_upstox_auth().status()
