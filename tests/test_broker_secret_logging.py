"""AngelOne's read-only adapter must not leak broker secrets.

This file used to assert that logzero's logger was silenced to CRITICAL, because the
AngelOne SmartAPI logs complete failing request headers through logzero. The narrow index
adapter must be the sole importer and must silence that logger before client creation.
"""
import pathlib
import re

PRODUCTION = ("data", "engine", "trading", "intelligence", "config", "dashboard")
ROOT = pathlib.Path(__file__).resolve().parents[1]
# A documentation sample, never imported by the system.
EXEMPT = {"engine/apex55/example_usage.py"}

IMPORT_PATTERN = re.compile(r"^\s*(from\s+SmartApi|import\s+SmartApi|from\s+smartapi|import\s+smartapi)", re.M)


def _production_sources():
    for package in PRODUCTION:
        for path in (ROOT / package).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in EXEMPT or "__pycache__" in rel:
                continue
            yield rel, path
    for name in ("scanner.py", "main.py"):
        path = ROOT / name
        if path.exists():
            yield name, path


def test_only_sanitized_adapter_imports_smartapi():
    offenders = []
    for rel, path in _production_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        if IMPORT_PATTERN.search(text):
            offenders.append(rel)
    assert offenders == ["data/angelone_market_data.py"]


def test_adapter_silences_sdk_and_never_logs_raw_exceptions():
    source = (ROOT / "data/angelone_market_data.py").read_text(encoding="utf-8")
    assert "smart_module.logger.setLevel(logging.CRITICAL)" in source
    assert 'logger.warning(f' not in source
    assert "logger.exception" not in source


def test_the_data_layer_still_redacts_sensitive_text():
    """Redaction itself is still used on the surviving REST error paths."""
    from trading.secure_logging import redact_sensitive_text

    assert "SECRET123" not in redact_sensitive_text("password=SECRET123")


def test_smart_api_handle_stays_none_so_legacy_paths_are_unreachable():
    """Every Angel branch is guarded on this attribute; it must never be populated."""
    import inspect

    from data.market_data import MarketDataProvider

    src = inspect.getsource(MarketDataProvider.__init__)
    assert "self.smart_api = None" in src
    assert "SmartConnect(" not in inspect.getsource(MarketDataProvider)
