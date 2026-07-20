"""Pre-market checklist alarm: Fyers daily-token freshness warning."""
import json
from datetime import datetime

import pytz

from engine.safe_mode import fyers_token_freshness_warning

IST = pytz.timezone("Asia/Kolkata")
NOW = IST.localize(datetime(2026, 7, 8, 8, 45, 0))


def _write_token(tmp_path, stamp):
    p = tmp_path / "fyers_token.json"
    p.write_text(json.dumps({"access_token": "x", "timestamp": stamp}), encoding="utf-8")
    return p


def test_fresh_today_token_is_quiet(tmp_path):
    p = _write_token(tmp_path, "2026-07-08T08:40:00.000000")
    assert fyers_token_freshness_warning(NOW, p) == ""


def test_yesterday_token_warns(tmp_path):
    p = _write_token(tmp_path, "2026-07-07T13:26:35.373285")
    msg = fyers_token_freshness_warning(NOW, p)
    assert "2026-07-07" in msg and "re-auth" in msg.lower()


def test_missing_token_file_warns(tmp_path):
    msg = fyers_token_freshness_warning(NOW, tmp_path / "absent.json")
    assert "missing" in msg


def test_unreadable_timestamp_warns(tmp_path):
    p = tmp_path / "fyers_token.json"
    p.write_text("{not json", encoding="utf-8")
    assert "unreadable" in fyers_token_freshness_warning(NOW, p)


def test_wired_into_readiness_validator():
    import inspect
    from engine import safe_mode
    src = inspect.getsource(safe_mode.validate_real_mode_readiness)
    assert "fyers_token_freshness_warning" in src
