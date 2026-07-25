"""Confirmed-live-trend "let winners run" (2026-07-24 audit).

On 2026-07-24 strong trends were mislabelled RANGE_DAY, which (a) force-armed
scalp-lock and (b) put the profit give-back guard in non-trend scope, so winners
were cut ~70% of peak (BANKNIFTY peak 1315 -> exit 944; SENSEX peak 626 -> 186).

`_confirmed_trend_runner` lets a with-trend PROVEN winner in a genuine strong-trend
regime ride the runner lock (10% give-back) instead of the tighter give-back guard,
independent of the lagging session day-type classifier. EOD / unconfirmed-repaint
protection is preserved by the caller.
"""
from types import SimpleNamespace

import scanner as scanner_module
from scanner import Scanner


def _settings(enabled=True, min_r=1.0):
    return SimpleNamespace(
        trend_runner_let_winners_run_enabled=enabled,
        trend_runner_min_peak_r_multiple=min_r,
    )


def _trade(direction="LONG", inst_type="OPT", option_type="CE"):
    return SimpleNamespace(direction=direction, inst_type=inst_type, option_type=option_type)


def _runner(monkeypatch, trade, *, regime, peak, risk, settings=None):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings or _settings())
    sc = Scanner.__new__(Scanner)
    return Scanner._confirmed_trend_runner(
        sc, trade, regime=regime, peak_pnl=peak, initial_risk_rs=risk
    )


def test_with_trend_proven_winner_is_runner(monkeypatch):
    # CE (bullish) in TRENDING_UP, peak 1200 >= 1R(1000) -> runner (let it run).
    assert _runner(monkeypatch, _trade("LONG", "OPT", "CE"),
                   regime="TRENDING_UP", peak=1200.0, risk=1000.0) is True
    # PE (bearish) in TRENDING_DOWN also with-trend.
    assert _runner(monkeypatch, _trade("SHORT", "OPT", "PE"),
                   regime="TRENDING_DOWN", peak=1200.0, risk=1000.0) is True
    # Futures LONG in BREAKOUT_UP.
    assert _runner(monkeypatch, _trade("LONG", "FUT", ""),
                   regime="BREAKOUT_UP", peak=1200.0, risk=1000.0) is True


def test_counter_trend_is_not_runner(monkeypatch):
    # PE (bearish) in TRENDING_UP is counter-trend -> must NOT get runner treatment.
    assert _runner(monkeypatch, _trade("SHORT", "OPT", "PE"),
                   regime="TRENDING_UP", peak=1200.0, risk=1000.0) is False


def test_small_peak_not_yet_runner(monkeypatch):
    # Peak 400 < 1R(1000): not a proven winner, so give-back protection still applies.
    assert _runner(monkeypatch, _trade("LONG", "OPT", "CE"),
                   regime="TRENDING_UP", peak=400.0, risk=1000.0) is False


def test_ranging_regime_not_runner(monkeypatch):
    assert _runner(monkeypatch, _trade("LONG", "OPT", "CE"),
                   regime="RANGING", peak=1200.0, risk=1000.0) is False


def test_disabled_setting_reverts(monkeypatch):
    assert _runner(monkeypatch, _trade("LONG", "OPT", "CE"),
                   regime="TRENDING_UP", peak=1200.0, risk=1000.0,
                   settings=_settings(enabled=False)) is False


def _lock(monkeypatch, regime, base=0.90, high_vol=0.85, enabled=True):
    monkeypatch.setattr(
        scanner_module, "get_settings",
        lambda: SimpleNamespace(
            trend_runner_dynamic_lock_enabled=enabled,
            trend_runner_lock_pct_high_vol=high_vol,
        ),
    )
    sc = Scanner.__new__(Scanner)
    return Scanner._dynamic_runner_lock_pct(sc, base, regime)


def test_dynamic_lock_looser_in_high_vol(monkeypatch):
    # Volatile / breakout tape -> looser lock (0.85, more room) so noise doesn't shake out.
    assert _lock(monkeypatch, "VOLATILE") == 0.85
    assert _lock(monkeypatch, "BREAKOUT_UP") == 0.85
    assert _lock(monkeypatch, "BREAKOUT_DOWN") == 0.85


