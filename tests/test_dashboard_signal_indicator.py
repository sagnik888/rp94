import re
from pathlib import Path


def test_normal_scan_ticks_use_non_animated_running_indicator():
    app_js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")
    css = Path("dashboard/static/css/style.css").read_text(encoding="utf-8")

    signal_sync_match = re.search(
        r"function syncSignalState\(data\) \{(?P<body>.*?)\n\}",
        app_js,
        re.S,
    )
    assert signal_sync_match, "signal-state synchronizer not found"
    signal_sync = signal_sync_match.group("body")

    assert "data.scan_count > lastSystemScanCount" in signal_sync
    assert "state-running" in signal_sync
    assert "classList.add('state-analysing')" not in signal_sync
    assert "RUNNING" in signal_sync
    assert "syncSignalState(data);" in app_js
    assert ".analysing-signal.state-running .signal-dot" in css
    assert "animation: none" in css


def test_trade_panel_resimulation_notice_does_not_glow_panel_on_initial_bind():
    trade_js = Path("dashboard/static/js/trade-table.js").read_text(encoding="utf-8")

    assert "const previousSimId = state.lastSimId;" in trade_js
    assert "if (previousSimId && indicator)" in trade_js
    assert "panel.style.boxShadow" not in trade_js
    assert "RE-SIMULATION COMPLETE" in trade_js


def test_settings_engine_and_ut_trail_controls_are_explicit():
    settings_js = Path("dashboard/static/js/dashboard-settings.js").read_text(encoding="utf-8")
    css = Path("dashboard/static/css/style.css").read_text(encoding="utf-8")

    assert 'id="settScannerEngine"' in settings_js
    assert "SCANNER_ENGINE_OPTIONS" in settings_js
    assert "ensureScannerEngineOptions(settingsEl)" in settings_js
    assert "['quad_union', 'Quad Union']" in settings_js
    assert "['quad_prime', 'Quad Prime']" in settings_js
    assert "['ut_smart', 'UT Smart Exit']" in settings_js      # replaced Adaptive Dual 2026-09-25
    assert "cmd: 'set_scanner_engine'" in settings_js
    assert "settEngineLab" in settings_js
    assert "settEngineLabRealOnly" in settings_js
    assert "engine_lab_enabled" in settings_js
    assert "engine_lab_real_mode_only" in settings_js
    assert "UT Trail Sensitivity" in settings_js
    assert "Aggressive - Trail 1.0x / ATR 10" in settings_js
    assert "Balanced - Trail 1.3x / ATR 10" in settings_js
    assert "Conservative - Trail 1.6x / ATR 14" in settings_js
    assert "EMA/Consensus flips stay unchanged" in settings_js
    assert "Engine 11 dual-lane mode" in settings_js
    assert ".settings-control-help" in css


def test_connection_status_dot_is_steady_after_websocket_state_changes():
    app_js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")
    css = Path("dashboard/static/css/style.css").read_text(encoding="utf-8")

    status_dot_block = re.search(r"\.status-dot\s*\{(?P<body>.*?)\}", css, re.S)
    status_error_block = re.search(r"\.status-dot\.error\s*\{(?P<body>.*?)\}", css, re.S)
    assert status_dot_block, "base status dot CSS block not found"
    assert status_error_block, "error status dot CSS block not found"

    assert "animation: none" in status_dot_block.group("body")
    assert "animation: pulse" not in status_dot_block.group("body")
    assert "animation: none" in status_error_block.group("body")
    assert "classList.add('connected')" in app_js
    assert "classList.remove('error')" in app_js
    assert "classList.add('error')" in app_js
    assert "classList.remove('connected')" in app_js


def test_websocket_reconnect_does_not_rebind_control_listeners():
    app_js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")

    assert "connectionControlsBound: false" in app_js
    assert "if (state.connectionControlsBound) return;" in app_js
    assert "state.connectionControlsBound = true;" in app_js


def test_failed_token_bootstrap_is_retried_instead_of_cached_empty():
    app_js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")

    assert "if (window.dashboardAuthToken) return window.dashboardAuthToken;" in app_js
    assert "window.dashboardAuthToken = ''" not in app_js


def test_live_diagnostics_top_reject_does_not_show_backtest_window_label():
    app_js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")

    assert "const windowLabel = isHistorical && simulation.backtest_days" in app_js


