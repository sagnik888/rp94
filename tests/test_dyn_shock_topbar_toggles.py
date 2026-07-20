"""DYN + SHOCK toggles: topbar placement (between EXPIRY and TF) + full wire path.

Covers HTML uniqueness/order, JS configure/sync commands, server allowlist,
scanner.configure → settings, broadcast config, and backend consumers:
  - session_mood_dynamic_enabled → _current_day_type
  - market_ltp_shock_enabled → _evaluate_market_ltp_shock early-out
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from dashboard.server import _valid_command_payload
from scanner import Scanner


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "dashboard" / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "dashboard" / "static" / "js" / "app.js").read_text(encoding="utf-8")


# ── HTML placement ───────────────────────────────────────────────────

def test_dyn_shock_ids_unique_and_between_expiry_and_tf():
    assert HTML.count('id="sessionMoodDynamicSwitch"') == 1
    assert HTML.count('id="marketLtpShockSwitch"') == 1
    assert "switch-label\">DYN<" in HTML.replace(" ", "") or "switch-label\">DYN</" in HTML or ">DYN</" in HTML
    assert "SHOCK" in HTML

    i_exp = HTML.find("expiryGammaSwitch")
    i_dyn = HTML.find("sessionMoodDynamicSwitch")
    i_shk = HTML.find("marketLtpShockSwitch")
    i_tf = HTML.find("topTimeframePolicy")
    assert i_exp > 0 and i_dyn > 0 and i_shk > 0 and i_tf > 0
    assert i_exp < i_dyn < i_shk < i_tf


def test_dyn_shock_not_on_regime_card():
    start = HTML.find("regime-mood-card")
    end = HTML.find("Options Health", start)
    assert start > 0 and end > start
    chunk = HTML[start:end]
    assert "sessionMoodDynamicSwitch" not in chunk
    assert "marketLtpShockSwitch" not in chunk
    # Regime card is horizons-only
    assert "rhPulse" in chunk and "rhHour" in chunk and "rhDay" in chunk


# ── JS wire ──────────────────────────────────────────────────────────

def test_js_sends_configure_for_both_toggles():
    assert "sessionMoodDynamicSwitch" in APP_JS
    assert "marketLtpShockSwitch" in APP_JS
    assert "session_mood_dynamic_enabled" in APP_JS
    assert "market_ltp_shock_enabled" in APP_JS
    # change handlers
    assert "cmd: 'configure'" in APP_JS or 'cmd: "configure"' in APP_JS
    assert "session_mood_dynamic_enabled: on" in APP_JS
    assert "market_ltp_shock_enabled: on" in APP_JS
    # server → UI sync
    assert "sessionMoodDynamicSwitchSync" in APP_JS or (
        "sessionMoodDynamicSwitch" in APP_JS and "session_mood_dynamic_enabled !== undefined" in APP_JS
    )


# ── Server command validation / allowlist ────────────────────────────

def test_configure_payload_accepts_dyn_and_shock_toggles():
    assert (
        _valid_command_payload(
            {
                "cmd": "configure",
                "session_mood_dynamic_enabled": False,
                "market_ltp_shock_enabled": False,
            }
        )
        is None
    )
    assert (
        _valid_command_payload(
            {
                "cmd": "configure",
                "session_mood_dynamic_enabled": True,
                "market_ltp_shock_enabled": True,
            }
        )
        is None
    )


def test_server_allowlist_includes_dyn_and_shock():
    from dashboard import server as srv

    fields = set(getattr(srv, "_ADVANCED_CONFIG_FIELDS", []) or [])
    # Some servers use a second allowlist for booleans
    bools = set(getattr(srv, "_BOOL_CONFIG_FIELDS", []) or [])
    allow = fields | bools
    # Read source if constants use different names
    src = Path(srv.__file__).read_text(encoding="utf-8")
    assert "session_mood_dynamic_enabled" in src
    assert "market_ltp_shock_enabled" in src
    assert 'session_mood_dynamic_enabled=msg.get("session_mood_dynamic_enabled")' in src
    assert 'market_ltp_shock_enabled=msg.get("market_ltp_shock_enabled")' in src


# ── Scanner configure + broadcast ────────────────────────────────────

def test_configure_sets_and_broadcasts_dyn_and_shock():
    settings = get_settings()
    saved_dyn = bool(getattr(settings, "session_mood_dynamic_enabled", True))
    saved_shock = bool(getattr(settings, "market_ltp_shock_enabled", True))
    try:
        st = Scanner.__new__(Scanner)
        # Minimal stubs for configure body that touches self
        st._last_configure_block_reason = ""
        st.mode = "PAPER"
        st.trades = SimpleNamespace(
            has_live_exposure=lambda: False,
            update_risk_settings=lambda *a, **k: None,
            mode="PAPER",
        )
        st.risk_manager = SimpleNamespace(update_settings=lambda **k: None)
        st.mtf = SimpleNamespace()
        st.data = None
        st.on_notification = None
        st.auto_mode = False
        st.inst_pref = "AUTO"
        st.capital_total = 100000
        st.capital_fut = 50000
        st.capital_opt = 50000
        st.risk_fut_pct = 1.0
        st.risk_opt_pct = 1.0
        st.futures_sl_pct = 0.15
        st.options_sl_pct = 30.0
        st.max_daily_loss_pct = 5.0
        st.user_lots = {}
        st.user_lots_fut = {}
        st.active_indices = ["NIFTY"]
        st.backtest_days = 1
        st._pending_live_signals = {}
        st._pending_opposite_exit = {}
        st._live_signal_recheck_eligible = {}
        st._live_signal_recheck_at = {}
        st.log_event = lambda *a, **k: None
        st._setting_enabled = Scanner._setting_enabled
        st._strict_setting_enabled = Scanner._strict_setting_enabled
        st._normalize_signal_grade_preference = lambda x: "auto"
        st._signal_fire_mode = lambda: "CLOSED"
        st._next_simulation_id = lambda: 1
        st._stamp_simulation_diagnostics = lambda: None
        st._hist_candidates_loaded = True
        st.get_broadcast_config = Scanner.get_broadcast_config.__get__(st, Scanner)

        # OFF both
        ok = Scanner.configure(
            st,
            session_mood_dynamic_enabled=False,
            market_ltp_shock_enabled=False,
            persist_settings=False,
        )
        assert ok is not False
        assert settings.session_mood_dynamic_enabled is False
        assert settings.market_ltp_shock_enabled is False
        cfg = st.get_broadcast_config()
        assert cfg.get("session_mood_dynamic_enabled") is False
        assert cfg.get("market_ltp_shock_enabled") is False

        # ON both
        Scanner.configure(
            st,
            session_mood_dynamic_enabled=True,
            market_ltp_shock_enabled=True,
            persist_settings=False,
        )
        assert settings.session_mood_dynamic_enabled is True
        assert settings.market_ltp_shock_enabled is True
        cfg2 = st.get_broadcast_config()
        assert cfg2.get("session_mood_dynamic_enabled") is True
        assert cfg2.get("market_ltp_shock_enabled") is True
    finally:
        settings.session_mood_dynamic_enabled = saved_dyn
        settings.market_ltp_shock_enabled = saved_shock


# ── Backend consumers ────────────────────────────────────────────────

def test_dyn_toggle_controls_current_day_type():
    settings = get_settings()
    saved = bool(getattr(settings, "session_mood_dynamic_enabled", True))
    try:
        scanner = Scanner.__new__(Scanner)

        def _intel(base, tf, allow_fallback=True):
            return {
                "session_context": {
                    "day_type": "TREND_DAY_UP",
                    "opening_day_type": "TREND_DAY_UP",
                    "effective_day_type": "TREND_DAY_DOWN",
                    "market_phase": "TREND_DAY_DOWN",
                    "phase_confidence": 0.80,
                }
            }

        scanner._cached_timeframe_intel = _intel
        scanner._setting_enabled = Scanner._setting_enabled

        settings.session_mood_dynamic_enabled = True
        assert scanner._current_day_type("NIFTY") == "TREND_DAY_DOWN"

        settings.session_mood_dynamic_enabled = False
        assert scanner._current_day_type("NIFTY") == "TREND_DAY_UP"
    finally:
        settings.session_mood_dynamic_enabled = saved


def test_shock_toggle_disables_evaluate_market_ltp_shock():
    settings = get_settings()
    saved = bool(getattr(settings, "market_ltp_shock_enabled", True))
    try:
        scanner = Scanner.__new__(Scanner)
        scanner._setting_enabled = Scanner._setting_enabled
        settings.market_ltp_shock_enabled = False
        # When disabled, must short-circuit to None without needing candles.
        out = Scanner._evaluate_market_ltp_shock(
            scanner, "NIFTY", "SELL", None, timeframe="5min"
        )
        assert out is None
    finally:
        settings.market_ltp_shock_enabled = saved


def test_shock_enabled_default_and_dyn_default_on_settings():
    s = get_settings.__wrapped__() if hasattr(get_settings, "__wrapped__") else None
    # Fresh field defaults on class
    from config.settings import Settings

    defaults = Settings()
    assert defaults.session_mood_dynamic_enabled is True
    assert defaults.market_ltp_shock_enabled is True
