"""Redact broker credentials before third-party SDK records reach handlers."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any


_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+\-/=]+")
_AUTHORIZATION_RE = re.compile(
    r"(?i)((?:['\"])?authorization(?:['\"])?\s*[:=]\s*(?:['\"])?)(?:Bearer\s+)?[^'\",}\]]+"
)
_SECRET_FIELD_RE = re.compile(
    r"(?ix)"
    r"((?:['\"])?(?:"
    r"x-privatekey|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|feed[_-]?token|jwt[_-]?token|password|mpin|pin|totp"
    r")(?:['\"])?\s*[:=]\s*(?:['\"])?)([^'\"\s,}\]]+)"
)


def redact_sensitive_text(value: Any) -> str:
    """Return log-safe text while preserving non-secret diagnostic context."""
    text = str(value or "")
    text = _AUTHORIZATION_RE.sub(r"\1<redacted>", text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    return _SECRET_FIELD_RE.sub(r"\1<redacted>", text)


class SensitiveDataFilter(logging.Filter):
    """Mutate a LogRecord before any console or file handler formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(getattr(record, "msg", "") or "")
        record.msg = redact_sensitive_text(rendered)
        record.args = ()
        return True


class _SdkToLoguruHandler(logging.Handler):
    """Forward SmartAPI's logzero records into the application log.

    The SDK reports the broker's ACTUAL failure reason through its own logger — see
    SmartApi/smartConnect.py `_request` ("Error occurred while making a POST request ...
    Error: {message}") and `placeOrderFullResponse` ("API request failed: {response}" /
    "Invalid response format: {response}"). None of it reached ut1.log, because logzero is
    a separate logger from the application's, so an order that AngelOne refused arrived in
    our code as an unexplained response and was reported only as "acknowledgement is
    ambiguous". Records pass through SensitiveDataFilter first, so tokens stay redacted.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            from loguru import logger as _loguru

            message = redact_sensitive_text(str(record.getMessage()))
            level = record.levelname if record.levelname in {
                "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
            } else "INFO"
            _loguru.bind(sdk="smartapi").log(level, f"[SmartAPI] {message}")
        except Exception:
            pass


def install_smartapi_log_redaction() -> bool:
    """Install one logger-level filter on the logzero logger used by SmartAPI."""
    try:
        import logzero

        sdk_logger = logzero.logger
        marker = "_ut_sensitive_data_filter"
        log_filter = getattr(sdk_logger, marker, None)
        if log_filter is None:
            log_filter = SensitiveDataFilter()
            sdk_logger.addFilter(log_filter)
            setattr(sdk_logger, marker, log_filter)

        # Also mirror the SDK's own diagnostics into ut1.log. Without this the broker's
        # reason for refusing an order is written only to logzero's handlers and is lost.
        capture_marker = "_ut_sdk_capture_handler"
        if getattr(sdk_logger, capture_marker, None) is None:
            handler = _SdkToLoguruHandler()
            handler.setLevel(logging.INFO)
            sdk_logger.addHandler(handler)
            setattr(sdk_logger, capture_marker, handler)
        return True
    except Exception:
        return False


def redact_sensitive_log_file(path: Any) -> bool:
    """Mechanically sanitize an existing SDK text log while preserving evidence."""
    target = Path(path)
    if not target.exists() or not target.is_file():
        return False
    original = target.read_text(encoding="utf-8", errors="replace")
    redacted = redact_sensitive_text(original)
    if redacted == original:
        return False
    target.write_text(redacted, encoding="utf-8")
    return True
