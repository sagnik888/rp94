"""The retired broker's SDK must not be a runtime dependency of the live data layer.

This file used to assert that logzero's logger was silenced to CRITICAL, because the
AngelOne SmartAPI SDK logged through logzero and could echo credentials. That SDK is gone:
data/market_data.py imported SmartConnect and SmartWebSocketV2 into module globals and
never constructed either, `self.smart_api` is None from __init__ onward so every legacy
Angel path is unreachable, and HAS_SMARTAPI was written twice and read nowhere.

Silencing a logger no component uses proves nothing. The property worth protecting now is
the one the old test was standing in for: no retired-broker SDK is imported at runtime,
so there is no SDK channel for a secret to leak through in the first place.
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


def test_no_production_module_imports_the_retired_broker_sdk():
    offenders = []
    for rel, path in _production_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        if IMPORT_PATTERN.search(text):
            offenders.append(rel)
    assert offenders == [], (
        f"the retired AngelOne SDK is still imported at runtime by: {offenders}"
    )


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
