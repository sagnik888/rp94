"""Pullback hedge decision logic (Phase 3, 2026-07-24).

Design: while a proven runner (>= the 0.4R crossover) rides a confirmed trend, an early
pullback can arm a FRESH-ATM opposite option leg, released when the move resumes.

BACKTEST VERDICT (archived sessions): the hedge armed only once in 8 sessions and lost
money at every lock-relief level, because the 85-90% runner lock exits the parent only
~4-5% below peak — leaving almost no pullback to hedge through. It therefore ships
DEFAULT OFF. These tests pin the decision logic so the feature stays correct and
tunable if it is ever revisited.
"""
from types import SimpleNamespace

import scanner as scanner_module
from scanner import Scanner


def _settings(**over):
    base = dict(
        pullback_hedge_enabled=True,
        pullback_hedge_trigger_lock_fraction=0.5,
        pullback_hedge_trigger_giveback_pct=0.0,
        pullback_hedge_resume_recovery_pct=50.0,
        pullback_hedge_max_per_trade=2,
        pullback_hedge_max_hold_minutes=15.0,
        # big-move gate (the hedge inherits it)
        trend_runner_let_winners_run_enabled=True,
        trend_runner_min_peak_r_multiple=0.4,
        trend_runner_big_move_min_adx=25.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _trade(inst_type="OPT", option_type="CE", direction="LONG", adx=30.0):
    return SimpleNamespace(inst_type=inst_type, option_type=option_type,
                           direction=direction, adx_value=adx)


def _arm(monkeypatch, trade, *, cur, peak, gain_r=1.0, regime="TRENDING_UP",
         used=0, stop=0.0, settings=None):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings or _settings())
    sc = Scanner.__new__(Scanner)
    return Scanner._pullback_hedge_arm(
        sc, trade, current_premium=cur, peak_premium=peak, gain_r=gain_r,
        regime=regime, hedges_used=used, runner_stop=stop,
    )


def test_disabled_by_default_setting(monkeypatch):
    ok, why = _arm(monkeypatch, _trade(), cur=170, peak=180, stop=172,
                   settings=_settings(pullback_hedge_enabled=False))
    assert ok is False and why == "disabled"


def test_arms_once_half_the_lock_distance_is_retraced(monkeypatch):
    # peak 180, runner stop 172 -> lock distance 8; 50% = 4 -> arm at <= 176.
    assert _arm(monkeypatch, _trade(), cur=176, peak=180, stop=172)[0] is True
    assert _arm(monkeypatch, _trade(), cur=178, peak=180, stop=172)[0] is False


def test_small_gain_never_hedges(monkeypatch):
    # Below the 0.4R crossover a trade must never pay a second premium.
    ok, why = _arm(monkeypatch, _trade(), cur=176, peak=180, gain_r=0.2, stop=172)
    assert ok is False and "not a hedgeable runner" in why


def test_counter_trend_never_hedges(monkeypatch):
    ok, why = _arm(monkeypatch, _trade(option_type="PE", direction="SHORT"),
                   cur=176, peak=180, regime="TRENDING_UP", stop=172)
    assert ok is False and "not a hedgeable runner" in why


def test_futures_parent_not_supported(monkeypatch):
    ok, why = _arm(monkeypatch, _trade(inst_type="FUT", option_type=""),
                   cur=176, peak=180, stop=172)
    assert ok is False and "OPT only" in why


def test_hedge_cap_blocks_churn(monkeypatch):
    ok, why = _arm(monkeypatch, _trade(), cur=176, peak=180, stop=172, used=2)
    assert ok is False and "cap" in why


def test_missing_runner_stop_does_not_arm(monkeypatch):
    ok, why = _arm(monkeypatch, _trade(), cur=176, peak=180, stop=0.0)
    assert ok is False and "runner stop unavailable" in why


def _release(monkeypatch, *, cur, peak, low, mins=0.0, settings=None):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings or _settings())
    sc = Scanner.__new__(Scanner)
    return Scanner._pullback_hedge_release(
        sc, current_premium=cur, peak_premium=peak, pullback_low=low, minutes_held=mins
    )


def test_release_when_move_resumes(monkeypatch):
    # peak 180, pullback low 170 -> depth 10; 50% recovery = 175.
    assert _release(monkeypatch, cur=176, peak=180, low=170)[0] is True
    assert _release(monkeypatch, cur=172, peak=180, low=170)[0] is False


def test_release_on_hold_cap(monkeypatch):
    ok, why = _release(monkeypatch, cur=171, peak=180, low=170, mins=20.0)
    assert ok is True and "hold cap" in why


def test_no_measurable_pullback_holds(monkeypatch):
    assert _release(monkeypatch, cur=180, peak=180, low=180)[0] is False


def test_hedge_ships_default_off():
    # Guard the documented decision: unproven money logic must not default on.
    from config.settings import Settings
    assert Settings(_env_file=None).pullback_hedge_enabled is False


def test_dashboard_toggle_is_fully_wired():
    """A whitelisted advanced field that is not ALSO forwarded/exposed is silently
    dropped (documented 2026-07-10 regression). Pin every hop of the A/B toggle."""
    from pathlib import Path
    import dashboard.server as srv

    assert "pullback_hedge_enabled" in srv._ADVANCED_CONFIG_FIELDS
    assert "pullback_hedge_enabled" in srv._ADVANCED_BOOLEAN_CONFIG_FIELDS

    root = Path(__file__).resolve().parents[1]
    server_src = (root / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert "pullback_hedge_enabled=msg.get(" in server_src

    js = (root / "dashboard" / "static" / "js" / "dashboard-settings.js").read_text(encoding="utf-8")
    assert "settPullbackHedge" in js                      # card injected
    assert "cfg.pullback_hedge_enabled" in js             # hydrated from server
    assert "pullback_hedge_enabled: document.getElementById" in js  # sent on save

    scanner_src = (root / "scanner.py").read_text(encoding="utf-8")
    assert '"pullback_hedge_enabled": getattr(get_settings()' in scanner_src  # broadcast

    # The switch card must ALSO be listed in the General tab's System Controls group —
    # injecting it into the modal body alone leaves it out of the rendered tab (that is
    # exactly why the toggle was initially invisible).
    system_group = js.split("makeCard('settings-system'")[1].split("]))")[0]
    assert "settPullbackHedge" in system_group

    # Bump the cache-buster whenever this bundle changes, or browsers keep serving the
    # old JS and the new control never appears for the operator.
    index_html = (root / "dashboard" / "static" / "index.html").read_text(encoding="utf-8")
    # Pins the CURRENT cache-buster, so bumping the bundle forces this to be updated
    # deliberately. Bumped to v25 on 2026-08-18 when the breakeven-stop,
    # give-back-cap and backtest-live-exit controls were added: they were present
    # in the JS and mounted in the DOM, but invisible in the running dashboard
    # because the version string still said v=24 and browsers served the cache.
    assert "dashboard-settings.js?v=31-stop-reentry-net" in index_html


def test_configure_applies_and_reverts_the_toggle(monkeypatch):
    """End-to-end: the dashboard toggle must actually change the live setting."""
    from types import SimpleNamespace as NS
    from config.settings import get_settings
    from scanner import Scanner

    settings = get_settings()
    original = settings.pullback_hedge_enabled
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

        assert sc.configure(pullback_hedge_enabled=True, persist_settings=False) is True
        assert get_settings().pullback_hedge_enabled is True
        assert sc.configure(pullback_hedge_enabled=False, persist_settings=False) is True
        assert get_settings().pullback_hedge_enabled is False
    finally:
        settings.pullback_hedge_enabled = original
