"""Stop-out re-entry (2026-07-25).

Gap: on trend days trades were stopped out early and the move then ran without us.
When the SAME direction re-validates in an aligned strong trend shortly after a
STOP-type exit, allow one more attempt by bypassing ONLY the post-exit cooldown.

The daily trade budget and the consecutive-loss halt are real risk limits and must
NEVER be bypassed — those tests are the important ones here.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from scanner import Scanner


def _settings(**over):
    base = dict(
        stop_out_reentry_enabled=True,
        stop_out_reentry_window_minutes=30.0,
        stop_out_reentry_max_per_index=1,
        stop_out_reentry_min_adx=25.0,
        index_cooldown_regime_scoped=True,
        ut_regime_adaptation=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _closed(instrument="NIFTY", direction="LONG", reason="STOP_HIT", minutes_ago=5.0, now=None):
    now = now or datetime(2026, 7, 24, 11, 0)
    return SimpleNamespace(
        instrument=instrument, direction=direction, exit_reason=reason,
        exit_time=now - timedelta(minutes=minutes_ago), pnl=-500.0,
    )


def _scanner(monkeypatch, closed_trades, regime="TRENDING_UP", settings=None, used=None):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings or _settings())
    sc = Scanner.__new__(Scanner)
    sc.trades = SimpleNamespace(closed_trades=list(closed_trades))
    sc.latest_regimes = {"NIFTY": regime}
    sc._stop_out_reentries = dict(used or {})
    return sc


NOW = datetime(2026, 7, 24, 11, 0)


def test_allows_same_direction_reentry_after_stop_on_aligned_trend(monkeypatch):
    sc = _scanner(monkeypatch, [_closed(now=NOW)])
    ok, why = sc._stop_out_reentry_allowed("NIFTY", "LONG", NOW, adx_value=32.0)
    assert ok is True
    assert "stop-out re-entry" in why


def test_disabled_by_default_setting(monkeypatch):
    sc = _scanner(monkeypatch, [_closed(now=NOW)],
                  settings=_settings(stop_out_reentry_enabled=False))
    ok, why = sc._stop_out_reentry_allowed("NIFTY", "LONG", NOW, adx_value=32.0)
    assert ok is False and why == "disabled"


def test_ships_enabled_with_conservative_caps():
    """Enabled on backtest evidence (73% win rate, +Rs 3,560 over 8 archived sessions),
    but only with caps that keep it a genuine second chance rather than churn."""
    from config.settings import Settings
    s = Settings(_env_file=None)
    assert s.stop_out_reentry_enabled is True
    assert s.stop_out_reentry_max_per_index == 1      # one second chance per index/day
    assert s.stop_out_reentry_window_minutes <= 30.0  # same move, not a new phase
    assert s.stop_out_reentry_min_adx >= 25.0         # confirmed strong trend only


@pytest.mark.parametrize("reason", ["TARGET_HIT", "PEAK_THREAT_EXIT", "STALE_SESSION_END",
                                    "PROFIT_GIVEBACK_GUARD"])
def test_non_stop_exits_do_not_qualify(monkeypatch, reason):
    sc = _scanner(monkeypatch, [_closed(reason=reason, now=NOW)])
    ok, why = sc._stop_out_reentry_allowed("NIFTY", "LONG", NOW, adx_value=32.0)
    assert ok is False and "not a stop-out" in why


@pytest.mark.parametrize("reason", ["STOP_HIT", "TRAILING_STOP", "EARLY_LOSS_CUT",
                                    "LOSS_THREAT_EXIT", "STOP_LOSS"])
def test_all_stop_flavours_qualify(monkeypatch, reason):
    sc = _scanner(monkeypatch, [_closed(reason=reason, now=NOW)])
    assert sc._stop_out_reentry_allowed("NIFTY", "LONG", NOW, adx_value=32.0)[0] is True


def test_opposite_direction_is_a_reversal_not_a_reentry(monkeypatch):
    sc = _scanner(monkeypatch, [_closed(direction="LONG", now=NOW)], regime="TRENDING_DOWN")
    ok, why = sc._stop_out_reentry_allowed("NIFTY", "SHORT", NOW, adx_value=32.0)
    assert ok is False and "opposite direction" in why


def test_outside_time_window_rejected(monkeypatch):
    sc = _scanner(monkeypatch, [_closed(minutes_ago=45.0, now=NOW)])
    ok, why = sc._stop_out_reentry_allowed("NIFTY", "LONG", NOW, adx_value=32.0)
    assert ok is False and "outside" in why


def test_non_trending_regime_rejected(monkeypatch):
    sc = _scanner(monkeypatch, [_closed(now=NOW)], regime="RANGING")
    ok, why = sc._stop_out_reentry_allowed("NIFTY", "LONG", NOW, adx_value=32.0)
    assert ok is False and "not a confirmed trend" in why


def test_trend_must_agree_with_direction(monkeypatch):
    sc = _scanner(monkeypatch, [_closed(direction="LONG", now=NOW)], regime="TRENDING_DOWN")
    ok, why = sc._stop_out_reentry_allowed("NIFTY", "LONG", NOW, adx_value=32.0)
    assert ok is False and "does not agree" in why


def test_weak_adx_rejected_but_unknown_adx_does_not_veto(monkeypatch):
    sc = _scanner(monkeypatch, [_closed(now=NOW)])
    assert sc._stop_out_reentry_allowed("NIFTY", "LONG", NOW, adx_value=18.0)[0] is False
    # ADX unavailable (0) must not silently disable the gate nor block everything.
    assert sc._stop_out_reentry_allowed("NIFTY", "LONG", NOW, adx_value=0.0)[0] is True


def test_per_index_cap_limits_second_chances(monkeypatch):
    sc = _scanner(monkeypatch, [_closed(now=NOW)], used={"NIFTY": 1})
    ok, why = sc._stop_out_reentry_allowed("NIFTY", "LONG", NOW, adx_value=32.0)
    assert ok is False and "cap" in why


def test_note_increments_the_cap_counter(monkeypatch):
    sc = _scanner(monkeypatch, [_closed(now=NOW)])
    assert sc._stop_out_reentry_allowed("NIFTY", "LONG", NOW, adx_value=32.0)[0] is True
    sc._note_stop_out_reentry("NIFTY")
    assert sc._stop_out_reentries["NIFTY"] == 1
    assert sc._stop_out_reentry_allowed("NIFTY", "LONG", NOW, adx_value=32.0)[0] is False


# ── the important guarantees: real risk limits are NEVER bypassed ────────────

def _budget_scanner(monkeypatch, *, trades_today, losses_today, settings=None):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings or _settings())
    sc = Scanner.__new__(Scanner)
    sc.trades = SimpleNamespace(closed_trades=[_closed(now=NOW)])
    sc.latest_regimes = {"NIFTY": "TRENDING_UP"}
    sc._stop_out_reentries = {}
    sc._reset_daily_counters = lambda: None
    sc._trades_today = dict(trades_today)
    sc._losses_today = dict(losses_today)
    sc.max_consecutive_losses = 3
    sc.index_cooldown_minutes = 4.0
    sc._dynamic_trade_budget = lambda instrument: (2, "test budget")
    sc._last_exit_time = {"NIFTY": NOW - timedelta(minutes=1)}  # inside cooldown
    sc.mode = "HISTORICAL"
    sc._index_cooldown_applies = lambda *_a: (True, "test")
    sc._reversion_window_active = lambda instrument: False
    sc.log_event = lambda *a, **k: None
    return sc


def test_reentry_bypasses_cooldown_only(monkeypatch):
    sc = _budget_scanner(monkeypatch, trades_today={"NIFTY": 1}, losses_today={"NIFTY": 1})
    allowed, why = sc._can_trade_instrument("NIFTY", NOW, direction="LONG", adx_value=32.0)
    assert allowed is True, why          # cooldown bypassed
    assert sc._stop_out_reentries["NIFTY"] == 1


def test_daily_trade_budget_is_never_bypassed(monkeypatch):
    sc = _budget_scanner(monkeypatch, trades_today={"NIFTY": 2}, losses_today={})
    allowed, why = sc._can_trade_instrument("NIFTY", NOW, direction="LONG", adx_value=32.0)
    assert allowed is False and "Max daily trades" in why


def test_consecutive_loss_halt_is_never_bypassed(monkeypatch):
    sc = _budget_scanner(monkeypatch, trades_today={"NIFTY": 1}, losses_today={"NIFTY": 3})
    allowed, why = sc._can_trade_instrument("NIFTY", NOW, direction="LONG", adx_value=32.0)
    assert allowed is False and "HALTED" in why


def test_dashboard_toggle_is_fully_wired():
    """A whitelisted advanced field that is not ALSO forwarded/exposed/placed in the
    General tab is silently dropped or invisible. Pin every hop."""
    from pathlib import Path
    import dashboard.server as srv

    assert "stop_out_reentry_enabled" in srv._ADVANCED_CONFIG_FIELDS
    assert "stop_out_reentry_enabled" in srv._ADVANCED_BOOLEAN_CONFIG_FIELDS

    root = Path(__file__).resolve().parents[1]
    assert "stop_out_reentry_enabled=msg.get(" in (root / "dashboard" / "server.py").read_text(encoding="utf-8")

    js = (root / "dashboard" / "static" / "js" / "dashboard-settings.js").read_text(encoding="utf-8")
    assert "settStopOutReentry" in js
    assert "cfg.stop_out_reentry_enabled" in js
    assert "stop_out_reentry_enabled: document" in js
    assert "settStopOutReentry" in js.split("makeCard('settings-system'")[1].split("]))")[0]

    scanner_src = (root / "scanner.py").read_text(encoding="utf-8")
    assert '"stop_out_reentry_enabled": getattr(get_settings()' in scanner_src


def test_configure_applies_and_reverts_the_toggle():
    from types import SimpleNamespace as NS
    from config.settings import get_settings

    settings = get_settings()
    original = settings.stop_out_reentry_enabled
    try:
        sc = Scanner.__new__(Scanner)
        sc.mode = "HISTORICAL"
        sc.auto_mode = False
        sc.on_notification = None
        sc.futures_sl_pct, sc.options_sl_pct, sc.max_daily_loss_pct = 0.3, 15.0, 3.0
        sc.risk_fut_pct, sc.risk_opt_pct = 2.0, 8.0
        sc.capital_fut, sc.capital_opt, sc.capital_total = 250000.0, 100000.0, 1100000.0
        sc.trades = NS(update_risk_settings=lambda *a, **k: None, has_live_exposure=lambda: False)
        sc.risk_manager = NS(update_settings=lambda *a, **k: None)
        sc._publish_config_snapshot = lambda *a, **k: None
        sc.mtf = NS(apply_engine_params=lambda *a, **k: None)

        assert sc.configure(stop_out_reentry_enabled=False, persist_settings=False) is True
        assert get_settings().stop_out_reentry_enabled is False
        assert sc.configure(stop_out_reentry_enabled=True, persist_settings=False) is True
        assert get_settings().stop_out_reentry_enabled is True
    finally:
        settings.stop_out_reentry_enabled = original
