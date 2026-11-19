"""The REAL-mode gate must validate Upstox primary broker.

Execution moved to Upstox API v2 with Angel One 30s Fallback.
The Upstox access token expires daily at 03:30 IST.
"Has the daily login been done" is the one question a pre-market gate exists to answer.
"""
from datetime import datetime, timedelta

import pytest
import pytz

import engine.safe_mode as safe_mode
from engine.safe_mode import validate_real_mode_readiness

IST = pytz.timezone("Asia/Kolkata")
ARMED = IST.localize(datetime(2026, 8, 3, 9, 20))


class _Settings:
    upstox_api_key = "abc123"
    upstox_api_secret = "secret456"
    active_indices = ["NIFTY"]

    def risk_configuration_summary(self):
        return {"ok": True, "issues": [], "warnings": []}


class _Auth:
    def __init__(self, **status):
        self._status = status

    def status(self):
        return self._status


def _gate(monkeypatch, **upstox_status):
    monkeypatch.setattr("data.upstox_auth.get_upstox_auth", lambda: _Auth(**upstox_status))
    return validate_real_mode_readiness(_Settings(), None, now=ARMED)


def _blockers(result):
    return " | ".join(result.get("blockers") or [])


def test_a_missing_upstox_login_blocks_real_mode(monkeypatch):
    result = _gate(monkeypatch, connected=False, auth_required=True,
                   reason="no cached Upstox token")
    assert result["ok"] is False
    assert "Upstox daily login required" in _blockers(result)


def test_a_session_that_died_mid_life_blocks_real_mode(monkeypatch):
    """auth_required is set when the token is invalidated during a session."""
    result = _gate(monkeypatch, connected=True, auth_required=True,
                   reason="entry order: Invalid access token")
    assert result["ok"] is False
    assert "Upstox daily login required" in _blockers(result)


def test_todays_authenticated_session_does_not_block(monkeypatch):
    result = _gate(monkeypatch, connected=True, auth_required=False,
                   reason="")
    assert "Upstox daily login required" not in _blockers(result)
