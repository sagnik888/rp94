from types import SimpleNamespace
import importlib.util
from pathlib import Path

import pandas as pd

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

    audit.run_end_to_end_audit(days=1)

    joined = "\n".join(messages)
    assert "Quote: angel_ws" in joined
    assert "Freshest: angel_ws, age=0.2s" in joined
    assert "entry-ok" in joined
    assert "Source: unknown" not in joined
