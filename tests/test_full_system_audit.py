from types import SimpleNamespace
import importlib.util
from pathlib import Path

import pandas as pd

from data.market_data import MarketDataProvider

_AUDIT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "full_system_audit.py"
_SPEC = importlib.util.spec_from_file_location("full_system_audit", _AUDIT_PATH)
audit = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(audit)


class FakeProvider:
    def __init__(self, *args, **kwargs):
        self.fyers = None

    def connect(self):
        return True

    def get_historical_candles(self, *args, **kwargs):
        return pd.DataFrame(
            {"close": [1.0]},
            index=pd.to_datetime(["2026-06-15 10:00:00"]),
        )

    def get_index_quote(self, symbol):
        return {
            "ltp": 23950.0,
            "source": "angel_ws",
            "quote_label": "LIVE",
        }

    def get_latest_price(self, symbol):
        return 23950.0

    def get_market_source_snapshot(self, symbol):
        return {
            "freshest": {
                "source": "angel_ws",
                "age_seconds": 0.2,
            },
            "entry_eligible": True,
        }

    def is_market_open(self):
        return True

    def get_option_chain(self, symbol):
        return pd.DataFrame(
            [{"strike": 23950, "call_ltp": 100.0, "put_ltp": 90.0}]
        )


def test_full_system_audit_reports_quote_and_freshest_source(monkeypatch):
    messages = []
    monkeypatch.setattr(
        audit,
        "get_settings",
        lambda: SimpleNamespace(
            angelone_api_key="",
            angelone_client_id="",
            angelone_password="",
            angelone_totp_secret="",
            active_indices=["NIFTY"],
        ),
    )
    monkeypatch.setattr(audit, "MarketDataProvider", FakeProvider)
    monkeypatch.setattr(audit.logger, "info", lambda msg: messages.append(str(msg)))
    monkeypatch.setattr(audit.logger, "warning", lambda msg: messages.append(str(msg)))
    monkeypatch.setattr(audit.logger, "error", lambda msg: messages.append(str(msg)))
    monkeypatch.setattr(audit.time, "sleep", lambda _seconds: None)

    result = audit.run_end_to_end_audit(days=1)

    joined = "\n".join(messages)
    assert "Quote: angel_ws" in joined
    assert "Freshest: angel_ws, age=0.2s" in joined
    assert "entry-ok" in joined
    assert "Source: unknown" not in joined
    assert "AngelOne" not in joined
    assert result["read_only"] is True


def test_closed_session_replay_tick_is_never_reported_entry_eligible(monkeypatch):
    class ClosedProvider(FakeProvider):
        def get_index_quote(self, symbol):
            return {"ltp": 23950.0, "source": "kite_ws", "quote_label": "CLOSED"}

        def is_market_open(self):
            return False

    messages = []
    monkeypatch.setattr(
        audit,
        "get_settings",
        lambda: SimpleNamespace(
            angelone_api_key="", angelone_client_id="", angelone_password="",
            angelone_totp_secret="", active_indices=["NIFTY"],
        ),
    )
    monkeypatch.setattr(audit, "MarketDataProvider", ClosedProvider)
    monkeypatch.setattr(audit.logger, "info", lambda msg: messages.append(str(msg)))
    monkeypatch.setattr(audit.logger, "warning", lambda msg: messages.append(str(msg)))
    monkeypatch.setattr(audit.logger, "error", lambda msg: messages.append(str(msg)))
    monkeypatch.setattr(audit.time, "sleep", lambda _seconds: None)

    audit.run_end_to_end_audit(days=1)

    joined = "\n".join(messages)
    assert "context-only (CLOSED)" in joined
    assert "entry-ok" not in joined


def _run_fyers_history_audit(monkeypatch, history_result=None, history_error=None):
    class FyersClient:
        def history(self, _request):
            if history_error is not None:
                raise history_error
            return history_result

    class FyersProvider(FakeProvider):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fyers = FyersClient()

    monkeypatch.setattr(
        audit,
        "get_settings",
        lambda: SimpleNamespace(
            angelone_api_key="",
            angelone_client_id="",
            angelone_password="",
            angelone_totp_secret="",
            active_indices=["NIFTY"],
        ),
    )
    monkeypatch.setattr(audit, "MarketDataProvider", FyersProvider)
    monkeypatch.setattr(audit.time, "sleep", lambda _seconds: None)
    return audit.run_end_to_end_audit(days=1)


def test_fyers_history_none_response_fails_readiness_audit(monkeypatch):
    result = _run_fyers_history_audit(monkeypatch, history_result=None)

    assert result["ok"] is False
    assert "NIFTY Fyers volume history" in result["failures"]


def test_fyers_history_exception_fails_readiness_audit(monkeypatch):
    result = _run_fyers_history_audit(
        monkeypatch,
        history_error=RuntimeError("history unavailable"),
    )

    assert result["ok"] is False
    assert "NIFTY Fyers volume history" in result["failures"]


def test_fyers_history_malformed_payload_fails_readiness_audit(monkeypatch):
    result = _run_fyers_history_audit(monkeypatch, history_result=["not", "a", "mapping"])

    assert result["ok"] is False
    assert "NIFTY Fyers volume history" in result["failures"]


def test_fyers_history_ok_with_empty_candles_fails_readiness_audit(monkeypatch):
    result = _run_fyers_history_audit(
        monkeypatch,
        history_result={"s": "ok", "candles": []},
    )

    assert result["ok"] is False
    assert "NIFTY Fyers volume history" in result["failures"]


def test_fyers_history_ok_with_zero_volume_fails_readiness_audit(monkeypatch):
    result = _run_fyers_history_audit(
        monkeypatch,
        history_result={"s": "ok", "candles": [[1, 100, 101, 99, 100, 0]]},
    )

    assert result["ok"] is False
    assert "NIFTY Fyers volume history" in result["failures"]


def test_fyers_history_ok_with_nan_volume_fails_readiness_audit(monkeypatch):
    result = _run_fyers_history_audit(
        monkeypatch,
        history_result={"s": "ok", "candles": [[1, 100, 101, 99, 100, float("nan")]]},
    )

    assert result["ok"] is False
    assert "NIFTY Fyers volume history" in result["failures"]


def test_fyers_history_ok_with_positive_volume_passes_that_check(monkeypatch):
    result = _run_fyers_history_audit(
        monkeypatch,
        history_result={"s": "ok", "candles": [[1, 100, 101, 99, 100, 10]]},
    )

    assert result["ok"] is True
    assert "NIFTY Fyers volume history" not in result["failures"]


def test_market_data_stream_shutdown_is_bounded_and_closes_both_sockets():
    closed = []

    class Socket:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed.append(self.name)

    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._fyers_ws_generation = 4
    provider._fyers_ws = Socket("fyers")
    provider.kws = Socket("kite")
    provider._fyers_ws_auth_failed = False
    provider._fyers_ws_connected = True
    provider._fyers_ws_connected_since = 1.0
    provider._kite_ws_connected = True

    assert provider.shutdown_streams(timeout=0.5) is True
    assert sorted(closed) == ["fyers", "kite"]
    assert provider._fyers_ws_generation == 5
    assert provider._fyers_ws is None and provider.kws is None
    assert provider._fyers_ws_auth_failed is True
