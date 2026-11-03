from pathlib import Path
from types import SimpleNamespace

import dashboard.server as server


class _Cache:
    def state(self):
        return {
            "market_status": "OPEN",
            "mode": "REAL",
            "instruments": {
                "NIFTY": {"ltp": 1.0, "quote_source": "old_scan"},
            },
        }


def test_quote_endpoint_reads_provider_cache_not_scanner_loop(monkeypatch):
    calls = []

    class _Provider:
        def get_cached_index_quote_snapshot(self, symbol):
            calls.append(symbol)
            return {
                "ltp": 24001.25,
                "change_pct": 0.5,
                "quote_source": "kite_ws",
                "quote_label": "LIVE",
                "quote_age_seconds": 0.2,
                "quote_fresh": True,
            }

    scanner = SimpleNamespace(
        active_indices=["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"],
        data=_Provider(),
        dashboard_cache=_Cache(),
    )
    monkeypatch.setattr(server, "scanner_ref", scanner)

    payload = server._dashboard_index_quote_snapshot()

    assert calls == ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    assert payload["quote_lane"] == "provider_cache"
    assert payload["market_status"] == "OPEN"
    assert payload["instruments"]["NIFTY"]["ltp"] == 24001.25
    assert payload["instruments"]["NIFTY"]["quote_age_seconds"] == 0.2


def test_quote_endpoint_falls_back_to_scan_cache_during_rolling_restart(monkeypatch):
    scanner = SimpleNamespace(
        active_indices=["NIFTY"],
        data=SimpleNamespace(),
        dashboard_cache=_Cache(),
    )
    monkeypatch.setattr(server, "scanner_ref", scanner)

    payload = server._dashboard_index_quote_snapshot()

    assert payload["quote_lane"] == "scan_cache_fallback"
    assert payload["instruments"]["NIFTY"]["ltp"] == 1.0


def test_frontend_polls_quotes_without_rerendering_the_whole_dashboard():
    js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")

    assert "setInterval(refreshIndexQuotes, 500)" in js
    assert "authFetch('/api/quotes'" in js
    assert "updateInstrumentSelectors(normalized, merged);" in js
    assert "updateDashboard(merged);" not in js.split("async function refreshIndexQuotes", 1)[1].split("async function connectWebSocket", 1)[0]
    assert "RUNNING" in js
    assert "state-running" in js
    for name in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"):
        assert f'id="quote_{name}"' in html


def test_quote_read_api_is_local_and_token_protected():
    assert "/api/quotes" in server._LOCAL_ONLY_API_PATHS
    assert "/api/quotes" in server._TOKEN_REQUIRED_API_PATHS


def test_dashboard_controls_and_scroll_regions_have_accessible_names():
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")
    assert 'id="topTimeframePolicy" class="tf-policy-select modern-select" aria-label=' in html
    assert 'id="activityLog" role="region" tabindex="0" aria-label=' in html
    assert 'id="diagContentView" class="intel-view-pane" role="region" tabindex="0" aria-label=' in html
    assert 'id="diagRejectList" role="region" tabindex="0" aria-label=' in html
    # Every switch/checkbox has a programmatic name, including visually-hidden inputs.
    for line in html.splitlines():
        if 'type="checkbox"' in line:
            assert 'aria-label=' in line


def test_index_quote_badges_are_two_row_contained_and_cache_busted():
    css = Path("dashboard/static/css/style.css").read_text(encoding="utf-8")
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")

    card_rule = css.split(".chart-header-mode .inst-tab {", 1)[1].split("}", 1)[0]
    quote_rule = css.split(".inst-quote-status {", 1)[1].split("}", 1)[0]
    scoped_quote_rule = css.rsplit(".chart-header-mode .inst-quote-status {", 1)[1].split("}", 1)[0]

    assert '"name quote"' in card_rule
    assert '"price change"' in card_rule
    assert "minmax(0, 1fr) minmax(43px, max-content)" in card_rule
    assert "overflow: hidden" in card_rule
    assert "max-width: 100%" in quote_rule
    assert "overflow: hidden" in quote_rule
    assert "text-overflow: ellipsis" in quote_rule
    assert "grid-area: quote" in scoped_quote_rule
    assert "style.css?v=32-angelone-badge" in html


def test_trade_table_normalizes_iso_time_instead_of_substringing_the_date():
    js = Path("dashboard/static/js/trade-table.js").read_text(encoding="utf-8")
    assert "function normalizeTradeDateTime" in js
    assert "entryClock.timeText" in js
    assert "entryTime.substring(0, 8)" not in js
    assert 'trade-table.js?v=' in Path(
        "dashboard/static/index.html"
    ).read_text(encoding="utf-8")
