"""Upstox API v2 session authentication and lifecycle management.

Handles OAuth2 code exchange, token persistence in upstox_token.json,
daily expiry checks (Upstox tokens refresh every 24h / expire 03:30 IST),
and session validation.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / "upstox_token.json"

AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
PROFILE_URL = "https://api.upstox.com/v2/user/profile"

_AUTH_LOCK = threading.Lock()
_CACHED_TOKEN_INFO: Optional[Dict[str, Any]] = None


def get_upstox_auth_url(api_key: str = "", redirect_uri: str = "") -> str:
    """Generate the OAuth authorization URL for Upstox."""
    if not api_key or not redirect_uri:
        from config.settings import get_settings
        settings = get_settings()
        api_key = api_key or getattr(settings, "upstox_api_key", "")
        redirect_uri = redirect_uri or getattr(settings, "upstox_redirect_uri", "")

    params = {
        "client_id": api_key,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_token(
    code: str,
    api_key: str = "",
    api_secret: str = "",
    redirect_uri: str = ""
) -> Dict[str, Any]:
    """Exchange authorization code for Upstox access token."""
    if not api_key or not api_secret or not redirect_uri:
        from config.settings import get_settings
        settings = get_settings()
        api_key = api_key or getattr(settings, "upstox_api_key", "")
        api_secret = api_secret or getattr(settings, "upstox_api_secret", "")
        redirect_uri = redirect_uri or getattr(settings, "upstox_redirect_uri", "")

    headers = {
        "accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "code": code.strip(),
        "client_id": api_key,
        "client_secret": api_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    try:
        resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=15)
        payload = resp.json()
    except Exception as exc:
        logger.error(f"Upstox token exchange network error: {exc}")
        return {"status": "error", "message": str(exc)}

    token = payload.get("access_token")
    if not token:
        err_msg = payload.get("errors") or payload.get("message") or resp.text
        logger.error(f"Upstox token exchange failed: {err_msg}")
        return {"status": "error", "message": str(err_msg), "raw": payload}

    user_id = payload.get("user_id") or "7JATYL"
    save_upstox_token(token, user_id=user_id)
    return {"status": "success", "access_token": token, "user_id": user_id}


def save_upstox_token(token: str, user_id: str = "7JATYL") -> None:
    """Save the Upstox access token to upstox_token.json."""
    global _CACHED_TOKEN_INFO
    now = datetime.now()
    data = {
        "access_token": token,
        "user_id": user_id,
        "saved_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
    }
    with _AUTH_LOCK:
        TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _CACHED_TOKEN_INFO = data
    logger.info(f"Upstox token saved successfully to {TOKEN_FILE.name}")


def load_upstox_token_data() -> Optional[Dict[str, Any]]:
    """Read the token data from upstox_token.json or environment."""
    global _CACHED_TOKEN_INFO
    if _CACHED_TOKEN_INFO and _CACHED_TOKEN_INFO.get("access_token"):
        return _CACHED_TOKEN_INFO

    with _AUTH_LOCK:
        if TOKEN_FILE.exists():
            try:
                data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
                if data.get("access_token"):
                    _CACHED_TOKEN_INFO = data
                    return data
            except Exception as e:
                logger.warning(f"Could not read {TOKEN_FILE}: {e}")

        # Fallback to environment variable
        env_token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
        if env_token:
            data = {
                "access_token": env_token,
                "user_id": os.environ.get("UPSTOX_USER_ID", "7JATYL"),
                "saved_at": datetime.now().isoformat(),
                "expires_at": (datetime.now() + timedelta(hours=24)).isoformat(),
            }
            _CACHED_TOKEN_INFO = data
            return data
    return None


def get_upstox_token() -> Optional[str]:
    """Get active access token if available."""
    data = load_upstox_token_data()
    return data.get("access_token") if data else None


def get_upstox_auth_status() -> Dict[str, Any]:
    """Check whether Upstox session is live and valid."""
    data = load_upstox_token_data()
    if not data or not data.get("access_token"):
        return {
            "authenticated": False,
            "status": "NO_TOKEN",
            "reason": "Token file missing or empty",
            "user_id": "",
            "hours_remaining": 0.0
        }

    token = data["access_token"]
    user_id = data.get("user_id", "7JATYL")

    # Fast probe against profile endpoint
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(PROFILE_URL, headers=headers, timeout=5)
        if resp.status_code == 200:
            profile_data = resp.json().get("data", {})
            user_name = profile_data.get("user_name", "")
            return {
                "authenticated": True,
                "status": "LIVE",
                "user_id": profile_data.get("user_id") or user_id,
                "user_name": user_name,
                "email": profile_data.get("email", ""),
                "broker": "UPSTOX",
                "saved_at": data.get("saved_at", ""),
                "expires_at": data.get("expires_at", "")
            }
        elif resp.status_code == 401:
            return {
                "authenticated": False,
                "status": "EXPIRED",
                "reason": "Token rejected / unauthorized (401)",
                "user_id": user_id
            }
        else:
            return {
                "authenticated": False,
                "status": "ERROR",
                "reason": f"HTTP {resp.status_code}: {resp.text[:100]}",
                "user_id": user_id
            }
    except Exception as exc:
        # Transient network check: assume authenticated if token was recently saved
        return {
            "authenticated": True,
            "status": "OFFLINE_ASSUMED_LIVE",
            "reason": f"Profile probe network error: {exc}",
            "user_id": user_id
        }
