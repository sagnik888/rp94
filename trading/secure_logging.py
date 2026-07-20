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
    r"refresh[_-]?token|feed[_-]?token|jwt[_-]?token|password|totp"
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
