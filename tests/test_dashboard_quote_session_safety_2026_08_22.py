"""Static contract checks for dashboard quote-session rendering.

The browser bundle is deliberately dependency-free, so these assertions keep the
runtime context hand-off and the safety precedence visible without introducing a JS
test runner solely for one small pure display rule.  ``node --check`` is run alongside
this file to validate the complete bundle's syntax.
"""

from pathlib import Path


APP_JS = Path("dashboard/static/js/app.js")


def _function_body(source: str, name: str, next_name: str) -> str:
    return source.split(f"function {name}", 1)[1].split(f"function {next_name}", 1)[0]


def test_quote_lane_passes_runtime_context_without_full_dashboard_render():
    source = APP_JS.read_text(encoding="utf-8")
    refresh = _function_body(source, "refreshIndexQuotes", "connectWebSocket")

    assert "updateInstrumentSelectors(normalized, merged);" in refresh
    assert "updateDashboard(merged);" not in refresh
    assert "setInterval(refreshIndexQuotes, 500)" in source


def test_historical_and_closed_context_override_a_cached_live_quote():
    source = APP_JS.read_text(encoding="utf-8")
    normalizer = _function_body(source, "normalizeQuoteLabel", "quoteTooltip")
    cards = _function_body(source, "updateInstrumentSelectors", "normalizeQuoteLabel")

    assert "normalizeQuoteLabel(d, dashboardContext)" in cards
    assert "runtimeMode === 'HISTORICAL'" in normalizer
    assert "marketStatus === 'HISTORICAL'" in normalizer
    assert "return 'HISTORICAL'" in normalizer
    assert "marketStatus === 'CLOSED'" in normalizer
    assert "return 'CLOSED'" in normalizer
    assert "sessionLabel === 'LIVE' ? signal : sessionLabel" in cards
    assert "sessionLabel === 'HISTORICAL'" in cards


def test_real_open_live_quote_path_remains_available():
    source = APP_JS.read_text(encoding="utf-8")
    normalizer = _function_body(source, "normalizeQuoteLabel", "quoteTooltip")

    assert "if (raw === 'LIVE' || raw === 'OPEN') return 'LIVE';" in normalizer
