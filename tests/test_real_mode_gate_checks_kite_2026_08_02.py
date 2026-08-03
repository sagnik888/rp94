"""The REAL-mode gate must validate the broker that actually places orders.

Execution moved to Zerodha Kite, but validate_real_mode_readiness still demanded four
AngelOne credentials and never looked at Kite. With those credentials left in .env — the
normal state after the migration — the gate reported REAL mode READY while the Kite
session was never checked at all.

That matters more than an ordinary stale check: the Kite access token expires EVERY day.
"Has the daily login been done" is the one question a pre-market gate exists to answer,
and it was the one question it did not ask. A miss means every order of the session is
refused with "no authenticated Kite session" while the dashboard reports ready.
"""
from datetime import datetime, timedelta

import pytest
import pytz

import engine.safe_mode as safe_mode
from engine.safe_mode import validate_real_mode_readiness

IST = pytz.timezone("Asia/Kolkata")
ARMED = IST.localize(datetime(2026, 8, 3, 9, 20))     # inside the arming window


class _Settings:
    kite_api_key = "abc123"
    kite_api_secret = "secret456"
    active_indices = ["NIFTY"]

    def risk_configuration_summary(self):
        return {"ok": True, "issues": [], "warnings": []}


class _Auth:
    def __init__(self, **status):
        self._status = status

    def status(self):
        return self._status


def _gate(monkeypatch, **kite_status):
    monkeypatch.setattr(safe_mode, "get_kite_auth", lambda settings=None: _Auth(**kite_status),
                        raising=False)
    monkeypatch.setattr("data.kite_auth.get_kite_auth", lambda settings=None: _Auth(**kite_status))
    return validate_real_mode_readiness(_Settings(), None, now=ARMED)


def _blockers(result):
    return " | ".join(result.get("blockers") or [])


def test_a_missing_kite_login_blocks_real_mode(monkeypatch):
    result = _gate(monkeypatch, connected=False, auth_required=True,
                   reason="no cached Kite token", token_updated_at=None)
    assert result["ok"] is False
    assert "Kite daily login required" in _blockers(result)


def test_a_session_that_died_mid_life_blocks_real_mode(monkeypatch):
    """auth_required is set when the token is invalidated during a session."""
    result = _gate(monkeypatch, connected=True, auth_required=True,
                   reason="entry order: Invalid access token",
                   token_updated_at=ARMED.isoformat())
    assert result["ok"] is False
    assert "Kite daily login required" in _blockers(result)


def test_yesterdays_token_blocks_real_mode(monkeypatch):
    """The token expires daily; a valid-looking cached one from yesterday is not valid."""
    stale = (ARMED - timedelta(days=1)).isoformat()
    result = _gate(monkeypatch, connected=True, auth_required=False,
                   reason="", token_updated_at=stale)
    assert result["ok"] is False
    assert "stale" in _blockers(result).lower()


def test_todays_authenticated_session_does_not_block(monkeypatch):
    result = _gate(monkeypatch, connected=True, auth_required=False,
                   reason="", token_updated_at=ARMED.isoformat())
    assert "Kite" not in _blockers(result), (
        f"a healthy Kite session was blocked: {_blockers(result)}"
    )


def test_missing_kite_credentials_block_real_mode(monkeypatch):
    class _NoCreds(_Settings):
        kite_api_key = ""
        kite_api_secret = ""

    monkeypatch.setattr("data.kite_auth.get_kite_auth",
                        lambda settings=None: _Auth(connected=True, auth_required=False,
                                                    reason="", token_updated_at=ARMED.isoformat()))
    result = validate_real_mode_readiness(_NoCreds(), None, now=ARMED)
    assert result["ok"] is False
    assert "kite_api_key missing" in _blockers(result)
    assert "kite_api_secret missing" in _blockers(result)


def test_the_gate_no_longer_demands_retired_angelone_credentials():
    """Those credentials place no orders; requiring them blocks REAL mode for nothing."""
    import inspect

    src = inspect.getsource(validate_real_mode_readiness)
    for retired in ("angelone_api_key", "angelone_client_id",
                    "angelone_password", "angelone_totp_secret"):
        assert retired not in src, f"{retired} is still a REAL-mode requirement"


# ───────── An underfunded account must fail fast with the real reason ─────────
# preflight_entry_margin computed this correctly and had NO caller, so an account that
# could not fund a single lot fired a doomed order per signal and got back a generic RMS
# rejection. The cause was knowable before sending and was not being asked for.

import threading as _threading
from datetime import datetime as _dt

from trading.order_retry import BrokerOrderRejected as _Rejected
from trading.trade_manager import TradeManager as _TM


class _MarginBroker:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    def preflight_entry_margin(self, **kw):
        self.calls.append(kw)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


def _margin_manager(broker):
    m = _TM.__new__(_TM)
    m.broker = broker
    m.product_type = "CARRYFORWARD"
    m.ui_logger = None
    return m


def test_an_unaffordable_entry_is_refused_before_it_is_sent():
    broker = _MarginBroker({
        "ok": False, "basis": "KITE_ORDER_MARGIN",
        "available": 30_504.0, "required": 45_825.0,
        "limitation": "insufficient funds: 45,825.00 required, 30,504.00 available",
    })
    verdict = _margin_manager(broker)._entry_margin_verdict("NIFTY2680423900CE", 65, "BUY", 705.7)
    assert verdict["ok"] is False
    assert "insufficient funds" in verdict["limitation"]
    assert broker.calls and broker.calls[0]["qty"] == 65


def test_an_affordable_entry_passes():
    broker = _MarginBroker({"ok": True, "available": 500_000.0, "required": 45_825.0})
    assert _margin_manager(broker)._entry_margin_verdict("NIFTY2680423900CE", 65, "BUY", 705.7)["ok"]


def test_an_unavailable_margin_endpoint_never_blocks_trading():
    """Fail OPEN. A broker that cannot answer must not halt a funded account."""
    broker = _MarginBroker(RuntimeError("margins endpoint down"))
    assert _margin_manager(broker)._entry_margin_verdict("NIFTY", 65, "BUY", 700.0) is None


def test_a_broker_without_preflight_support_is_not_blocked():
    class _Bare:
        pass

    assert _margin_manager(_Bare())._entry_margin_verdict("NIFTY", 65, "BUY", 700.0) is None