def test_dynamic_lock_tighter_in_calm(monkeypatch):
    # Calm trend / range -> base lock (0.90, tighter).
    assert _lock(monkeypatch, "TRENDING_UP") == 0.90
    assert _lock(monkeypatch, "RANGING") == 0.90


def test_dynamic_lock_disabled_returns_base(monkeypatch):
    assert _lock(monkeypatch, "VOLATILE", enabled=False) == 0.90


def test_dynamic_lock_bounds_clamped(monkeypatch):
    assert _lock(monkeypatch, "TRENDING_UP", base=1.5) == 0.98
    assert _lock(monkeypatch, "TRENDING_UP", base=0.10) == 0.25


def _big(monkeypatch, trade, *, gain_r, regime, min_r=0.4, min_adx=25.0, enabled=True):
    monkeypatch.setattr(
        scanner_module, "get_settings",
        lambda: SimpleNamespace(
            trend_runner_let_winners_run_enabled=enabled,
            trend_runner_min_peak_r_multiple=min_r,
            trend_runner_big_move_min_adx=min_adx,
        ),
    )
    sc = Scanner.__new__(Scanner)
    return Scanner._big_move_expected(sc, trade, gain_r=gain_r, regime=regime)[0]


def _t(direction="LONG", inst_type="OPT", option_type="CE", adx=30.0):
    return SimpleNamespace(direction=direction, inst_type=inst_type,
                           option_type=option_type, adx_value=adx)


def test_small_gain_below_min_r_is_small_move(monkeypatch):
    # 0.4R crossover: below 0.4R the current guards run, no ride.
    for gr in (0.05, 0.10, 0.25, 0.35):
        assert _big(monkeypatch, _t(), gain_r=gr, regime="TRENDING_UP") is False


def test_at_or_above_crossover_rides(monkeypatch):
    # At/above the 0.4R crossover, a with-trend strong move hands off to the runner.
    assert _big(monkeypatch, _t(adx=30.0), gain_r=0.4, regime="TRENDING_UP") is True
    assert _big(monkeypatch, _t(adx=30.0), gain_r=0.6, regime="TRENDING_UP") is True


def test_proven_gain_in_strong_trend_rides(monkeypatch):
    assert _big(monkeypatch, _t(adx=30.0), gain_r=1.2, regime="TRENDING_UP") is True
    assert _big(monkeypatch, _t("SHORT", "OPT", "PE", adx=32.0), gain_r=1.5, regime="TRENDING_DOWN") is True


def test_proven_gain_but_weak_adx_does_not_ride(monkeypatch):
    # Big gain but ADX fading below floor -> move losing strength, bank it.
    assert _big(monkeypatch, _t(adx=18.0), gain_r=1.5, regime="TRENDING_UP") is False


def test_unknown_adx_does_not_veto(monkeypatch):
    # ADX unknown (0) must not block an otherwise-qualifying big move.
    assert _big(monkeypatch, _t(adx=0.0), gain_r=1.5, regime="TRENDING_UP") is True


def test_counter_trend_big_gain_does_not_ride(monkeypatch):
    assert _big(monkeypatch, _t("SHORT", "OPT", "PE", adx=40.0), gain_r=2.0, regime="TRENDING_UP") is False


def test_gain_in_r_helper():
    # entry 100, initial stop 90 -> risk 10 pts; +15 move -> 1.5R
    t = SimpleNamespace(entry_price=100.0, initial_stop=90.0, trailing_stop=90.0)
    assert Scanner._trade_gain_in_r(t, 15.0) == 1.5
    # unknown risk -> 0
    assert Scanner._trade_gain_in_r(SimpleNamespace(entry_price=0.0, initial_stop=0.0, trailing_stop=0.0), 15.0) == 0.0


def test_giveback_guard_wires_the_runner_suppression():
    # Structural guard: the give-back arming honours the trend-runner suppression and
    # still preserves EOD / repaint protection.
    import inspect
    src = inspect.getsource(Scanner._peak_giveback_or_reversal_exit)
    assert "trend_runner_active" in src
    assert "_confirmed_trend_runner" in src
    assert "_eod_protect_active" in src  # EOD protection preserved
    assert "and not trend_runner_active" in src
