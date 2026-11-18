"""Unit tests for UpstoxAuth session lifecycle, daily cutoff, and token decoding."""
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from data.upstox_auth import (
    IST,
    UpstoxAuth,
    decode_jwt_expiry,
    most_recent_token_cutoff,
    next_token_expiry,
    read_upstox_env,
)


class TestUpstoxAuthLifecycle(unittest.TestCase):
    def test_daily_token_cutoffs(self):
        cutoff = most_recent_token_cutoff()
        self.assertEqual(cutoff.tzinfo, IST)
        self.assertEqual(cutoff.hour, 3)
        self.assertEqual(cutoff.minute, 30)

        expiry = next_token_expiry()
        self.assertGreater(expiry, cutoff)
        self.assertEqual((expiry - cutoff).days, 1)

    def test_extract_code_variants(self):
        auth = UpstoxAuth(token_file=Path("temp_nonexistent.json"))
        # Raw code
        self.assertEqual(auth.extract_code("qHZ4HU"), "qHZ4HU")
        # Full redirect URL with query params
        url1 = "http://127.0.0.1:8500/api/upstox/callback?code=qHZ4HU&state=xyz"
        self.assertEqual(auth.extract_code(url1), "qHZ4HU")
        # Direct URL with code at end
        url2 = "http://127.0.0.1:8500/?code=my_auth_code_123"
        self.assertEqual(auth.extract_code(url2), "my_auth_code_123")
        # Empty
        self.assertEqual(auth.extract_code(""), "")

    def test_jwt_expiry_decoding(self):
        # Sample JWT with known expiry
        token = (
            "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ."
            "eyJzdWIiOiI3SkFUWUwiLCJqdGkiOiI2YWZlMTg0ZDVhZTdlMTdlNmY3NGUyZTQiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaWF0IjoxNzk1MDM3MjYxLCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNlcnZpY2UiLCJleHAiOjE3OTUwMzkyMDB9."
            "TOO064x8OJtpb7QMQgbFjXzxwM52rxqKGZcT6V10wr0"
        )
        exp = decode_jwt_expiry(token)
        self.assertIsNotNone(exp)
        self.assertEqual(exp.hour, 3)
        self.assertEqual(exp.minute, 30)

    @patch("data.upstox_auth.UpstoxAuth._verify_live")
    def test_complete_login_with_direct_token(self, mock_verify):
        mock_verify.return_value = (True, "", {"user_id": "7JATYL", "user_name": "UT1 User"})
        test_file = Path("tests/temp_test_upstox_token.json")
        try:
            auth = UpstoxAuth(token_file=test_file)
            token = (
                "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ."
                "eyJzdWIiOiI3SkFUWUwiLCJleHAiOjk5OTk5OTk5OTl9.signature"
            )
            res = auth.complete_login(token)
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["user_id"], "7JATYL")
            self.assertTrue(auth.has_valid_token())
            st = auth.status()
            self.assertTrue(st["connected"])
            self.assertEqual(st["status"], "LIVE")
        finally:
            if test_file.exists():
                test_file.unlink()


if __name__ == "__main__":
    unittest.main()