def test_diagnostics_reject_panel_expands_and_hardware_moves_to_scanner_state():
    app_js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")
    css = Path("dashboard/static/css/style.css").read_text(encoding="utf-8")

    diag_section = html.split('id="diagContentView"', 1)[1].split('id="ut1StateContentView"', 1)[0]
    scanner_section = html.split('id="ut1StateContentView"', 1)[1].split('id="pnlContentView"', 1)[0]

    assert 'id="hardwareGrid"' not in diag_section
    assert 'class="hardware-grid scanner-hardware-grid" id="hardwareGrid"' in scanner_section
    assert scanner_section.index('id="hardwareGrid"') < scanner_section.index("Signal Bias")
    assert 'class="diag-reject-panel glass-card"' in diag_section
    assert 'id="diagRejectTotal"' in diag_section
    assert 'id="diagRejectInsights"' in diag_section
    assert 'id="diagRejectContext"' in diag_section
    assert ".diag-reject-panel" in css
    assert "#ut1StateContentView .scanner-hardware-grid" in css
    state_css = css.split("#ut1StateContentView {", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto !important;" in state_css
    assert "overflow: hidden !important;" not in state_css
    assert "const rejectEntries = Object.entries(rejects)" in app_js
    assert "classifyReject" in app_js
    assert "of rejects" in app_js


def test_ut1_state_panel_prefers_fresh_mtf_scanner_state():
    app_js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")
    css = Path("dashboard/static/css/style.css").read_text(encoding="utf-8")

    assert "function resolveScannerTfState(inst, timeframe)" in app_js
    assert "const mtfState = mtf[`state_${timeframe}`] || {};" in app_js
    assert "return hasScannerTfState(mtfState) ? mtfState" in app_js
    assert "chart state is only a fallback" in app_js
    assert "const tfState = resolveScannerTfState(inst, tf);" in app_js
    assert "Active scanner engine:" in app_js
    assert "resolveScannerTfSource(inst, tf)" in app_js
    assert "infoEngine" in html
    assert "infoTimeframe" in html
    assert "infoStateSource" in html
    assert "Scanner State" in html
    assert "app.js?v=" in html
    assert "style.css?v=" in html
    assert "#ut1StateContentView .ut-state-strip" in css
    assert "#ut1StateContentView .diag-grid" in css


def test_signal_table_time_columns_have_reserved_width():
    css = Path("dashboard/static/css/style.css").read_text(encoding="utf-8")
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")
    trade_js = Path("dashboard/static/js/trade-table.js").read_text(encoding="utf-8")

    assert "style.css?v=" in html
    assert "<th>LTP / EXIT</th>" in html
    assert "<th>CTRL</th>" in html
    assert "<th>EXIT PX</th>" not in html
    assert "#resultsTable th:nth-child(8), #resultsTable td:nth-child(8) { width: 4.8%; }" in css
    assert "#resultsTable th:nth-child(12), #resultsTable td:nth-child(12) { width: 6.2%; }" in css
    assert "#resultsTable th:nth-child(13), #resultsTable td:nth-child(13) { width: 6.2%; }" in css
    assert "#resultsTable th:nth-child(14), #resultsTable td:nth-child(14) { width: 5.8%; }" in css
    assert "padding-left: 8px;" in css
    assert "padding-right: 8px;" in css
    assert "min-width: 58px;" in css
    assert ".trade-control-btn.entry" in css
    assert ".trade-control-btn.exit" in css
    assert "function renderLtpExitCell" in trade_js
    assert "function renderTradeControlCell" in trade_js
    assert "manual_exit_trade_id" in trade_js
    assert "manual_entry_executed" in trade_js
    assert "Submit dashboard exit for this manual signal" in trade_js
    assert "function formatRiskReward" in trade_js
    assert "cmd: 'execute_signal'" in trade_js
    assert "1:${rrValue.toFixed(1)}" not in trade_js
    assert "(sig.rr || 1.5).toFixed(1)" not in trade_js


def test_engine_lab_panel_is_exposed_and_rendered():
    app_js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")
    css = Path("dashboard/static/css/style.css").read_text(encoding="utf-8")

    assert 'data-type="engine_lab"' in html
    assert "engineLabTabBadge" in html
    assert "function updateEngineLabPanel(lab = {}, data = state.latestData || {})" in app_js
    assert "updateEngineLabPanel(data.engine_lab || {}, data)" in app_js
    assert "function renderEngineLabTape(lab = {}, data = state.latestData || {})" in app_js
    assert "function resolveEngineLabActualSummary(data = {})" in app_js
    assert "TRADE MANAGER BACKTEST" in app_js
    assert "SPOT REPLAY" in app_js
    assert "RAW SHADOW" in app_js
    assert "row.actual_result === true" in app_js
    assert "active && hasActual" in app_js
    assert "signals: actual.trades" in app_js
    assert "total_money: actual.pnl" in app_js
    assert "Actual' : 'Replay'" in app_js
    assert "isActual ? 'Trades' : 'Signals'" in app_js
    assert "isActual ? 'P&L' : 'Gain Pts'" in app_js
    assert "row.raw_signals || 0" in app_js
    assert "activeActual.label" in app_js
    assert "backend restart pending" in app_js
    assert "badge.textContent = lab.enabled === false" in app_js
    assert "scannerEngineLabel(row.engine)" in app_js
    assert "row.wins || 0} / ${row.losses || 0}" in app_js
    assert "Number(row.sharpe || 0).toFixed(2)" in app_js
    assert ".intel-tab-badge.has-data" in css
    assert ".engine-lab-tape-row" in css
    assert "overflow-x: hidden" in css
    assert "grid-template-areas:" in css
    assert "state.lastRenderedLogData = ''" in app_js
    assert "previousScrollTop" in app_js
    assert ".engine-lab-tape-row.actual" in css
